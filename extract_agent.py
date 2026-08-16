# -*- coding: utf-8 -*-
"""
Извлечение данных из техлиста через LlamaExtract.

Без сохранённого агента: схема и инструкция уходят прямо в запросе
(`POST /api/v1/extraction/run`). Оба файла лежат в `config/` и правятся
как обычный код — конфиг агента в интерфейсе LlamaCloud через публичный
API не читается и не пишется, и каждая правка там стоила бы ручной
вставки.

Чем это лучше регулярки из doc_parser: та находит только числа. Здесь
приезжает артикул исполнения и размер матраса — то, по чему исполнение
и выбирают.

Списки значений проверяет Python: LlamaExtract не соблюдает `enum`
в схеме, а молча возвращает последнее значение списка (проверено —
кровать становилась «Ковёр»). Поэтому `type_ru` и `role_ru` приходят
свободными строками и сверяются здесь.
"""

from __future__ import annotations

import json
import os
import time

import httpx

import product_lookup as pl
import safe_fetch

API = "https://api.cloud.llamaindex.ai/api/v1"
CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")

POLL_INTERVAL = 4
POLL_ATTEMPTS = 30          # ~2 минуты
MAX_PDF_MB = 40


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


def extract_pdf(url: str) -> dict:
    """Скачивает PDF и возвращает извлечённые данные по нашей схеме."""
    headers = {"Authorization": f"Bearer {_key()}"}
    schema = json.loads(_read("extraction_schema.json"))
    config = {
        "system_prompt": _read("extraction_prompt.txt"),
        "use_reasoning": True,
        # Иначе правка инструкции молча не действует: кеш её не учитывает.
        "invalidate_cache": True,
    }

    # Ссылку на документ присылает клиент — качаем с проверкой адреса,
    # иначе роут превращается в прокси во внутреннюю сеть.
    got = safe_fetch.get(url, timeout=180, headers={"User-Agent": "Mozilla/5.0"})

    with httpx.Client(timeout=180, follow_redirects=True) as http:
        if len(got.content) > MAX_PDF_MB * 1024 * 1024:
            raise RuntimeError(f"Документ больше {MAX_PDF_MB} МБ — не разбираю.")

        name = url.split("?")[0].rsplit("/", 1)[-1] or "document"
        if not name.lower().endswith(".pdf"):
            name += ".pdf"

        up = http.post(f"{API}/files", headers=headers,
                       files={"upload_file": (name, got.content, "application/pdf")})
        up.raise_for_status()

        started = http.post(f"{API}/extraction/run", headers=headers, json={
            "data_schema": schema,
            "config": config,
            "file_id": up.json()["id"],
        })
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
                    "LlamaExtract не смог разобрать документ: "
                    + str(state.get("error") or "причина не указана")
                )
            time.sleep(POLL_INTERVAL)
        else:
            raise RuntimeError("LlamaExtract не ответил вовремя — попробуйте ещё раз.")

        result = http.get(f"{API}/extraction/jobs/{job}/result", headers=headers)
        result.raise_for_status()
        return result.json().get("data") or {}


def to_candidates(data: dict) -> tuple[list[dict], list[dict], list[str]]:
    """Данные извлечения -> исполнения, отделки, предупреждения.

    Габариты и объём считает Python: набор чисел извлечение отдаёт верный,
    а по осям раскладывает нестабильно.
    """
    warnings: list[str] = []
    candidates: list[dict] = []
    finishes: list[dict] = []
    brand = (data.get("brand") or "").strip()

    for product in data.get("products") or []:
        model = (product.get("model") or "").strip()

        type_ru, warning = pl.normalize_type(product.get("type_ru") or "")
        if warning:
            warnings.append(warning)

        for f in product.get("finishes") or []:
            material = (f.get("material") or "").strip()
            if not material:
                continue
            role, warning = pl.normalize_role(f.get("role_ru") or "")
            if warning and warning not in warnings:
                warnings.append(warning)
            finishes.append({"role_ru": role, "material": material,
                             "code": (f.get("code") or "").strip() or None})

        for v in product.get("variants") or []:
            dims_raw = (v.get("dims_raw") or "").strip()
            if not dims_raw:
                continue
            w, d, h, sure = pl.parse_dims(dims_raw, type_ru)

            declared = v.get("packed_volume_m3")
            if isinstance(declared, (int, float)) and declared > 0:
                volume, source = round(float(declared), 2), "производитель"
            else:
                volume, source = pl.volume_m3(w, d, h), "формула"

            candidates.append({
                "sku": (v.get("sku") or "").strip() or None,
                "value": dims_raw,
                "context": (v.get("variant_note") or "").strip(),
                "width_cm": w, "depth_cm": d, "height_cm": h,
                "volume_m3": volume, "volume_source": source,
                "dims_confident": sure,
                "brand": brand, "model": model, "type_ru": type_ru,
            })

        if not (product.get("variants") or []):
            warnings.append(f"У «{model or 'позиции'}» не нашлось ни одного исполнения с габаритами.")

    if not candidates:
        warnings.append("Габаритов в документе не нашлось — заполните вручную.")
    return candidates, finishes, warnings
