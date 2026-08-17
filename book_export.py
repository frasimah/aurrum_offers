# -*- coding: utf-8 -*-
"""
Проект -> готовый файл Excel в разметке рабочей формы.

Закрывает последний ручной шаг: раньше строки переносились через буфер
по одной, а фотографии менеджер вставлял руками. Здесь позиции, формулы,
итоги и снимки уже на местах — файл открывают и работают дальше в Excel.

Ставки пишутся в первую строку (`Z1`, `AA1`, `AC1`, `AF1`…`AI1`): формулы
расчёта ссылаются именно на них, и без этих ячеек цена не посчитается.
Заодно они снимаются в файл — открытый через полгода проект покажет те
числа, что были при отправке, а не пересчитается по новым ставкам.

Фотография кладётся одна на позицию, первая из отмеченных: столько же
их в рабочей форме, и ответ не распухает — на serverless он ограничен.
"""

from __future__ import annotations

import io

import openpyxl
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

import book_row
import pricing
import safe_fetch

# Разметка формы: ставки в первой строке, заголовок таблицы в 13-й.
RATES_ROW = 1
HEADER_ROW = 13
FIRST_ITEM_ROW = HEADER_ROW + 1

# Ячейки ставок — те же, на которые ссылаются формулы позиции.
RATE_CELLS = {
    "Z": ("margin", "РЕНТАБ, %"),
    "AA": ("transfer", "ТРАНШ, %"),
    "AC": ("freight", "ТРАНСПОРТ, евро за м³"),
    "AF": ("designer", "ДИЗАЙНЕР, %"),
    "AG": ("usno", "УСНО, %"),
    "AH": ("vat", "НДС, %"),
    "AI": ("finserv", "FINSERV, %"),
}

HEADERS = [
    "№", "Производитель", "Описание", "К-во", "Цена, Евро", "Сумма, Евро",
    "СПЕЦ. ЦЕНА, Евро", "СПЕЦ. СУММА, Евро", "Фото / Схема",
    "Д, см", "Г, см", "В, см", "Отделка 1", "Отделка 2", "Отделка 3",
    "Примечание", "Схема", "м3", "м3 всего",
]

# Ширина колонки с фото и высота строки под снимок — в точках Excel.
PHOTO_COLUMN_WIDTH = 24
PHOTO_ROW_HEIGHT = 96
PHOTO_MAX_PX = 170

COLUMN_WIDTHS = {"A": 5, "B": 18, "C": 46, "D": 6, "E": 12, "F": 12,
                 "G": 14, "H": 14, "I": PHOTO_COLUMN_WIDTH,
                 "J": 8, "K": 8, "L": 8, "M": 12, "N": 12, "O": 12,
                 "P": 18, "Q": 10, "R": 8, "S": 8}


def _number(value):
    """Строку с числом пишем числом — иначе Excel не посчитает по ней."""
    if isinstance(value, (int, float)):
        return value
    text = str(value or "").strip().replace(",", ".")
    if not text or text.startswith("="):
        return value
    try:
        number = float(text)
    except ValueError:
        return value
    return int(number) if number.is_integer() else number


def _photo(url: str) -> XLImage | None:
    """Скачиваем и ужимаем снимок. Не вышло — молча пропускаем: файл
    без картинки лучше, чем отсутствие файла."""
    try:
        raw = safe_fetch.get(url, timeout=30,
                             headers={"User-Agent": "Mozilla/5.0"}).content
        from PIL import Image

        im = Image.open(io.BytesIO(raw))
        im.thumbnail((PHOTO_MAX_PX, PHOTO_MAX_PX), Image.LANCZOS)
        if im.mode != "RGB":
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82, optimize=True)
        buf.seek(0)
        return XLImage(buf)
    except Exception:            # noqa: BLE001 — картинка не критична
        return None


def build(positions: list[dict], rates: dict | None = None) -> bytes:
    """Позиции проекта -> содержимое файла .xlsx."""
    r = {**pricing.DEFAULT_RATES, **(rates or {})}

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Лист1"

    # Ставки и подписи к ним. Без первой строки формулы позиций молчат.
    for column, (key, label) in RATE_CELLS.items():
        ws[f"{column}{RATES_ROW}"] = r[key]
        ws[f"{column}{RATES_ROW + 1}"] = label
        ws[f"{column}{RATES_ROW + 1}"].font = Font(size=8, color="949598")

    ws["A9"] = "Коммерческое предложение по решению интерьера"
    ws["A9"].font = Font(size=13, bold=True)

    for index, title in enumerate(HEADERS, start=1):
        cell = ws.cell(HEADER_ROW, index, title)
        cell.font = Font(size=9, bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="bottom")

    for column, width in COLUMN_WIDTHS.items():
        ws.column_dimensions[column].width = width

    for offset, position in enumerate(positions):
        row = FIRST_ITEM_ROW + offset
        computed = pricing.line(
            position.get("list_price"), position.get("volume_m3"),
            factory_discount=position.get("factory_discount"),
            dealer_markup=position.get("dealer_markup"),
            assembly=position.get("assembly"), rates=r,
        )
        fields = {**position, "number": offset + 1, "swift": r["swift"],
                  # Цена клиенту — предложение расчёта; менеджер правит в файле.
                  "price": position.get("price") or computed.price or ""}

        cells = book_row.visible_row(fields, row) + book_row.pricing_row(fields, row)
        for index, value in enumerate(cells, start=1):
            ws.cell(row, index, _number(value))

        ws.cell(row, 3).alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[row].height = PHOTO_ROW_HEIGHT

        photos = position.get("photos") or []
        if photos:
            image = _photo(photos[0])
            if image is not None:
                ws.add_image(image, f"I{row}")

    # Итоги — теми же формулами, что в форме.
    last = FIRST_ITEM_ROW + max(0, len(positions)) - 1
    totals_row = last + 2
    if positions:
        ws.cell(totals_row, 5, "Сумма, Евро").font = Font(bold=True)
        ws.cell(totals_row, 6, f"=SUM(F{FIRST_ITEM_ROW}:F{last})").font = Font(bold=True)
        ws.cell(totals_row, 19, f"=SUM(S{FIRST_ITEM_ROW}:S{last})")

    # Служебные колонки прячем, как в рабочей форме.
    for column in ("G", "R"):
        ws.column_dimensions[column].hidden = True
    for index in range(20, 36):
        ws.column_dimensions[get_column_letter(index)].hidden = True

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
