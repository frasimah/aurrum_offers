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

# Две модели на две разные задачи — разделение снято с замеров, а не
# выбрано на вкус.
#
# Тяжёлая читает документы: у VENICEM размеры стоят выносками на чертеже,
# и lite там теряет глубину — «Ø 25 x 125» вместо «125, Ø 25, 31».
# Ошибка молчаливая и уезжает в объём, то есть в счёт за перевозку.
#
# Лёгкая разбирает текст страницы: на восьми брендах она дала тот же тип
# и те же габариты, а тип даже чище (тяжёлая навешивает уточнения вроде
# «Стол обеденный», которые мы всё равно срезаем). Работает вдвое-втрое
# быстрее. Её слабость известна: иногда недочитывает отделки и исполнения —
# поэтому пустой ответ переспрашивается у тяжёлой.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-3.7-flash"
GEMINI_MODEL_LIGHT = (os.environ.get("GEMINI_MODEL_LIGHT", "").strip()
                      or "gemini-3.5-flash-lite")

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
    # package_dims_raw читает product_lookup (p.package_note) и печатает
    # карточка. В запросе его не было, поэтому от Gemini оно не приходило
    # никогда — поле жило только на запасном пути, у которого схема целиком.
    '"packed_volume_m3": …, "package_dims_raw": …}], '
    '"finishes": [{"role_ru": …, "material": …, "code": …}], '
    '"summary_ru": …, "tech_note": …}]}. '
    "Неизвестное оставляй пустым."
)


def _gemini(data: bytes | None = None, text: str | None = None,
            model: str | None = None) -> dict:
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
    # Ключ заголовком, а не в адресе: httpx пишет адрес запроса в лог
    # целиком, и на проде ключ Google лежал открытым в журнале Vercel.
    got = httpx.post(f"{GEMINI_URL}/{model or GEMINI_MODEL}:generateContent",
                     headers={"x-goog-api-key": key}, json=body, timeout=300)
    got.raise_for_status()
    answer = got.json()
    try:
        text = answer["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        # Ответ может кончиться не текстом: блокировка, обрыв по длине,
        # пустой candidates. Раньше это падало KeyError с нечитаемым
        # диагнозом — теперь причина называется словами.
        reason = ((answer.get("promptFeedback") or {}).get("blockReason")
                  or ((answer.get("candidates") or [{}])[0] or {}).get("finishReason")
                  or "ответ без текста")
        raise RuntimeError(f"Gemini не вернул разбор: {reason}") from None
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        # Раньше не-словарь молча превращался в {}. Отказом это не
        # считалось: пустой ответ уезжал дальше с ярлыком «Gemini»,
        # проверка в product_lookup молчала, а запасной путь даже
        # не пробовался.
        raise RuntimeError(
            f"Gemini вернул не объект, а {type(parsed).__name__} — "
            "разбор не состоялся.")
    return parsed


# Каким путём собраны данные. Ключ кладётся в ответ, потому что запасной
# путь читает только текст: на VENICEM, где размеры нарисованы на схеме,
# он вернёт пустые габариты — и без этой пометки менеджер решит, что их
# нет на сайте, вместо того чтобы посмотреть глазами.
SOURCE_KEY = "_извлекатель"
SOURCE_MAIN = "Gemini"


def _thin(answer: dict, known_types: tuple | list = ()) -> bool:
    """Стоит ли переспросить ответ у тяжёлой модели.

    Два случая, оба замерены на живых страницах. Пусто: у LONGHI и
    EMMEMOBILI лёгкая возвращала ноль исполнений там, где тяжёлая их
    находила. Ответ через слэш: «Скамья/Пуф» — это модель сама не
    выбрала, и тип уезжал в «Пуф», хотя в книге стоит «Банкетка».
    """
    products = (answer or {}).get("products") or []
    if not products:
        return True
    first = products[0] if isinstance(products[0], dict) else {}
    type_ru = str(first.get("type_ru") or "").strip()
    if "/" in type_ru:
        return True
    # Тип вне нашего списка — повод переспросить, а не молча уронить его
    # в «Другое». Ответ модели нестабилен от прогона к прогону: на одной
    # и той же странице VENICEM приходило то «Торшер», то мимо списка.
    if known_types and type_ru and type_ru not in known_types:
        return True
    return not (first.get("variants") or first.get("finishes"))


def from_text(text: str, known_types: tuple | list = ()) -> dict:
    """Разбор текста страницы.

    Очная ставка на пяти брендах: тип, число исполнений и габариты
    совпали с LlamaExtract везде, включая проверку на выдумку (у FENDI
    оба честно вернули ноль). Gemini при этом вдвое быстрее, а на HENGE
    втрое-вдевятеро: 3 секунды против 28.
    """
    text = (text or "").strip()
    if not text:
        return {}

    # Три ступени, а не две. Отказ лёгкой — тоже повод переспросить у
    # тяжёлой, а не сразу уходить к другому поставщику: раньше любой её
    # сбой (в том числе мусор в ответе) выбрасывал готовую половину
    # работы и уводил на запасной путь, который читает только текст.
    why_heavy = ""
    try:
        got = _gemini(text=text, model=GEMINI_MODEL_LIGHT)
        if not _thin(got, known_types):
            got[SOURCE_KEY] = SOURCE_MAIN
            return got
        # У LONGHI и EMMEMOBILI лёгкая возвращала ноль исполнений там,
        # где тяжёлая их находила.
        why_heavy = "ответ лёгкой неполон"
    except Exception as light_failed:    # noqa: BLE001 — есть чем заменить
        why_heavy = f"лёгкая не ответила: {str(light_failed)[:60]}"

    try:
        got = _gemini(text=text)
        got[SOURCE_KEY] = f"{SOURCE_MAIN} (переспрошено, {why_heavy})"
        return got
    except Exception as gemini_failed:   # noqa: BLE001 — есть чем заменить
        got = llama_extract.from_text(text)
        got[SOURCE_KEY] = f"LlamaExtract ({str(gemini_failed)[:100]})"
        return got


def from_url(url: str) -> dict:
    """Ссылка на техлист -> извлечённые данные."""
    got = safe_fetch.get(url, timeout=180, headers={"User-Agent": "Mozilla/5.0"})
    data = got.content
    if len(data) > MAX_PDF_MB * 1024 * 1024:
        raise RuntimeError(f"Документ больше {MAX_PDF_MB} МБ — не разбираю.")

    try:
        got = _gemini(data=data)
        got[SOURCE_KEY] = SOURCE_MAIN
        return got
    except Exception as gemini_failed:   # noqa: BLE001 — есть чем заменить
        reason = str(gemini_failed)[:120]

    text = llama_extract._pdf_text(data)
    if len(text.strip()) >= MIN_PDF_TEXT:
        got = llama_extract.from_text(text)
        got[SOURCE_KEY] = f"LlamaExtract по текстовому слою ({reason[:100]})"
        return got

    raise RuntimeError(
        f"Техлист не разобрать: чтение страницей не удалось ({reason}), "
        "а текстового слоя в документе нет."
    )
