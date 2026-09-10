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

# Шапка — ровно там, где её ищет обратный разбор (`spec_parser`), а он
# писан с живой книги `samples/2867_Спецификация_…`:
#
#   A5 «Покупатель»   H5 «Договор»   M5 «Дата»
#   A6  значение      H6  значение   M6  значение
#   A8 «Спецификация №… к Договору …»              N8 дата
#
# Строка 8 дублирует номер и договор нарочно: так сделано в книге, и
# разбор берёт их оттуда первым делом. Без этих ячеек напечатанный
# документ выходил с пустым номером и прочерками в покупателе и дате.
HEAD_LABEL_ROW = 5
HEAD_VALUE_ROW = 6
HEAD_TITLE_ROW = 8

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


def _photo(url: str, max_px: int = PHOTO_MAX_PX) -> XLImage | None:
    """Скачиваем и ужимаем снимок. Не вышло — молча пропускаем: файл
    без картинки лучше, чем отсутствие файла.

    `max_px` зависит от адресата: в ячейку Excel хватает 170, а печать
    показывает снимок втрое крупнее и из 170 получает мыло — ей 900,
    как и загруженным книгам (`spec_parser._to_data_uri`).
    """
    try:
        raw = safe_fetch.get(url, timeout=30,
                             headers={"User-Agent": "Mozilla/5.0"}).content
        from PIL import Image

        im = Image.open(io.BytesIO(raw))
        im.thumbnail((max_px, max_px), Image.LANCZOS)
        if im.mode != "RGB":
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82, optimize=True)
        buf.seek(0)
        return XLImage(buf)
    except Exception:            # noqa: BLE001 — картинка не критична
        return None


def _header(ws, head: dict) -> None:
    """Покупатель, договор, номер и дата — в ячейки разметки книги."""
    number = str(head.get("number") or "").strip()
    contract = str(head.get("contract") or "").strip()
    buyer = str(head.get("buyer") or "").strip()
    day = str(head.get("date") or "").strip()

    for column, label, value in (("A", "Покупатель", buyer),
                                 ("H", "Договор", contract),
                                 ("M", "Дата", day)):
        ws[f"{column}{HEAD_LABEL_ROW}"] = label
        ws[f"{column}{HEAD_LABEL_ROW}"].font = Font(size=9, color="949598")
        ws[f"{column}{HEAD_VALUE_ROW}"] = value

    # Строку заголовка собираем, только если есть номер: пустое
    # «Спецификация № к Договору» хуже, чем его отсутствие.
    if number:
        title = f"Спецификация №{number}"
        if contract:
            title += f" к Договору {contract}"
        ws[f"A{HEAD_TITLE_ROW}"] = title
        ws[f"A{HEAD_TITLE_ROW}"].font = Font(size=11, bold=True)
        ws[f"N{HEAD_TITLE_ROW}"] = day


def _geometric(position: dict) -> float:
    """Объём, который посчитала бы формула книги: ROUNDUP(Д×Г×В×1,5/1e6; 0,1)."""
    import math

    axes = [pricing._num(position.get(k)) for k in ("width_cm", "depth_cm", "height_cm")]
    if not all(axes):
        return 0.0
    exact = round(axes[0] * axes[1] * axes[2] * 1.5 / 1_000_000, 6)
    return math.ceil(exact * 10) / 10


def _write_position(ws, row: int, number: int, position: dict,
                    r: dict, values: bool) -> None:
    """Одна позиция в свою строку книги."""
    computed = pricing.for_position(position, r)
    fields = {**position, "number": number,
              # Количество — то же целое, что считает экран.
              "qty": pricing.quantity(position),
              # Ручной закуп первичен: в T уходит выведенная цена
              # прайса, и формулы книги воспроизводят тот же закуп.
              "list_price": (computed.list_price
                             if pricing._num(position.get("purchase")) > 0
                             else position.get("list_price")),
              # SWIFT в форме числом в каждой строке: своё значение
              # позиции выигрывает у общей ставки.
              "swift": position.get("swift") or r["swift"],
              # Цена клиенту — предложение расчёта; менеджер правит в файле.
              "price": position.get("price") or computed.price or ""}

    cells = book_row.visible_row(fields, row) + book_row.pricing_row(fields, row)
    # Объём. Книга выводит его формулой из габаритов, и это верно ровно
    # до тех пор, пока объём ИЗ НИХ и выведен. Объём от производителя,
    # из техлиста или правленный руками формула молча заменяла своим:
    # экран считал 4,5 м³ и перевозку 2250 €, файл — 3,1 м³ и 1550 €,
    # клиент получал цену на 700 € ниже названной. Такой объём уходит
    # числом, и вся цепочка книги (R, S, AC) считает от него.
    volume = pricing._num(position.get("volume_m3"))
    if volume and abs(volume - _geometric(position)) > 0.005:
        cells[20] = volume                                       # U  м3
    if values:
        qty = pricing.quantity(position)
        price = pricing._num(fields.get("price"))
        cells[5] = round(price * qty, 2) if price else ""      # F  Сумма
        cells[17] = volume or ""                                # R  м3
        cells[18] = round(volume * qty, 2) if volume else ""    # S  м3 всего
    for index, value in enumerate(cells, start=1):
        ws.cell(row, index, _number(value))

    ws.cell(row, 3).alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[row].height = PHOTO_ROW_HEIGHT

    photos = position.get("photos") or []
    if photos:
        image = _photo(photos[0], max_px=900 if values else PHOTO_MAX_PX)
        if image is not None:
            ws.add_image(image, f"I{row}")


def _blocks(positions: list[dict], rooms: list[str] | None) -> list[tuple[str, list[dict]]]:
    """Позиции -> блоки «комната, её позиции», в заданном порядке.

    Порядок берётся из списка комнат: он нужен ровно за двумя вещами —
    за пустой комнатой (в форме есть «Этаж 1» без позиций под ним) и за
    тем, чтобы менеджер расставлял их сам.

    Позиции без комнаты идут ПЕРВЫМИ, до всех заголовков. Иначе они
    неотличимы от позиций последней комнаты: метки «комната кончилась»
    в книге нет, и обратный разбор приписывал их к последнему заголовку.
    Заодно новая позиция, у которой комнаты ещё нет, видна сразу сверху.
    """
    order = [str(name).strip() for name in (rooms or []) if str(name).strip()]
    seen = list(dict.fromkeys(order))
    for position in positions:
        name = str(position.get("room") or "").strip()
        if name and name not in seen:
            seen.append(name)

    blocks = []
    loose = [p for p in positions if not str(p.get("room") or "").strip()]
    if loose:
        blocks.append(("", loose))
    blocks += [(name, [p for p in positions
                       if str(p.get("room") or "").strip() == name]) for name in seen]
    return blocks


def build(positions: list[dict], rates: dict | None = None,
          header: dict | None = None, final: dict | None = None,
          values: bool = False, rooms: list[str] | None = None) -> bytes:
    """Позиции проекта -> содержимое файла .xlsx.

    `values=True` — те же ячейки числами вместо формул: печать разбирает
    книгу тут же, а формулы в свежем файле ещё никем не вычислены, и
    разбор увидел бы пустоту. Числа считает тот же `pricing` — правда
    одна, отличается только форма записи.
    """
    r = {**pricing.DEFAULT_RATES, **(rates or {})}

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Лист1"

    # Ставки и подписи к ним. Без первой строки формулы позиций молчат.
    for column, (key, label) in RATE_CELLS.items():
        ws[f"{column}{RATES_ROW}"] = r[key]
        ws[f"{column}{RATES_ROW + 1}"] = label
        ws[f"{column}{RATES_ROW + 1}"].font = Font(size=8, color="949598")

    _header(ws, header or {})

    ws["A9"] = "Коммерческое предложение по решению интерьера"
    ws["A9"].font = Font(size=13, bold=True)

    for index, title in enumerate(HEADERS, start=1):
        cell = ws.cell(HEADER_ROW, index, title)
        cell.font = Font(size=9, bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="bottom")

    for column, width in COLUMN_WIDTHS.items():
        ws.column_dimensions[column].width = width

    # Строки идут подряд, но их номера больше не выводятся из индекса
    # позиции: между блоками вставляются заголовки комнат. Курсор бежит.
    row = FIRST_ITEM_ROW
    for room, chunk in _blocks(positions, rooms):
        if room:
            # Заголовок — только колонка C, как в рабочей форме: остальные
            # ячейки пустые, поэтому =SUM(F..) накрывает строку нулём.
            ws.cell(row, 3, room).font = Font(size=12, bold=True)
            ws.row_dimensions[row].height = 22
            row += 1
        # Нумерация начинается заново в каждой комнате — так в форме:
        # 1, 1, 1, 1, 2, а не сквозная.
        for number, position in enumerate(chunk, start=1):
            _write_position(ws, row, number, position, r, values)
            row += 1

    last = row - 1

    # Итоговый блок компреда — формулами, цепочка из спецификации 2867:
    # проценты вписаны в формулы числами, как делает сама книга
    # (=M17*0.05), поэтому файл пересчитывается в Excel без нас.
    totals_row = last + 2
    if positions:
        f = pricing.final_params(final)
        t = totals_row
        if values:
            # Тот же расчёт, что на экране: у каждой позиции своя цена —
            # ручная, иначе расчётная. Раньше сумма бралась по ручным
            # ценам, а позиции без ручной давали в неё ноль, хотя в своей
            # строке стояли с ценой: печать показывала строки на 14 500 и
            # «Сумму» 7000, итог выходил вдвое меньше экранного.
            totals = pricing.project(positions, r, f)
            items_sum = totals["sum"]
            fb = totals["final"]
            rows = [
                ("Сумма, Евро", round(items_sum, 2), True),
                ("Дополнительные услуги, Евро", fb["услуги"], False),
                ("Доставка по Москве/МО, Евро", fb["доставка"], False),
                ("Сборка/Монтаж, Евро", fb["сборка"], False),
                ("Всего, Евро", fb["всего"], True),
                ("Исключительная Персональная Скидка, Евро", fb["скидка"], False),
                ("Под-Итог, Евро", fb["подытог"], True),
                ("Дополнительная Скидка, Евро", fb["доп_скидка"], False),
                ("ИТОГО К ОПЛАТЕ, Евро", fb["к_оплате"], True),
            ]
        else:
            def part(key, base_cell, sign=""):
                # Ненулевое евро выигрывает у процента — как в pricing.
                if f[f"{key}_eur"]:
                    value = round(abs(f[f"{key}_eur"]), 2)
                    return -value if sign == "-" else value
                return f"={sign}{base_cell}*{f[f'{key}_pct'] / 100}"

            rows = [
                ("Сумма, Евро", f"=SUM(F{FIRST_ITEM_ROW}:F{last})", True),
                ("Дополнительные услуги, Евро", part("services", f"F{t}"), False),
                ("Доставка по Москве/МО, Евро", part("delivery", f"F{t}"), False),
                ("Сборка/Монтаж, Евро", part("assembly", f"F{t}"), False),
                ("Всего, Евро", f"=F{t}+F{t + 1}+F{t + 2}+F{t + 3}", True),
                ("Исключительная Персональная Скидка, Евро",
                 part("personal", f"F{t + 4}", sign="-"), False),
                ("Под-Итог, Евро", f"=F{t + 4}+F{t + 5}", True),
                ("Дополнительная Скидка, Евро", part("extra", f"F{t + 6}", sign="-"), False),
                ("ИТОГО К ОПЛАТЕ, Евро", f"=F{t + 6}+F{t + 7}", True),
            ]
        for offset, (label, value, strong) in enumerate(rows):
            row = t + offset
            ws.cell(row, 3, label).font = Font(bold=strong)
            cell = ws.cell(row, 6, _number(value))
            cell.font = Font(bold=strong)
        ws.cell(t, 19, f"=SUM(S{FIRST_ITEM_ROW}:S{last})")

    # Служебные колонки прячем, как в рабочей форме.
    for column in ("G", "R"):
        ws.column_dimensions[column].hidden = True
    for index in range(20, 36):
        ws.column_dimensions[get_column_letter(index)].hidden = True

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
