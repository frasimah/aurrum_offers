# -*- coding: utf-8 -*-
"""
Разбор техлиста (PDF) через LlamaParse.

Зачем отдельно от Firecrawl: на техлистах с таблицами Firecrawl теряет
структуру — подписи и значения разъезжаются, и «длина провода» попадает
в габариты. LlamaParse возвращает таблицы markdown-таблицами, и размеры
читаются как есть.

Кандидаты в габариты ищем регуляркой, а не моделью: нотация «202x241x92H»
однозначна, а у одного изделия бывает несколько исполнений (кровать под
матрасы 165/185/200) — выбрать должен человек, а не угадывать программа.
"""

from __future__ import annotations

import io
import os
import re
import time

import httpx

import safe_fetch

API = "https://api.cloud.llamaindex.ai/api/v1/parsing"
POLL_INTERVAL = 3
POLL_ATTEMPTS = 40          # ~2 минуты
MAX_PDF_MB = 40


def _key() -> str:
    key = os.environ.get("LLAMA_CLOUD_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "Не задан LLAMA_CLOUD_API_KEY. Добавьте его в .env "
            "(образец — в .env.example)."
        )
    return key


def parse_pdf(url: str) -> str:
    """Скачивает PDF и возвращает его markdown от LlamaParse."""
    headers = {"Authorization": f"Bearer {_key()}"}

    # Адрес приходит от клиента — качаем с проверкой, куда он ведёт.
    data = safe_fetch.get(url, timeout=120).content

    with httpx.Client(timeout=120, follow_redirects=True) as http:
        if len(data) > MAX_PDF_MB * 1024 * 1024:
            raise RuntimeError(f"Документ больше {MAX_PDF_MB} МБ — не разбираю.")

        name = (url.split("?")[0].rsplit("/", 1)[-1] or "document") + ""
        if not name.lower().endswith(".pdf"):
            name += ".pdf"

        started = http.post(
            f"{API}/upload",
            headers=headers,
            files={"file": (name, io.BytesIO(data), "application/pdf")},
        )
        started.raise_for_status()
        job = started.json().get("id")
        if not job:
            raise RuntimeError("LlamaParse не вернул идентификатор задания.")

        for _ in range(POLL_ATTEMPTS):
            state = http.get(f"{API}/job/{job}", headers=headers).json()
            status = state.get("status")
            if status == "SUCCESS":
                break
            if status == "ERROR":
                raise RuntimeError(
                    "LlamaParse не смог разобрать документ: "
                    + str(state.get("error") or "причина не указана")
                )
            time.sleep(POLL_INTERVAL)
        else:
            raise RuntimeError("LlamaParse не ответил вовремя — попробуйте ещё раз.")

        result = http.get(f"{API}/job/{job}/result/markdown", headers=headers)
        result.raise_for_status()
        return str(result.json().get("markdown") or "")


# Габариты в техлистах: «202x241x92H.», «66x60x44Н», «D25/31x125Н»
_DIM_RE = re.compile(
    r"(?<![\d.,])"
    r"(?:[ØøD]\s*)?\d{1,4}(?:[./]\d{1,4})?"
    r"(?:\s*[xX×]\s*\d{1,4}(?:[./]\d{1,4})?){1,2}"
    r"\s*[HНh]?\.?"
    r"(?![\d.,])"
)


def find_dim_candidates(markdown: str, limit: int = 12) -> list[dict]:
    """Кандидаты в габариты со строкой-контекстом.

    У изделия бывает несколько исполнений, поэтому возвращаем список,
    а не одно значение: выбор за менеджером.
    """
    seen: dict[str, dict] = {}
    for line in markdown.splitlines():
        clean = line.strip().strip("|").strip()
        if not clean or len(clean) > 400:
            continue
        for m in _DIM_RE.finditer(clean):
            value = m.group(0).strip().rstrip(".")
            nums = [int(d) for d in re.findall(r"\d+", value)]
            # Отсекаем годы и артикулы, а заодно дюймовые дроби вида «2/3x95x36»:
            # габарит мебели в сантиметрах не бывает меньше десяти.
            if len(nums) < 3 or max(nums) > 2000 or min(nums) < 10:
                continue
            if value in seen:
                continue
            context = re.sub(r"\s*\|\s*", " · ", clean)
            seen[value] = {
                "value": value,
                "context": context[:160],
            }
            if len(seen) >= limit:
                return list(seen.values())
    return list(seen.values())
