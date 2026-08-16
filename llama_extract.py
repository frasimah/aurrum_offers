# -*- coding: utf-8 -*-
"""
Единственный извлекатель: текст или PDF -> структура по нашей схеме.

Отдельным слоем, потому что им пользуются оба пути — страница бренда
и техлист. Схема и инструкция лежат в `config/` и правятся как код.

Почему не извлечение Firecrawl, которое здесь было раньше: замеры на
живых страницах показали три разных отказа одного и того же слоя.
На FENDI CASA он вернул габариты, которых на странице нет вовсе, — и
карточка посчитала по ним объём. На VENICEM потерял высоту, оставив
диаметр. На BAROVIER возвращал то четыре исполнения, то одно. Тот же
документ через LlamaExtract по нашей схеме дал четыре исполнения в двух
прогонах подряд и не перевёл названия материалов.

Firecrawl остался доставкой: он отрисовывает скрипты, проходит антибот
и отдаёт markdown со ссылками. В этом он хорош.
"""

from __future__ import annotations

import io
import json
import os
import time

import httpx

import safe_fetch

API = "https://api.cloud.llamaindex.ai/api/v1"
CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")

POLL_INTERVAL = 4
POLL_ATTEMPTS = 30          # ~2 минуты
MAX_PDF_MB = 40
# Меньше этого — считаем, что текстового слоя нет (скан или кривые).
MIN_PDF_TEXT = 200

# Модель закреплена намеренно: без этого работает дефолт провайдера, и он
# может смениться без единой правки с нашей стороны. Режим PREMIUM
# допускает только openai-gpt-4-1, openai-gpt-5 и openai-gpt-5-mini.
EXTRACT_MODEL = os.environ.get("LLAMA_EXTRACT_MODEL", "").strip() or "openai-gpt-5"


def _key() -> str:
    key = os.environ.get("LLAMA_CLOUD_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "Не задан LLAMA_CLOUD_API_KEY. Добавьте его в .env "
            "(образец — в .env.example)."
        )
    return key


def _read(name: str) -> str:
    with open(os.path.join(CONFIG, name), encoding="utf-8") as f:
        return f.read()


def _config() -> dict:
    return {
        "system_prompt": _read("extraction_prompt.txt"),
        "use_reasoning": True,
        # Иначе правка инструкции молча не действует: кеш её не учитывает.
        "invalidate_cache": True,
        "extract_model": EXTRACT_MODEL,
    }


def _run(payload: dict) -> dict:
    headers = {"Authorization": f"Bearer {_key()}"}
    body = {
        "data_schema": json.loads(_read("extraction_schema.json")),
        "config": _config(),
        **payload,
    }
    with httpx.Client(timeout=180) as http:
        started = http.post(f"{API}/extraction/run", headers=headers, json=body)
        started.raise_for_status()
        # Ответ /run — это job, а не run: опрашивать надо /extraction/jobs.
        job = started.json()["id"]

        for _ in range(POLL_ATTEMPTS):
            state = http.get(f"{API}/extraction/jobs/{job}", headers=headers).json()
            status = state.get("status")
            if status == "SUCCESS":
                break
            if status in ("ERROR", "FAILED", "CANCELLED"):
                raise RuntimeError(
                    "Извлечение не справилось: "
                    + str(state.get("error") or "причина не указана")
                )
            time.sleep(POLL_INTERVAL)
        else:
            raise RuntimeError("Извлечение не ответило вовремя — попробуйте ещё раз.")

        result = http.get(f"{API}/extraction/jobs/{job}/result", headers=headers)
        result.raise_for_status()
        return result.json().get("data") or {}


def from_text(text: str) -> dict:
    """Разбор готового текста — так работает страница бренда."""
    text = (text or "").strip()
    if not text:
        return {}
    return _run({"text": text})


def _pdf_text(data: bytes) -> str:
    """Текстовый слой PDF своими силами.

    Так мы не тратим квоту LlamaCloud на загрузку файлов и не зависим от
    неё вовсе. Сверено на техлисте TRUSSARDI VIBES: разбор этого текста
    даёт те же пять исполнений с артикулами, что и загрузка самого файла.
    """
    try:
        from pypdf import PdfReader
    except ImportError:          # pragma: no cover — пакет в requirements
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:            # noqa: BLE001 — битый или зашифрованный файл
        return ""


def from_pdf_url(url: str) -> dict:
    """Разбор техлиста по ссылке."""
    # Ссылку присылает клиент — качаем с проверкой адреса назначения.
    got = safe_fetch.get(url, timeout=180, headers={"User-Agent": "Mozilla/5.0"})
    if len(got.content) > MAX_PDF_MB * 1024 * 1024:
        raise RuntimeError(f"Документ больше {MAX_PDF_MB} МБ — не разбираю.")

    # Текстовый слой есть почти всегда — тогда файл никуда не загружаем.
    text = _pdf_text(got.content)
    if len(text.strip()) >= MIN_PDF_TEXT:
        return _run({"text": text})

    # Текста нет: документ отсканирован или нарисован кривыми. Такой
    # разбирается только как картинка, а это уже загрузка файла.
    name = url.split("?")[0].rsplit("/", 1)[-1] or "document"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"

    headers = {"Authorization": f"Bearer {_key()}"}
    with httpx.Client(timeout=180) as http:
        up = http.post(f"{API}/files", headers=headers,
                       files={"upload_file": (name, got.content, "application/pdf")})
        if up.status_code == 402:
            # Сюда попадают только документы без текстового слоя: обычные
            # техлисты разбираются текстом и загрузки не требуют.
            raise RuntimeError(
                "В документе нет текста — его можно разобрать только "
                "картинкой, а квота LlamaCloud на загрузку файлов исчерпана."
            )
        up.raise_for_status()
    return _run({"file_id": up.json()["id"]})
