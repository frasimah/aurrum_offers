# -*- coding: utf-8 -*-
"""
Извлечение: текст страницы или PDF -> структура по нашей схеме.

Порядок попыток и почему он такой.

1. **Gemini** читает PDF страницей, вместе с чертежами. Это решающее
   свойство: у VENICEM размеры нарисованы на схеме, текстового слоя под
   ними нет вовсе — pypdf достаёт оттуда одни колонтитулы. Gemini вернул
   «125 cm x Ø 25 cm x 31 cm» и объём 0,23 м³, совпав с рабочей книгой.
   На табличном техлисте TRUSSARDI он дал те же пять исполнений
   с артикулами. И он быстрее: 3–9 секунд против 40–60.

2. **Текстовый слой + LlamaExtract** — если ключа Google нет. Работает
   там, где текст в документе есть; на TRUSSARDI даёт те же 5 из 5.

3. **Загрузка файла в LlamaCloud** — последняя попытка, для сканов без
   текста. Упирается в квоту (402), поэтому и оказалась последней.

Ответ модели здесь не считается истиной: оси и объём считает Python,
списки значений сверяются со своими, а строка размеров — с источником.
"""

from __future__ import annotations

import base64
import json
import os

import httpx

import llama_extract
import safe_fetch

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-3.7-flash"

MAX_PDF_MB = 40
# Меньше этого — считаем, что текстового слоя нет.
MIN_PDF_TEXT = 200
# Потолок текста в одном запросе — страницы брендов заметно короче.
MAX_TEXT = 200_000

# Поля перечислены прямо в запросе, а не JSON-схемой: схема извлечения
# использует nullable-типы, которые Gemini в responseSchema не принимает.
# Проверка значений всё равно наша, в product_lookup.
_ASK = (
    "Разбери документ по инструкции и верни JSON вида "
    '{"brand": …, "products": [{"model": …, "collection": …, "type_ru": …, '
    '"variants": [{"sku": …, "dims_raw": …, "variant_note": …, '
    '"packed_volume_m3": …}], '
    '"finishes": [{"role_ru": …, "material": …, "code": …}], '
    '"tech_note": …}]}. '
    "Неизвестное оставляй пустым."
)


def _gemini(data: bytes | None = None, text: str | None = None) -> dict:
    key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("нет GOOGLE_API_KEY")

    if data is not None:
        payload = {"inline_data": {"mime_type": "application/pdf",
                                   "data": base64.b64encode(data).decode()}}
    else:
        payload = {"text": (text or "")[:MAX_TEXT]}

    body = {
        "contents": [{"parts": [payload, {"text": _ASK}]}],
        "systemInstruction": {"parts": [
            {"text": llama_extract._read("extraction_prompt.txt")}
        ]},
        "generationConfig": {"responseMimeType": "application/json"},
    }
    got = httpx.post(f"{GEMINI_URL}/{GEMINI_MODEL}:generateContent",
                     params={"key": key}, json=body, timeout=300)
    got.raise_for_status()
    text = got.json()["candidates"][0]["content"]["parts"][0]["text"]
    parsed = json.loads(text)
    return parsed if isinstance(parsed, dict) else {}


def from_text(text: str) -> dict:
    """Разбор текста страницы.

    Очная ставка на пяти брендах: тип, число исполнений и габариты
    совпали с LlamaExtract везде, включая проверку на выдумку (у FENDI
    оба честно вернули ноль). Gemini при этом вдвое быстрее, а на HENGE
    втрое-вдевятеро: 3 секунды против 28.
    """
    text = (text or "").strip()
    if not text:
        return {}
    try:
        return _gemini(text=text)
    except Exception:            # noqa: BLE001 — есть чем заменить
        return llama_extract.from_text(text)


def from_url(url: str) -> dict:
    """Ссылка на техлист -> извлечённые данные."""
    got = safe_fetch.get(url, timeout=180, headers={"User-Agent": "Mozilla/5.0"})
    data = got.content
    if len(data) > MAX_PDF_MB * 1024 * 1024:
        raise RuntimeError(f"Документ больше {MAX_PDF_MB} МБ — не разбираю.")

    try:
        return _gemini(data=data)
    except Exception as gemini_failed:   # noqa: BLE001 — есть чем заменить
        reason = str(gemini_failed)[:120]

    text = llama_extract._pdf_text(data)
    if len(text.strip()) >= MIN_PDF_TEXT:
        return llama_extract.from_text(text)

    raise RuntimeError(
        f"Техлист не разобрать: чтение страницей не удалось ({reason}), "
        "а текстового слоя в документе нет."
    )
