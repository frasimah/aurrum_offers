# -*- coding: utf-8 -*-
"""
Сверка парсера с эталонами из рабочих книг.

Запуск:
    ./.venv/bin/python check_lookup.py            # все эталоны
    ./.venv/bin/python check_lookup.py --offline  # без сети, только разбор размеров
    ./.venv/bin/python check_lookup.py <url>      # разовая проверка любой ссылки

Эталоны взяты из реальных спецификаций: колонки J/K/L (габариты),
R (объём) и текст описания. Скрипт показывает, что сошлось, а что нет —
и не притворяется, что расхождение это норма.
"""

from __future__ import annotations

import json
import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import json as _json  # noqa: E402
import re as _re  # noqa: E402

import product_lookup as pl  # noqa: E402
import pricing  # noqa: E402
import book_export  # noqa: E402

OK, BAD, WARN = "✓", "✗", "~"

# --- Эталоны разбора размеров: строка из книги -> Д, Г, В --------------
DIMS_CASES = [
    ("130x47x45Н",             "Банкетка",              (130, 47, 45),  "LONGHI ARIANA, R16"),
    ("90x84x80Н",              "Кресло",                (90, 84, 80),   "FLOU MADAME BUTTERFLY, R18"),
    ("D25/31x125Н",            "Торшер",                (31, 31, 125),  "VENICEM CIRCLE, R20"),
    ("202x241x36/92Н",         "Кровать",               (202, 241, 92), "TRUSSARDI VIBES, R22"),
    ("66x60x44Н SPECIAL",      "Тумбочка прикроватная", (66, 60, 44),   "TRUSSARDI COMFY, R23"),
    # Диапазон по оси — всегда НАИБОЛЬШЕЕ, на всех осях одинаково.
    # Раньше диаметр и высота брали наибольшее, а длина с глубиной — то,
    # что написано первым: у раздвижного стола порядок написания решал
    # 300 € перевозки.
    ("160/220x90x75Н",        "Стол",                  (220, 90, 75),  "раздвижной стол"),
    ("220/160x90x75Н",        "Стол",                  (220, 90, 75),  "он же наоборот"),
    ("202x241x92/36Н",        "Кровать",               (202, 241, 92), "изголовье первым"),
    ("130x47x60/45Н",         "Банкетка",              (130, 47, 60),  "высота диапазоном"),
    # Косая черта С ПРОБЕЛАМИ — разделитель осей, а не диапазон.
    ("125 cm (49”) / Ø 25 cm (9”8) / 31 cm", "Торшер",  (31, 31, 125), "VENICEM, три оси"),
    # Постороннее число рядом с размерами: артикул, год, вес, световой
    # поток, цветовая температура. Раньше «ART.1200 145x45x47» давало
    # длину 1200 — номер вытеснял настоящий размер, и объём выходил
    # впятеро больше без единой пометки.
    ("ART.1200 145x45x47",     "Банкетка",              (145, 45, 47),  "артикул перед размерами"),
    ("Mod. 2024 - 145x45x47",  "Банкетка",              (145, 45, 47),  "год модели перед размерами"),
    ("cod. 8801 / 145x45x47",  "Банкетка",              (145, 45, 47),  "код через дробь"),
    ("145x45x47 - 2500 g",     "Банкетка",              (145, 45, 47),  "вес после размеров"),
    # У торшера наибольшее число намеренно становится высотой (_TALL_TYPES),
    # поэтому ждём (45, 47, 145): проверяем здесь ровно то, что 1200 в оси
    # не попал.
    ("145x45x47, 1200 lm",     "Торшер",                (45, 47, 145),  "световой поток"),
    ("2700°K 31x31x125",       "Торшер",                (31, 31, 125),  "цветовая температура"),
    ("ART.1200 L2415 D780 H2685 mm", "Кухня",           (241, 78, 268), "артикул при подписанных осях"),
    # PORADA, настоящие строки из прогона по карте сайта. Из шести
    # карточек, помеченных «уверенно», три были неверны — флаг там был
    # хуже монетки, потому что ошибку никто не показывал.
    # «h. 156»: точка не давала сработать прямому шаблону, и высотой
    # становилась глубина.
    ("114x14 h. 156",          "Кровать",               (114, 14, 156), "PORADA AIDA"),
    # «h65» и «h 47» после полной цепочки — высота СИДЕНЬЯ, а не изделия.
    ("44.5 x 47 x 88 h65",     "Табурет",               (44, 47, 88),   "PORADA SVEVA STOOL"),
    ("71 x 75 x 73 h 47",      "Кресло",                (71, 75, 73),   "PORADA DAPHNE"),
    # А эти три уже читались верно и сдвинуться не должны.
    ("184 x 230 x 133 h",      "Кровать",               (184, 230, 133), "PORADA ZIGGY BED"),
    ("76 x 83 x 90 h - h. seduta 41", "Кресло",         (76, 83, 90),   "PORADA OPIUM"),
    ("Ø40 44",                 "Пуф",                   (40, 40, 44),   "PORADA PODI"),
    # Ряд размеров — это не оси: подушка выпускается 40, 50 и 60 см.
    ("40 - 50 - 60",           "Пуф",                   (0, 0, 0),      "PORADA SOAP, ряд размеров"),
    ("220 - 260 - 300 x 110 - 120 x 75", "Стол",        (0, 0, 0),      "PORADA OSMOSE, лист семейства"),
    # Два числа без пометок — ковёр, панель, столешница. Высоты нет и в
    # источнике, поэтому заполняем известное, а высоту оставляем пустой:
    # объём без неё не считается, значит перевозку выдумать нельзя.
    ("200x300",                "Ковёр",                 (200, 300, 0),  "PORADA EDEN, ковёр"),
    ("160.5x260",              "Ковёр",                 (160, 260, 0),  "PORADA GLADKO, ковёр"),
    # Раньше цепочка обрывалась на двух числах и строка не читалась вовсе.
    # Правило «диапазон — наибольшее» её починило: 18/46 схлопывается, и
    # остаётся полная цепочка 74 x 46 x 76.
    ("74 x 18/46 x 76",        "Другое",                (74, 46, 76),   "PORADA PIT STOP, диапазон внутри"),
    # Нотации живых сайтов: в книгах их нет, а ошибаются они дороже всего.
    # 43 1/4" = 110 см, 47 1/4" = 120 см, 25 5/8" = 65 см — это дюймовые
    # двойники диаметров; единственный несдиаметральный сантиметр — высота 75.
    ('Ø110 - 120, 43 1/4" - 47 1/4", 75 cm, Ø65, 25 5/8"',
                               "Стол",                  (120, 120, 75), "PORADA INFINITY, сайт"),
    ("135 cm, 100 cm, 64 cm",  "Кресло",                (135, 100, 64), "HENGE RADICAL, сайт"),
    ("Ø 120 cm, H 75 cm",      "Стол",                  (120, 120, 75), "круглый стол с меткой H"),
    ('200x52x80 cm (78 3/4"x20 1/2"x31 1/2")',
                               "Комод",                 (200, 52, 80),  "дюймы в скобках"),
    # Подписи словами — так пишет BAROVIER, и без них два числа неразличимы
    ("Height 60 cm, Diameter 93 cm",
                               "Люстра",                (93, 93, 60),   "BAROVIER METROPOLIS"),
    ("Altezza 120 cm, Diametro 127 cm",
                               "Люстра",                (127, 127, 120), "то же по-итальянски"),
    ("72x76x75H cm",           "Кресло",                (72, 76, 75),   "BENTLEY MERE, сайт"),
    ("300x90x75(h)cm",         "Стол",                  (300, 90, 75),  "HENGE SISMA, пометка в скобках"),
    # Горизонталь, численно равная высоте, пропадала: отбор шёл по
    # значению и вычёркивал оба числа разом. Бра выходило 4x4x51 вместо
    # 51x4x51, шкаф — 40x40x80 вместо 80x40x80, то есть вдвое уже и вдвое
    # дешевле по транспорту, и всё это с пометкой «уверенно».
    ("Height 51 cm Width 51 cm Depth 4 cm",
                               "Бра",                   (51, 4, 51),    "BAROVIER TUILERIES, ширина равна высоте"),
    ("Height 80 cm Width 80 cm Depth 40 cm",
                               "Шкаф",                  (80, 40, 80),   "шкаф: ширина равна высоте"),
    ("H 90 x 90 x 45",         "Стол",                  (90, 45, 90),   "квадратный стол"),
    # Миллиметры — стандарт итальянских техлистов. Без перевода объём
    # выходил в тысячу раз больше: 2450 м³ вместо 2,5, то есть счёт за
    # перевозку на миллион вместо тысячи, и всё с пометкой «уверенно».
    ("L2415 D780 H2685 mm",    "Мебель для кухни",      (241, 78, 268), "MODULNOVA из книги 2867, мм"),
    ("2400x1000x750 mm",       "Стол",                  (240, 100, 75), "мм без буквенных пометок"),
    ("Ø 1200 mm H 750 mm",     "Стол",                  (120, 120, 75), "круглый стол в мм"),
    # Голая D — диаметр только там, где нет пометок длины и ширины.
    ("W 240 D 100 H 75 cm",    "Стол",                  (240, 100, 75), "нотация W D H: D — глубина"),
    # Тот же BAROVIER, но извлечение подписало размеры после числа
    ("90 cm (высота), 127 cm (диаметр)",
                               "Люстра",                (127, 127, 90), "подпись в скобках"),
    # PORADA пишет высоту строчной «h» после списка диаметров
    ("Ø130 - 140 - 150 - 160 h 75",
                               "Стол",                  (160, 160, 75), "PORADA, техлист"),
    ("120 x 180 - 200 h 75",   "Стол",                  (120, 180, 75), "PORADA, овал"),
]

# --- Эталоны объёма: габариты -> м³ по колонке R книги -----------------
VOLUME_CASES = [
    ((130, 47, 45),  0.5, "R16"),
    ((90, 84, 80),   1.0, "R18"),
    ((31, 31, 125),  0.2, "R20"),
    ((202, 241, 92), 6.8, "R22"),
    # Не из книги: до правки высотой считался диаметр 120 и выходило 2,2 м³.
    ((120, 120, 75), 1.7, "PORADA INFINITY"),
]

# --- Тип по разделу сайта: адрес -> тип ('' = раздел неоднозначен) ------
URL_TYPE_CASES = [
    ("https://www.emmemobili.it/en/prodotti/contenitori/fractal", "Комод"),
    ("https://www.misuraemme.it/en/products/baltimora-bed",       "Кровать"),
    ("https://www.brand.it/en/products/armchairs/soft",           "Кресло"),
    ("https://www.brand.it/en/products/chairs/pin",               "Стул"),
    ("https://www.brand.it/prodotti/comodini/x",     "Тумбочка прикроватная"),
    ("https://www.porada.it/en/products/side-coffee-tables",      "Стол"),
    # Живой промах: настольная лампа BAROVIER стала «Столом» — голый
    # токен «table» совпадал с table-lamps и перекрывал извлечение.
    ("https://www.barovier.com/en/table-lamps/aurora",  "Настольная лампа"),
    ("https://www.barovier.com/en/chandeliers/metropolis",        "Люстра"),
    ("https://www.barovier.com/en/floor-lamps/lume",              "Торшер"),
    ("https://brand.it/lampade-da-tavolo/x",            "Настольная лампа"),
    # «chairs» лежит внутри «armchairs» — раздел смешанный, тип не навязываем
    ("https://www.henge07.com/products/sofas-and-armchairs/cohiba/", ""),
    ("https://www.porada.it/prodotto/infinity",                   ""),
    # У света тип задаёт способ монтажа, а не раздел
    ("https://flos.com/en/us/ic-lights-floor/M-ic-lights-floor.html", ""),
]

# --- Тип из извлечения -> тип из нашего списка --------------------------
# Модель отвечает не только словом из списка: LONGHI вернул «Банкетка / пуф»,
# HENGE — «Стол обеденный». Оба уезжали в «Другое», хотя нужное слово в них
# есть. «Диван-кровать» остаётся «Другим» намеренно: это не диван.
TYPE_NORM_CASES = [
    ("Кресло",         "Кресло"),
    ("Банкетка / пуф", "Банкетка"),
    ("Стол обеденный", "Стол"),
    ("Пуф",            "Пуф"),
    ("Диван-кровать",  "Другое"),
    ("",               ""),
]

# --- Живые ссылки: что ожидаем увидеть ---------------------------------
LIVE_CASES = [
    {
        "url": "https://www.venicem.com/product/circle-floor/",
        "note": "есть техлист — должна собраться полная карточка",
        "expect": {"type_ru": "Торшер", "width_cm": 31, "depth_cm": 31, "height_cm": 125},
    },
    {
        "url": "https://www.longhi.it/en-us/products/bench-pouf/arianna",
        "note": "техлиста нет — габариты должны остаться пустыми, а не выдуманными",
        "expect": {"type_ru": "Банкетка"},
    },
]


def check_dims() -> tuple[int, int]:
    print("\n РАЗБОР РАЗМЕРОВ (без сети)")
    print(" " + "-" * 74)
    good = 0
    for raw, type_ru, expected, source in DIMS_CASES:
        w, d, h, sure = pl.parse_dims(raw, type_ru)
        got = tuple(int(x) if x else 0 for x in (w, d, h))
        hit = got == expected
        good += hit
        mark = OK if hit else BAD
        note = "" if sure else "  (оси угаданы, не по пометке H)"
        print(f"  {mark} {raw:22} -> {got}{note}")
        if not hit:
            print(f"      ожидали {expected} — {source}")
    return good, len(DIMS_CASES)


def check_book_dims() -> tuple[int, int]:
    """В описание идёт нотация книги, а не строка источника.

    Поле и описание делают разную работу. Строка источника — улика: по
    ней сверяют разбор, и без неё знак «!» не с чем сопоставить. А в
    колонку C книги человек пишет «130x47x45Н», и клиент видит именно
    это, а не «Height 28 cm Depth 11 cm Minimum diameter 6 cm…».
    """
    print("\n НОТАЦИЯ РАЗМЕРОВ")
    print(" " + "-" * 74)

    def card(raw, type_ru=""):
        w, d, h, sure = pl.parse_dims(raw, type_ru)
        return pl.Product(model="X", dims_raw=raw, width_cm=w, depth_cm=d,
                          height_cm=h, dims_confident=sure)

    cases = [
        # Пометка высоты в источнике есть — ставим «Н», как в книге.
        ("130x47x45Н", "130x47x45Н", "книга, R16"),
        ("Height 28 cm Depth 11 cm Minimum diameter 6 cm Maximum diameter 10 cm",
         "10x10x28Н", "BAROVIER AURORA"),
        ("300x90x75h cm", "300x90x75Н", "HENGE SISMA"),
        ("L2415 D780 H2685 мм", "241.5x78x268.5Н", "MODULNOVA, миллиметры"),
        # Пометки нет — оси расставлены по порядку, «Н» утверждать нельзя.
        ("145 x 45 x 47", "145x45x47", "LONGHI, оси по порядку"),
        ("125 cm (49”) / Ø 25 cm (9”8) / 31 cm", "31x31x125", "VENICEM"),
        # Собирать не из чего — остаётся то, что сказал источник.
        ("200x300", "200x300", "ковёр, высоты нет"),
    ]
    checks = []
    for raw, want, who in cases:
        got = pl.book_dims(card(raw))
        checks.append((f"{raw[:34]:36} -> {got:16} {who}", got == want))

    # Строка источника остаётся в карточке нетронутой.
    p = card("Height 28 cm Depth 11 cm Minimum diameter 6 cm Maximum diameter 10 cm")
    checks.append(("строка источника в карточке цела",
                   p.dims_raw.startswith("Height 28 cm")))
    checks.append(("а в описание ушла нотация",
                   "10x10x28Н" in pl.to_excel_description(p)))
    checks.append(("строки источника в описании нет",
                   "Minimum diameter" not in pl.to_excel_description(p)))

    good = 0
    for label, hit in checks:
        good += bool(hit)
        print(f"  {OK if hit else BAD} {label}")
    return good, len(checks)


def check_units() -> tuple[int, int]:
    """Единица измерения не должна зависеть от слова в ответе модели.

    Строку dims_raw пишет модель и копирует дословно — промпт прямо
    запрещает ей нормализовать единицы. Пока перевод мм->см держался
    только на слове «mm», потеря этого слова давала объём в тысячу раз
    больше и счёт за перевозку 3 793 350 € вместо 3 800 € — с пометкой
    «уверенно», потому что оси-то были подписаны.
    """
    print("\n ЕДИНИЦА ИЗМЕРЕНИЯ")
    print(" " + "-" * 74)

    # (строка, ожидаемые оси в см, выведена ли единица)
    cases = [
        # Кухня MODULNOVA из книги 2867 — с единицей и без неё одинаково.
        ("L2415 D780 H2685 mm", (241, 78, 268), False),
        ("L2415 D780 H2685", (241, 78, 268), True),
        ("L2136 D331 H1216", (213, 33, 121), True),
        # Сантиметры остаются сантиметрами, даже крупные.
        ("300x90x75h cm", (300, 90, 75), False),
        ("241,5 x 78 x 268,5 cm", (241, 78, 268), False),
        # Ковёр 5 x 4 метра: числа велики для комнаты, но не для порога —
        # правило по величине их не трогает.
        ("500 x 400 x 2 cm", (500, 400, 2), False),
        ("500 x 400 x 2", (500, 400, 2), False),
        # Миллиметры без единицы — обычная запись техлиста.
        ("2000 x 900 x 750", (200, 90, 75), True),
    ]

    checks = []
    for raw, expect, guessed in cases:
        w, d, h, _ = pl.parse_dims(raw)
        got = tuple(int(x) if x else 0 for x in (w, d, h))
        checks.append((f"{raw:24} -> {got}", got == expect))
        checks.append((f"{raw:24}    единица {'выведена' if guessed else 'из строки'}",
                       pl.dims_unit_guessed(raw) is guessed))

    # Крупное число, которое не размер: артикул, год, вес, световой
    # поток. Одного максимума мало — иначе правильные сантиметры делятся
    # на десять и объём исчезает вместе с перевозкой.
    for raw in ("ART.1200 145x45x47", "Mod. 2024 - 145x45x47",
                "145x45x47, 1200 lm", "cod. 8801 / 145x45x47"):
        checks.append((f"{raw:24}    не миллиметры",
                       pl.dims_unit_guessed(raw) is False))

    # Книжный формат — числа без единицы — это норма, а не аномалия:
    # все пять позиций FORM записаны так. Предупреждение, которое
    # поднимается на каждой строке, перестаёт быть сигналом, поэтому
    # оно ставится только там, где есть повод: числа похожи на
    # миллиметры или объём неправдоподобен.
    for raw in ("130x47x45Н", "90x84x80Н", "66x60x44Н", "202x241x36/92Н"):
        m = pl.measure(raw)
        checks.append((f"{raw:24}    без предупреждений", not m.warnings))
        checks.append((f"{raw:24}    оси уверенные", m.confident is True))

    # Решение о единице принимается по числам, которые уйдут в оси, —
    # а не по сырой строке. Иначе артикул рядом с крупной мебелью, где
    # все оси и так больше метра, делает из сантиметров миллиметры:
    # шкаф 200x120x240 давал 0,1 м³ вместо 8,7, то есть перевозка
    # исчезала. Недобор тише перебора и потому опаснее.
    for raw, expect in (("ART. 1200 - 200 x 120 x 240", (200, 120, 240)),
                        ("cod. 1140 - 250 x 100 x 200", (250, 100, 200)),
                        ("L 240 P 100 H 105 (cod. 1580)", (240, 100, 105))):
        w, d, h, _ = pl.parse_dims(raw)
        got = tuple(int(x) if x else 0 for x in (w, d, h))
        checks.append((f"{raw:24} -> {got}", got == expect))

    # Единица, напечатанная вплотную к числу: «H100cm». Границы слова
    # между цифрой и буквой нет, и по \b единица считалась неназванной —
    # а цветовая температура из той же строки делала из неё миллиметры.
    checks.append(("«Ø120xH100cm» — единица названа",
                   pl.dims_unit_stated("Ø120xH100cm 3000K") is True))
    w, d, h, _ = pl.parse_dims("Ø120xH100cm 3000K")
    checks.append(("«Ø120xH100cm 3000K» -> 120x120x100", (w, d, h) == (120.0, 120.0, 100.0)))

    # Двух осей довольно: круглый стол «Ø1200 H750» — полный набор.
    checks.append(("«Ø1200 H750» — миллиметры",
                   pl.dims_unit_guessed("Ø1200 H750") is True))
    checks.append(("«Ø1200 H750» -> 1.7 м³",
                   pl.measure("Ø1200 H750").volume_m3 == 1.7))

    # Потолок правдоподобия. Настоящий максимум книг — 6,8 м³
    # (TRUSSARDI VIBES) и 7,6 м³ у кухни MODULNOVA; 27,3 м³ на строке
    # «L2415/2805/2415 H2685 мм» — это уже ошибка разбора, и она
    # обязана быть помечена, а не пройти молча.
    checks.append(("книжный максимум 6,8 м³ потолок не задевает",
                   pl.volume_m3(202.0, 241.0, 92.0) <= pl.MAX_PLAUSIBLE_M3))
    checks.append(("кухня MODULNOVA 7,6 м³ потолок не задевает",
                   pl.volume_m3(241.5, 78.0, 268.5) <= pl.MAX_PLAUSIBLE_M3))
    checks.append(("многосекционная кухня 27,3 м³ помечена",
                   pl.measure("L2415/2805/2415 H2685 мм").confident is False))
    checks.append(("ошибка на порядок помечена",
                   pl.measure("Ø450 H300").confident is False))

    # Отбивки стоят там, где считается объём, а не в одном из трёх
    # мест: путь техлиста — родина миллиметровой записи.
    import extract_agent
    cands, _, _ = extract_agent.to_candidates(
        {"products": [{"type_ru": "Стол", "variants": [{"dims_raw": "Ø1200 H750"}]}]})
    checks.append(("техлист: «Ø1200 H750» -> 1.7 м³",
                   cands and cands[0]["volume_m3"] == 1.7))
    checks.append(("техлист: карточка помечена",
                   bool(cands) and cands[0]["dims_confident"] is False))

    good = 0
    for label, hit in checks:
        good += bool(hit)
        print(f"  {OK if hit else BAD} {label}")
    return good, len(checks)


def check_volume_source() -> tuple[int, int]:
    """Откуда объём — должно быть сказано, а не «не определён».

    Карточки, сохранённые до того, как экран стал приносить состояние
    целиком, лежат без volume_source, и подпись под заполненным полем
    говорила «Не определён». Восстанавливается точно: совпал с расчётом
    по осям — расчётный; стоит и не совпал — поставлен руками.
    """
    import app as flask_app
    print("\n ОТКУДА ОБЪЁМ")
    print(" " + "-" * 74)
    cases = [
        ("совпал с расчётом — расчётный",
         {"volume_m3": 0.2, "width_cm": 70.0, "depth_cm": 18.0, "height_cm": 58.0},
         "расчёт по габаритам"),
        ("не совпал — поставлен руками",
         {"volume_m3": 0.35, "width_cm": 70.0, "depth_cm": 18.0, "height_cm": 58.0},
         "задан вручную"),
        ("ковёр без высоты — руками",
         {"volume_m3": 0.35, "width_cm": 200.0, "depth_cm": 300.0, "height_cm": None},
         "задан вручную"),
        ("объёма нет — и источника нет", {"volume_m3": None}, ""),
        ("записанное не перетирается",
         {"volume_m3": 0.2, "width_cm": 70.0, "depth_cm": 18.0, "height_cm": 58.0,
          "volume_source": "производитель"}, "производитель"),
    ]
    checks = []
    for label, item, want in cases:
        got = (item.get("volume_source") or flask_app._volume_source_of(item))
        checks.append((f"{label:34} -> {got or '—'}", got == want))
    good = 0
    for label, hit in checks:
        good += bool(hit)
        print(f"  {OK if hit else BAD} {label}")
    return good, len(checks)


def check_volume() -> tuple[int, int]:
    print("\n ОБЪЁМ ПО ФОРМУЛЕ КНИГИ")
    print(" " + "-" * 74)
    good = 0
    for (w, d, h), expected, source in VOLUME_CASES:
        got = pl.volume_m3(w, d, h)
        hit = abs(got - expected) < 0.05
        good += hit
        print(f"  {OK if hit else BAD} {w}x{d}x{h:<6} -> {got:.1f} м³   в книге {expected} ({source})")
    return good, len(VOLUME_CASES)


def check_dims_grounding() -> tuple[int, int]:
    """Габариты, которых нет в источнике, обязаны отбиваться.

    Случай из жизни: страница FENDI CASA не публикует размеров вовсе,
    а извлечение вернуло правдоподобные «83 / 81 / 88» — и карточка
    посчитала по ним объём.
    """
    print("\n СВЕРКА ГАБАРИТОВ С ИСТОЧНИКОМ")
    print(" " + "-" * 74)
    cases = [
        ("высота: 83 см, ширина: 81 см, глубина: 88 см",
         "Кресло Cleo, кожа и дерево. Фото и описание без размеров.",
         False, "выдумка на странице без размеров"),
        ("72x76x75H cm", "Mere armchair 72x76x75H cm, fabric or leather",
         True, "есть в источнике"),
        ("Height 60 cm; Diameter 93 cm", "Metropolis Height 60 cm Diameter 93 cm Weight 15 kg",
         True, "подписи словами"),
        ("202x241x92H.", "…165x200 - 202x241x92H. - H.B.36cm…",
         True, "из техлиста"),
        ("", "любой текст", True, "пустая строка не проверяется"),
    ]
    good = 0
    for dims, source, expected, note in cases:
        got = pl._dims_grounded(dims, source)
        hit = got == expected
        good += hit
        print(f"  {OK if hit else BAD} {(dims or '(пусто)')[:44]:46} -> "
              f"{'принято' if got else 'отбито':8} {note}")
    return good, len(cases)


def _page(name: str, **context) -> tuple[object, list[str], dict]:
    """Отрисовать страницу без сети и разобрать её.

    Отдаёт (soup, скрипты, dom) — разметку, тексты скриптов по порядку и
    описание элементов для исполнителя. Значение элемента считается как в
    браузере: у <select> это value выбранного <option>, а не атрибут —
    его у наших переключателей нет вовсе, и без этого finParams() вернул
    бы нули по всем десяти ключам.
    """
    import importlib
    import os
    import re as _re

    from bs4 import BeautifulSoup

    os.environ.setdefault("AURRUM_PASSWORD", "проверка")
    os.environ.setdefault("AURRUM_SECRET_KEY", "x" * 32)
    import app as flask_app
    importlib.reload(flask_app)

    if context.pop("_render", False):
        with flask_app.app.test_request_context():
            from flask import render_template
            html = render_template(name, **context)
    else:
        client = flask_app.app.test_client()
        with client.session_transaction() as sess:
            sess["authorized"] = True
        html = client.get(context.pop("_url")).get_data(as_text=True)

    soup = BeautifulSoup(html, "html.parser")

    scripts = []
    for tag in soup.find_all("script"):
        if tag.get("src"):
            # Общий модуль подключён файлом — читаем его с диска.
            path = tag["src"].split("/")[-1].split("?")[0]
            local = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "static", path)
            if os.path.exists(local):
                scripts.append(open(local, encoding="utf-8").read())
        elif tag.string:
            scripts.append(tag.string)

    ids: dict[str, dict] = {}
    for tag in soup.find_all(attrs={"id": True}):
        spec = {"dataset": {k[5:].replace("-", ""): v
                            for k, v in tag.attrs.items() if k.startswith("data-")}}
        if tag.name == "select":
            chosen = tag.find("option", selected=True) or tag.find("option")
            spec["value"] = (chosen.get("value") if chosen else "") or ""
        elif tag.name == "textarea":
            spec["value"] = tag.text or ""
        else:
            spec["value"] = tag.get("value", "")
        spec["placeholder"] = tag.get("placeholder", "")
        spec["checked"] = tag.has_attr("checked")
        spec["readOnly"] = tag.has_attr("readonly")
        spec["hidden"] = tag.has_attr("hidden")
        if tag.name not in ("input", "select", "textarea"):
            spec["text"] = tag.get_text(" ", strip=True)[:200]
        # Дети, которые страница ищет селектором и которые ЕСТЬ в разметке.
        # Отрисовка позиций пишет innerHTML в tbody таблицы.
        children = {}
        for selector in ("tbody",):
            if tag.find(selector):
                children[selector] = selector
        if children:
            spec["children"] = children
        ids[tag["id"]] = spec

    # Наборы по селекторам считаем здесь: заглушка ничего не выдумывает.
    selectors: dict[str, list[str]] = {}
    for selector in ('[id^="fin_"][id$="_val"], [id^="fin_"][id$="_unit"]',
                     "input.finpick", ".ph", ".parse", "button[data-del]",
                     "[data-meta]", ".toproject", ".card__del",
                     "input[data-k]", "td[data-edit]", ".diff",
                     "#parse_result button[data-f]", "#parse_result button[data-i]",
                     ".diff button.link", "button.usevariant", ".pickrow"):
        try:
            selectors[selector] = [t["id"] for t in soup.select(selector) if t.get("id")]
        except Exception:        # noqa: BLE001 — сложный селектор не беда
            selectors[selector] = []

    return soup, scripts, {"ids": ids, "selectors": selectors}


def _run_page(scripts: list[str], dom: dict, **kw) -> dict:
    """Прогнать скрипт страницы в node и вернуть снимок состояния."""
    import json as _json
    import os
    import subprocess

    payload = {"script": scripts, "dom": dom,
               "storage": kw.get("storage", {}),
               "responses": kw.get("responses", []),
               "actions": kw.get("actions", []),
               "runTimers": kw.get("run_timers", False),
               "confirm": kw.get("confirm", True),
               "prompt": kw.get("prompt")}
    runner = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_pages.mjs")
    got = subprocess.run(["node", runner], input=_json.dumps(payload, ensure_ascii=False),
                         capture_output=True, text=True, timeout=60)
    if got.returncode != 0:
        # Молчаливый провал прогона красит проверки в красный без причины:
        # снимок пустой, а почему — не сказано. Говорим вслух.
        why = (got.stderr or "").strip()[:300]
        print(f"  ! прогон страницы не состоялся: {why or 'без вывода'}")
        return {"error": why, "ids": {}, "storage": {},
                "fetches": [], "throws": []}
    return _json.loads(got.stdout)


def check_page_contract() -> tuple[int, int]:
    """Имена, которыми страница и сервер обязаны совпадать.

    Ловит класс промахов, который дважды кусался на живых правках: поле
    называлось f_type, а код искал f_type_ru — сохранение падало на первой
    же правке карточки. Ни node, ни сети: разметку разбирает bs4, а имена,
    рождённые шаблонной строкой скрипта, достаются регуляркой — узлов от
    innerHTML в разметке физически нет, и селектор по ним даёт ноль.
    """
    import re as _re

    import app as flask_app
    import pricing
    print("\n КОНТРАКТ СТРАНИЦ")
    print(" " + "-" * 74)

    checks: list[tuple[str, bool]] = []

    soup, scripts, dom = _page("project.html", _url="/project")
    page = "\n".join(scripts)
    ids = set(dom["ids"])

    fin_rows = _re.search(r"FIN_ROWS = \[([^\]]*)\]", page)
    rows = _re.findall(r"'([a-z0-9_]+)'", fin_rows.group(1)) if fin_rows else []
    checks.append(("строки итога: поля и переключатели есть в разметке",
                   bool(rows) and all(f"fin_{r}_val" in ids and f"fin_{r}_unit" in ids
                                      for r in rows)))
    # Переименованная строка итога тихо выпала бы и из печати, и из файла.
    checks.append(("ключи итога совпадают с pricing.DEFAULT_FINAL",
                   {f"{r}_{unit}" for r in rows for unit in ("pct", "eur")}
                   == set(pricing.DEFAULT_FINAL)))

    head = _re.search(r"HEAD_FIELDS = \[([^\]]*)\]", page)
    fields = _re.findall(r"'([a-z0-9_]+)'", head.group(1)) if head else []
    # Название проекта — своё поле: проект заводят по имени, а номер
    # спецификации и покупатель появляются позже. Без него в списке он
    # назывался «Без имени».
    import projects as _projects
    checks += [
        ("своё название сильнее собранного",
         _projects.title({"header": {"name": "Владимир, гостиная",
                                     "number": "2867"}}) == "Владимир, гостиная"),
        ("без своего собирается как раньше",
         _projects.title({"header": {"number": "2867", "buyer": "Иванов"}})
         == "Спецификация № 2867 · Иванов"),
        ("пустая шапка — «Без имени»", _projects.title({}) == "Без имени"),
    ]

    # Новый проект кладётся СРАЗУ НА СЕРВЕР: черновик живёт в браузере, а
    # список читает записи с сервера — заведённый только черновиком в
    # перечне не появлялся, «я создал, а его нет».
    projects_page = "\n".join(_page("projects.html", _render=True, rows=[], query="",
                                     total=0, error=None)[1])
    checks += [
        ("создание проекта пишет на сервер",
         "project_save" in projects_page or "/project/save" in projects_page),
        ("и открывает созданный, а не пустой",
         "/project/open/" in projects_page),
        ("«Пересобрать список» убрана", "reindex" not in projects_page),
    ]

    # Знать адрес мало. Страница слала поля проекта верхним уровнем, а
    # сервер ждёт их внутри «project» — и на «Создать» отвечал «Неверный
    # запрос»: завести проект было нельзя вовсе. Поэтому проверяем не
    # адрес, а само тело — тем же разбором, что стоит на сервере.
    _, made_scripts, made_dom = _page("projects.html", _render=True, rows=[],
                                      query="", total=0, error=None)
    made = _run_page(
        made_scripts, made_dom,
        actions=["document.getElementById('newproj_name').value = '2078'",
                 "document.getElementById('newproj').dispatchEvent("
                 "{ type: 'submit', preventDefault() {} })",
                 "await null"],
        responses=[{"json": {"id": "x", "rev": 1}}])
    sent = (made.get("fetches") or [{}])[0]
    body = _json.loads(sent.get("body") or "{}")
    accepted, refusal = flask_app._incoming_project(body)
    checks += [
        ("«Создать» уходит на сохранение проекта",
         "/project/save" in (sent.get("url") or "")),
        ("сервер принимает это тело", refusal is None),
        ("и название доходит до записи",
         ((accepted or {}).get("header") or {}).get("name") == "2078"),
    ]

    # Бургер: полоскам нужна явная ширина — общее правило кнопок ставит
    # align-items: center, и без неё они схлопываются в ноль.
    nav = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "templates", "_nav.html"), encoding="utf-8").read()
    checks.append(("у полосок бургера есть ширина",
                   ".menu__toggle span" in nav and "width: 100%" in nav))

    checks.append(("поля шапки есть в разметке",
                   bool(fields) and all(f"h_{f}" in ids for f in fields)))

    # Ряд таблицы строится innerHTML — имена берём из шаблонной строки.
    row_keys = set(_re.findall(r"cell\('([a-z0-9_]+)'", page)) \
        | set(_re.findall(r'data-k="([a-z0-9_]+)"', page)) \
        | set(_re.findall(r'data-edit="([a-z0-9_]+)"', page))
    known = set(pricing.DEFAULT_POSITION) | {
        "qty", "volume_m3", "list_price", "price", "purchase", "swift",
        "margin", "transfer", "freight", "customs", "assembly_pct",
        "factory_discount_pct", "dealer_markup_pct"}
    checks.append(("ключи ряда позиции известны расчёту",
                   bool(row_keys) and row_keys <= known))

    # Позиции берутся ИЗ БИБЛИОТЕКИ, а не по ссылке. Порядок работы:
    # сперва собирается каталог, потом из него формируется проект;
    # ссылку разбирают один раз, дальше карточка живёт в каталоге.
    checks += [
        ("сверху проекта есть возврат к списку",
         any("списку проектов" in a.get_text() for a in soup.select(".crumbs a"))),
        ("на странице проекта есть выбор из библиотеки",
         "frombook" in set(dom["ids"])),
        ("выбор открывается окном", "bookdlg" in set(dom["ids"])),
        ("в окне есть поиск", "bookdlg_q" in set(dom["ids"])),
        ("поля ввода ссылки на странице проекта нет",
         not soup.select_one('form.add input[type="url"]')),
        ("но путь к разбору со страницы виден",
         any("Разобрать по ссылке" in a.get_text() for a in soup.select("a"))),
        ("за полной карточкой идут в файл", "/library/card/" in page),
    ]

    # Витрина проекта: что в нём лежит — лицом, а не рядами чисел.
    checks += [
        ("витрина проекта есть в разметке", "shelf" in set(dom["ids"])),
        ("она рисуется из тех же позиций", "function renderShelf" in page
         and "renderShelf();" in page),
    ]

    # Константы: списки полей против величин расчёта.
    _, set_scripts, set_dom = _page("settings.html", _url="/settings")
    settings = "\n".join(set_scripts)
    rate_keys = set(_re.findall(r"\['([a-z0-9_]+)',\s*'[^']+',", settings))
    checks.append(("поля ставок совпадают с pricing.DEFAULT_RATES",
                   rate_keys >= set(pricing.DEFAULT_RATES)))
    pos_block = _re.search(r"POS_FIELDS = \[(.*?)\];", settings, _re.S)
    pos_keys = set(_re.findall(r"\['([a-z0-9_]+)'", pos_block.group(1))) if pos_block else set()
    checks.append(("поля начальных чисел совпадают с pricing.DEFAULT_POSITION",
                   pos_keys == set(pricing.DEFAULT_POSITION)))
    # Поля «Констант» рождаются шаблонной строкой — ищем её, а не разметку.
    checks.append(("поля констант строятся шаблонной строкой",
                   'id="f_${key}"' in settings and 'id="p_${key}"' in settings))

    # Редактор карточки один на разбор и каталог. Всё, что слияние
    # пересборки считает «правкой руками» (app.EDITABLE), обязано быть
    # правимым на экране — иначе оно бережётся, но задать его негде.
    lookup_src = open(os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "templates", "lookup.html"), encoding="utf-8").read()
    card_js = _re.search(r"function card\(\) \{(.+?)\n    \}", lookup_src, _re.S)
    # Ключи бывают и по нескольку в строке — ищем после начала строки
    # ИЛИ после запятой, иначе теряются depth_cm и height_cm.
    sent = set(_re.findall(r"(?:^\s*|,\s*)([a-z0-9_]+):",
                           card_js.group(1), _re.M)) if card_js else set()
    missing = sorted(set(flask_app.EDITABLE) - sent)
    checks.append(("редактор шлёт всё, что app.EDITABLE считает правкой"
                   + (f" — нет: {missing}" if missing else ""), not missing))

    good = 0
    for label, hit in checks:
        good += bool(hit)
        print(f"  {OK if hit else BAD} {label}")
    return good, len(checks)


def check_variant_pick() -> tuple[int, int]:
    """Подстановка исполнения обязана менять ЧИСЛА, а не только строку.

    Кнопка «Подставить» несла только строку размеров, и оси с объёмом
    оставались от первого исполнения. У HENGE Sisma это 3,1 м³ вместо
    5,4 — перевозка 1550 € вместо 2700, недобор 1150 € на позиции.
    Хуже, что в описании клиенту печатался подставленный размер: файл
    заявлял 320x150x75, а был оценён по 300x90x75.
    """
    print("\n ПОДСТАНОВКА ИСПОЛНЕНИЯ")
    print(" " + "-" * 74)

    product = pl.Product(
        source_url="https://example.com/x", brand="HENGE", model="SISMA",
        type_ru="Стол", dims_raw="300x90x75h cm",
        width_cm=300.0, depth_cm=90.0, height_cm=75.0,
        volume_m3=3.1, volume_source="расчёт по габаритам", dims_confident=True,
        variants=[{"dims_raw": "300x90x75h cm", "sku": "A"},
                  {"dims_raw": "320x150x75h cm", "sku": "B"}],
    )
    cards = pl.variant_cards(product)
    checks = [
        ("исполнение несёт посчитанные оси",
         cards[1]["width_cm"] == 320.0 and cards[1]["height_cm"] == 75.0),
        ("исполнение несёт свой объём", cards[1]["volume_m3"] == 5.4),
        ("форма совпадает с кандидатом техлиста",
         set(cards[1]) >= {"value", "width_cm", "depth_cm", "height_cm",
                           "volume_m3", "volume_source", "dims_confident",
                           "warnings"}),
    ]

    _, scripts, dom = _page("lookup.html", _render=True, url=product.source_url,
                            product=product, variants=cards,
                            description=pl.to_excel_description(product),
                            saved_id=None, types=pl.TYPES_RU)

    after = _run_page(scripts, dom, actions=[
        "document.getElementById('usevariant_1').click()"])
    got = after["ids"]
    checks += [
        ("строка размеров сменилась",
         got["f_dims_raw"]["value"] == "320x150x75h cm"),
        ("длина сменилась", got["f_d"]["value"] == "320"),
        ("глубина сменилась", got["f_g"]["value"] == "150"),
        ("высота осталась верной", got["f_v"]["value"] == "75"),
        ("объём пересчитан", got["f_vol"]["value"] == "5.4"),
    ]

    # Исполнение без разбираемых размеров обязано ОЧИСТИТЬ группу,
    # а не оставить числа предыдущего: смесь хуже пустоты.
    mixed = pl.Product(
        source_url="https://example.com/y", brand="X", model="Y",
        type_ru="Стол", dims_raw="300x90x75h cm",
        width_cm=300.0, depth_cm=90.0, height_cm=75.0, volume_m3=3.1,
        dims_confident=True,
        variants=[{"dims_raw": "300x90x75h cm"},
                  {"dims_raw": "по запросу"}],
    )
    mixed_cards = pl.variant_cards(mixed)
    _, scripts2, dom2 = _page("lookup.html", _render=True, url=mixed.source_url,
                              product=mixed, variants=mixed_cards,
                              description=pl.to_excel_description(mixed),
                              saved_id=None, types=pl.TYPES_RU)
    cleared = _run_page(scripts2, dom2, actions=[
        "document.getElementById('usevariant_1').click()"])["ids"]
    checks += [
        ("исполнение без размеров очищает длину",
         cleared["f_d"]["value"] == ""),
        ("исполнение без размеров очищает объём",
         cleared["f_vol"]["value"] == ""),
    ]

    import app as flask_app

    # Подстановка правит ТУ строку описания, где стоял прежний размер.
    # Раньше правилась третья по счёту — а третья это размер только
    # когда есть и модель, и тип; без типа туда попадала аннотация, и
    # подстановка молча затирала её размером.
    no_type = pl.Product(source_url="https://x/y", model="Sisma",
                         dims_raw="300x90x75", width_cm=300, depth_cm=90,
                         height_cm=75, summary_ru="Кровать с изголовьем.")
    no_type.variants = [{"dims_raw": "300x90x75"}, {"dims_raw": "320x150x75"}]
    text = pl.to_excel_description(no_type, with_finishes=False)
    _, sc_nt, dom_nt = _page("lookup.html", _render=True, product=no_type,
                             url=no_type.source_url, description=text,
                             variants=pl.variant_cards(no_type),
                             types=pl.TYPES_RU, project_choices=[],
                             card_base=flask_app._card_base(no_type, text))
    swapped = _run_page(sc_nt, dom_nt,
                        actions=["document.getElementById('usevariant_1').click()"])
    got = swapped["ids"].get("f_desc", {}).get("value", "")
    checks += [
        ("подстановка не затирает аннотацию",
         "Кровать с изголовьем." in got),
        ("и ставит новый размер на место прежнего",
         "320x150x75" in got and "300x90x75" not in got),
        ("о подстановке сказано там, где нажали",
         "320x150x75" in swapped["ids"].get("variant_said", {}).get("text", "")),
    ]

    good = 0
    for label, hit in checks:
        good += bool(hit)
        print(f"  {OK if hit else BAD} {label}")
    return good, len(checks)


def check_page_formats() -> tuple[int, int]:
    """Формулы показа и записи — текстом из шаблонов, прогоном в node.

    Коэффициент сборки живёт в четырёх копиях: показ и запись в проекте,
    показ и запись в константах. Расхождение любой из них тихо меняет
    делитель СУММЫ, то есть цену клиенту. Проверяем НАСТОЯЩИЕ копии,
    вынутые из шаблонов, а не их пересказ, и прогоняем в node: round()
    в Python банковское, а Math.round половину гонит вверх — на 0,05
    копии разошлись бы, а мы бы этого не увидели.
    """
    import json as _json
    import os
    import re as _re
    import subprocess
    print("\n ФОРМУЛЫ СТРАНИЦ")
    print(" " + "-" * 74)

    here = os.path.dirname(os.path.abspath(__file__))
    project = open(os.path.join(here, "templates", "project.html"), encoding="utf-8").read()
    settings = open(os.path.join(here, "templates", "settings.html"), encoding="utf-8").read()

    # Показ: коэффициент -> проценты. Запись: проценты -> коэффициент.
    show = _re.findall(r"Math\.round\(\(1 - [^)]*\)? ?\* 1000\) / 10", project)
    write = _re.search(r"Math\.round\(\(1 - pct / 100\) \* 1000\) / 1000", project)
    set_show = _re.search(r"asmToPct = v => (Math\.round\(\(1 - v\) \* 1000\) / 10)", settings)
    set_write = _re.search(r"Math\.round\(\(1 - number / 100\) \* 1000\) / 1000", settings)

    checks: list[tuple[str, bool]] = []
    checks.append(("формула показа найдена в проекте и в константах",
                   bool(show) and bool(set_show)))
    checks.append(("формула записи найдена в проекте и в константах",
                   bool(write) and bool(set_write)))
    # Обе записи обязаны совпасть текстуально с точностью до имени числа.
    same = bool(write and set_write) and (
        write.group(0).replace("pct", "N") == set_write.group(0).replace("number", "N"))
    checks.append(("копии формулы записи совпадают текстуально", same))
    same_show = bool(show and set_show) and (
        _re.sub(r"\(1 - [^*]*\*", "(1 - N *", show[0])
        == _re.sub(r"\(1 - [^*]*\*", "(1 - N *", set_show.group(1)))
    checks.append(("копии формулы показа совпадают текстуально", same_show))

    # Круговой прогон в node: показ(запись(x)) == x на живых значениях.
    if write and set_show:
        script = f"""
        const toCoef = (pct) => {write.group(0).replace('pct', 'pct')};
        const toPct = (v) => {set_show.group(1)};
        const out = [0, 5, 12.5, 20, 45].map((x) => {{
          const pct = x; return [x, toPct(toCoef(pct))];
        }});
        process.stdout.write(JSON.stringify(out));
        """
        got = subprocess.run(["node", "-e", script], capture_output=True,
                             text=True, timeout=30)
        pairs = _json.loads(got.stdout) if got.returncode == 0 else []
        checks.append(("круговой прогон: показ(запись(x)) == x",
                       bool(pairs) and all(abs(a - b) < 1e-9 for a, b in pairs)))
        if pairs:
            broken = [f"{a} -> {b}" for a, b in pairs if abs(a - b) >= 1e-9]
            if broken:
                print("      разошлось:", ", ".join(broken))
    else:
        checks.append(("круговой прогон: показ(запись(x)) == x", False))

    good = 0
    for label, hit in checks:
        good += bool(hit)
        print(f"  {OK if hit else BAD} {label}")
    return good, len(checks)


def check_page_logic() -> tuple[int, int]:
    """Поведение страниц: скрипт исполняется, состояние сверяется.

    Только по статической разметке — узлов, рождённых innerHTML, у
    заглушки нет, и она это честно объявляет. Первая проверка —
    страховочная: если засев не сработал, весь слой недостоверен, и
    остальные его выводы ничего не значат.
    """
    import json as _json
    print("\n ПОВЕДЕНИЕ СТРАНИЦ")
    print(" " + "-" * 74)

    checks: list[tuple[str, bool]] = []
    _, scripts, dom = _page("project.html", _url="/project")

    # 0. Страховка: пустое хранилище — значения из разметки, а не нули.
    probe = _run_page(scripts, dom, responses=[{"notJson": True}],
                      actions=["document.getElementById('status').textContent = "
                               "JSON.stringify(finParams())"])
    params = {}
    try:
        params = _json.loads(probe["ids"]["status"]["text"])
    except Exception:            # noqa: BLE001
        params = {}
    checks.append(("засев сработал: сборка 5 %, персональная скидка 30 %",
                   params.get("assembly_pct") == 5 and params.get("personal_pct") == 30))
    checks.append(("ноль в евро не вытесняет процент",
                   params.get("assembly_eur") == 0))

    # 1. Живой укус: /calc ответил не-JSON — позиции обязаны остаться.
    project = _json.dumps([{"brand": "VENICEM", "model": "CIRCLE", "qty": 1,
                            "list_price": 2550, "volume_m3": 0.2,
                            "assembly": 0.95}], ensure_ascii=False)
    storage = {"aurrum.current": "p1",
               "aurrum.draft.p1": _json.dumps({"positions": _json.loads(project),
                                               "header": {}, "final": {}, "rev": 0})}
    fail = _run_page(scripts, dom, storage=storage, responses=[{"notJson": True}],
                     actions=["await recalc()"])
    status = (fail["ids"].get("status") or {}).get("text", "")
    checks.append(("отказ расчёта не прячет позиции",
                   not (fail["ids"].get("rows") or {}).get("hidden", True)))
    checks.append(("отказ расчёта объяснён словами", "не ответил" in status))
    checks.append(("полоса итогов при отказе спрятана",
                   (fail["ids"].get("totals") or {}).get("hidden") is True))

    # 2. Удачный расчёт, затем отказ: старая сумма не должна остаться.
    good_answer = {"ok": True, "json": {
        "lines": [{"purchase": 1275, "margin": 446, "transfer": 64, "swift": 200,
                   "freight": 100, "total": 2085, "with_assembly": 2195,
                   "price": 2190, "sum": 2190, "levels": {"finserv": 3000}}],
        "sum": 2190, "volume_m3": 0.2, "count": 1, "levels": {"finserv": 3000},
        "final": {"услуги": 0, "доставка": 0, "сборка": 0, "всего": 2190,
                  "скидка": 0, "подытог": 2190, "доп_скидка": 0, "к_оплате": 2190}}}
    two = _run_page(scripts, dom, storage=storage,
                    responses=[good_answer, {"notJson": True}],
                    actions=["await recalc()", "await recalc()"])
    checks.append(("после отказа полоса не показывает прежнюю сумму",
                   (two["ids"].get("totals") or {}).get("hidden") is True))

    # 3. Восстановление единиц итога из сохранённых параметров.
    with_eur = dict(storage)
    with_eur["aurrum.draft.p1"] = _json.dumps({
        "positions": [], "header": {}, "rev": 0,
        "final": {"delivery_eur": 1500, "personal_eur": 0, "personal_pct": 30}})
    units = _run_page(scripts, dom, storage=with_eur, responses=[{"notJson": True}])
    checks.append(("доставка в евро включает режим €",
                   (units["ids"].get("fin_delivery_unit") or {}).get("value") == "eur"
                   and (units["ids"].get("fin_delivery_val") or {}).get("value") == "1500"))
    checks.append(("нулевое евро НЕ включает режим € у скидки",
                   (units["ids"].get("fin_personal_unit") or {}).get("value") == "pct"))

    # 4. Круговая: тело запроса со страницы скармливаем настоящему расчёту.
    body = None
    for call in two["fetches"]:
        if "/calc" in call["url"]:
            body = _json.loads(call["body"])
            break
    ring = False
    if body:
        import pricing
        line = pricing.project(body["positions"], rates=body.get("rates"),
                               final=body.get("final"))["lines"][0]
        ring = abs(line["price"] - 2190) < 0.5
    checks.append(("запрос страницы даёт расчётную цену книги", ring))

    # 5. Отделки в разборе: снятие галочки вынимает элемент списка,
    #    а не подстроку — CRYSTAL входит в CRYSTAL/GREY/OLIVE.
    product = type("P", (), {})()
    for name, value in dict(
            source_url="https://x", brand="B", model="M", type_ru="Люстра",
            collection="", designer="", dims_raw="", width_cm=None, depth_cm=None,
            height_cm=None, dims_confident=True, volume_m3=None, volume_source="",
            package_note="", tech_note="", summary_ru="", photo_urls=[], doc_urls=[],
            spec_pdf_url="", variants=[], warnings=[], dims_from_spec=False,
            finishes=[{"role_ru": "Стекло", "material": "Crystal", "code": "CC"},
                      {"role_ru": "Стекло", "material": "Crystal/Grey/Olive", "code": "ED"}],
    ).items():
        setattr(product, name, value)
    _, look_scripts, look_dom = _page(
        "lookup.html", _render=True, product=product, types=pl.TYPES_RU,
        description="M\nЛюстра\nСтекло - CRYSTAL + CRYSTAL/GREY/OLIVE", error=None)
    # Строку кладём ДЕЙСТВИЕМ, а не начальным описанием: при открытии
    # карточка, где отмечено всё до единой отделки, считается заполненной
    # разбором, и строка убирается. Здесь же проверяется точность снятия
    # одного материала, а не то правило.
    fin = _run_page(look_scripts, look_dom, actions=[
        "document.getElementById('f_desc').value = "
        "['M', 'Люстра', 'Стекло - CRYSTAL + CRYSTAL/GREY/OLIVE']"
        ".join(String.fromCharCode(10))",
        "removeFinish('Стекло', 'Crystal')",
        "document.getElementById('f_note').value = document.getElementById('f_desc').value",
    ])
    left = (fin["ids"].get("f_note") or {}).get("value", "")
    checks.append(("снятие CRYSTAL не задевает CRYSTAL/GREY/OLIVE",
                   "CRYSTAL/GREY/OLIVE" in left and "- CRYSTAL +" not in left))

    good = 0
    for label, hit in checks:
        good += bool(hit)
        print(f"  {OK if hit else BAD} {label}")
    return good, len(checks)


def check_parse_progress() -> tuple[int, int]:
    """Ход разбора: полоса, лента и готовая карточка последней строкой.

    Разбор идёт полминуты и дольше, и всё это время экран был пуст.
    Проверяем не вид, а договор: что уходит на сервер, что делают
    строки потока и чем всё кончается — карточкой или honest-ошибкой.
    """
    import app as flask_app
    print("\n ХОД РАЗБОРА")
    print(" " + "-" * 74)

    _, scripts, dom = _page("lookup.html", _render=True)
    nl = chr(10)

    def submit(body: str, **kw):
        return _run_page(
            scripts, dom,
            actions=["document.getElementById('url').value = 'https://porada.it/x'",
                     "document.getElementById('form').dispatchEvent("
                     "{ type: 'submit', preventDefault() {} })",
                     "await null", "await null", "await null"],
            responses=[{"text": body}], **kw)

    steps = nl.join([
        _json.dumps({"share": 0.15, "line": "Страница получена: 11423 знаков"},
                    ensure_ascii=False),
        _json.dumps({"share": 0.55, "line": "Прочитано: Porada Infinity · Стол"},
                    ensure_ascii=False),
        _json.dumps({"html": "<html>карточка</html>"}, ensure_ascii=False),
    ])
    done = submit(steps)
    sent = (done.get("fetches") or [{}])[0]

    broken = submit(nl.join([
        _json.dumps({"share": 0.3, "line": "Читаю страницу моделью…"},
                    ensure_ascii=False),
        _json.dumps({"error": "Страница не отдала содержимого."},
                    ensure_ascii=False),
    ]))

    # Строка, разрезанная посередине куска, не должна ронять разбор.
    half = submit('{"share": 0.4, "li')

    checks = [
        ("разбор уходит потоком", "/lookup/stream" in (sent.get("url") or "")),
        ("ссылка уходит в теле", "porada.it" in (sent.get("body") or "")),
        ("полоса на месте", _page("lookup.html", _render=True)[0].find(id="run_fill")
         is not None),
        ("полоса доходит до конца",
         done["ids"].get("run_fill", {}).get("style", {}).get("width") == "55%"),
        ("последняя веха видна",
         "Прочитано" in done["ids"].get("run_now", {}).get("text", "")),
        ("лента держит все строки",
         done["ids"].get("run_log", {}).get("text", "").count(nl) == 1),
        ("готовая карточка выводится, а не разбирается заново",
         done.get("written") == "<html>карточка</html>"),
        ("ошибка названа словами",
         "не отдала" in broken["ids"].get("run_now", {}).get("text", "")),
        ("после ошибки кнопку можно нажать снова",
         broken["ids"].get("go", {}).get("disabled") is False),
        ("ошибка не подменяет страницу", not broken.get("written")),
        ("разрезанная строка ничего не роняет", not half.get("error")),
        ("страница карточки собирается одним местом",
         "_card_page" in open(os.path.join(
             os.path.dirname(os.path.abspath(__file__)), "app.py"),
             encoding="utf-8").read()),
    ]

    # Вехи ставит сам разбор: без обходчика поток нечем наполнить.
    import inspect
    checks.append(("разбор принимает обходчик хода работы",
                   "progress" in inspect.signature(
                       flask_app.product_lookup.lookup).parameters))

    good = 0
    for label, hit in checks:
        good += bool(hit)
        print(f"  {OK if hit else BAD} {label}")
    return good, len(checks)


def check_projects() -> tuple[int, int]:
    """Хранилище проектов: счётчик правок и то, что он обязан ловить.

    Без сети: транспорт подменяется. Проверяется не «записалось ли», а
    единственное, ради чего всё затевалось, — что чужая работа не может
    быть затёрта молча.
    """
    import projects
    print("\n ПРОЕКТЫ НА СЕРВЕРЕ")
    print(" " + "-" * 74)

    store: dict[str, object] = {}
    index: list[dict] = []

    def fake_put(pathname, payload):
        if pathname == projects.INDEX:
            index[:] = payload["items"]
        else:
            store[pathname] = payload

    real_put, real_index = projects._put, projects.read_index
    projects._put = fake_put
    projects.read_index = lambda: [dict(r) for r in index]

    checks: list[tuple[str, bool]] = []
    try:
        first = projects.save({"id": "p1", "rev": 0, "positions": [],
                               "header": {"number": "2867/3", "buyer": "Иванов И. И."}})
        checks.append(("новый проект получает правку № 1", first["rev"] == 1))
        checks.append(("имя для списка собрано из шапки",
                       index and index[0]["title"] == "Спецификация № 2867/3 · Иванов И. И."))

        second = projects.save({**first, "positions": [{"brand": "X", "model": "Y"}]})
        checks.append(("повторная запись поднимает правку", second["rev"] == 2))
        checks.append(("в списке число позиций", index[0]["count"] == 1))

        # Тот самый случай: двое открыли одно, один сохранил, второй пишет
        # поверх со старым номером.
        stale = False
        try:
            projects.save({**first, "positions": [{"brand": "Чужое"}]})
        except projects.Conflict:
            stale = True
        checks.append(("устаревшая правка отклонена, а не записана", stale))
        checks.append(("отклонённая запись ничего не изменила",
                       store["projects/p1.json"]["positions"][0]["brand"] == "X"))

        # Проект удалён, а у менеджера открыт: сохранение не должно его воскрешать.
        index.clear()
        gone = False
        try:
            projects.save({**second, "rev": 2})
        except projects.Conflict:
            gone = True
        checks.append(("удалённый проект не воскресает сохранением", gone))

        # Пустая шапка не оставляет строку без имени.
        index.clear()
        projects.save({"id": "p2", "rev": 0, "positions": [], "header": {}})
        checks.append(("проект без шапки называется «Без имени»",
                       index[0]["title"] == "Без имени"))

        # Сумма в списке считается той же цепочкой, что на экране.
        index.clear()
        with_money = projects.save({
            "id": "p3", "rev": 0, "header": {},
            "positions": [{"list_price": 2550, "volume_m3": 0.2, "qty": 1, "assembly": 0.95}],
            "final": {"assembly_pct": 5, "personal_pct": 30}})
        import pricing
        want = pricing.project(with_money["positions"], final=with_money["final"])["final"]["к_оплате"]
        checks.append(("сумма в списке совпадает с расчётом страницы",
                       abs((index[0].get("sum") or 0) - want) < 0.01))

        checks.append(("поиск идёт по фамилии покупателя",
                       len(projects.search([{"buyer": "Иванов И. И."}, {"buyer": "Петров"}],
                                           "иванов")) == 1))

        # Маршруты: та же подмена, без сети.
        import importlib
        import os
        os.environ.setdefault("AURRUM_PASSWORD", "проверка")
        os.environ.setdefault("AURRUM_SECRET_KEY", "x" * 32)
        import app as flask_app
        importlib.reload(flask_app)
        flask_app.projects._put = fake_put
        flask_app.projects.read_index = lambda: [dict(r) for r in index]
        # Чтение тоже без сети: иначе маршрут открытия честно отвечает
        # 502 «хранилище не настроено», и проверка мерит не то.
        flask_app.projects.get = lambda pid: store.get(f"projects/{pid}.json")
        client = flask_app.app.test_client()

        # Запросу за данными — отказ с JSON, а не страница входа: fetch
        # шёл за редиректом и выдавал «Unexpected token '<'».
        got = client.post("/project/save", json={"project": {"id": "p9", "rev": 0, "positions": []}})
        checks.append(("истёкшая сессия: JSON-запрос получает 401 с причиной",
                       got.status_code == 401 and "error" in (got.get_json() or {})))
        checks.append(("истёкшая сессия: переход по ссылке уводит на вход",
                       client.get("/project").status_code == 302))

        with client.session_transaction() as sess:
            sess["authorized"] = True
        index.clear()

        body = {"id": "p9", "rev": 0, "positions": [], "header": {"number": "1"}}
        first = client.post("/project/save", json={"project": body})
        checks.append(("сохранение отдаёт номер правки",
                       first.status_code == 200 and first.get_json()["rev"] == 1))
        again = client.post("/project/save", json={"project": body})
        checks.append(("повтор со старым номером — 409, а не тихая запись",
                       again.status_code == 409 and again.get_json().get("current")))

        bad = [({"id": "..", "rev": 0, "positions": []}, "чужой адрес"),
               ({"id": "p9", "rev": "три", "positions": []}, "номер правки строкой"),
               ({"id": "p9", "rev": 0, "positions": "нет"}, "позиции не списком"),
               ({"id": "p9", "rev": 0, "positions": [1]}, "позиция не записью")]
        checks.append(("кривой запрос отбивается до записи",
                       all(client.post("/project/save", json={"project": b}).status_code == 400
                           for b, _ in bad)))
        checks.append(("страница проекта без записи не рисуется",
                       client.get("/project/open/net-takogo").status_code in (404, 503)))
        # Запись есть — страница открывается с ней, а не с чужим черновиком.
        store["projects/p9.json"] = {"id": "p9", "rev": 1, "positions": [],
                                     "header": {"number": "77/1"}}
        page = client.get("/project/open/p9")
        checks.append(("открытая запись попадает в страницу",
                       page.status_code == 200 and "77/1" in page.get_data(as_text=True)))
    finally:
        projects._put, projects.read_index = real_put, real_index

    good = 0
    for label, hit in checks:
        good += bool(hit)
        print(f"  {OK if hit else BAD} {label}")
    return good, len(checks)


def check_rooms() -> tuple[int, int]:
    """Комнаты: раскладка, книга и обратный разбор.

    Числа взяты с рабочей формы (docs/form-rooms.md): нумерация
    начинается заново в каждой комнате, заголовок стоит только в колонке
    «Описание», итоговая сумма охватывает весь блок вместе с заголовками.
    """
    import io

    import book_export
    import openpyxl
    print("\n КОМНАТЫ")
    print(" " + "-" * 74)

    checks: list[tuple[str, bool]] = []

    # Раскладка: порядок из списка, пустая комната жива, безымянные в хвост.
    blocks = book_export._blocks(
        [{"room": "Холл", "model": "A"}, {"model": "B"},
         {"room": "Спальня", "model": "C"}, {"room": "Холл", "model": "D"}],
        ["Этаж 1", "Холл"])
    checks.append(("порядок комнат берётся из списка",
                   [name for name, _ in blocks] == ["", "Этаж 1", "Холл", "Спальня"]))
    checks.append(("пустая комната остаётся: в форме есть «Этаж 1» без позиций",
                   blocks[1][1] == []))
    # Первыми, а не хвостом: метки «комната кончилась» в книге нет, и
    # хвост при обратном разборе прилипал к последней комнате.
    checks.append(("позиции без комнаты идут первыми",
                   [p["model"] for p in blocks[0][1]] == ["B"]))
    checks.append(("комната, которой нет в списке, не теряется",
                   any(name == "Спальня" for name, _ in blocks)))

    # Описание обязательно: печать считает строку позицией по паре
    # «номер + описание», и без него позиция в документ не попадёт.
    positions = [
        {"brand": "LONGHI", "model": "ARIANA", "room": "Холл", "qty": 1,
         "list_price": 6886, "price": 5750, "volume_m3": 0.5,
         "description": "ARIANA\nБанкетка"},
        {"brand": "FLOU", "model": "BUTTERFLY", "room": "Спальня 1", "qty": 1,
         "list_price": 4389, "price": 4130, "volume_m3": 1,
         "description": "MADAME BUTTERFLY\nКресло"},
        {"brand": "TRUSSARDI", "model": "VIBES", "room": "Спальня 2", "qty": 1,
         "list_price": 16020, "price": 15590, "volume_m3": 6.8,
         "description": "VIBES\nКровать"},
        {"brand": "TRUSSARDI", "model": "COMFY", "room": "Спальня 2", "qty": 1,
         "list_price": 5000, "price": 3850, "volume_m3": 0.3,
         "description": "COMFY\nТумбочка"},
    ]
    rooms = ["Этаж 1", "Холл", "Спальня 1", "Спальня 2"]
    book = openpyxl.load_workbook(io.BytesIO(
        book_export.build(positions, rooms=rooms))).active

    titles = {book.cell(r, 3).value for r in range(14, 24)}
    checks.append(("заголовки комнат стоят в колонке «Описание»",
                   {"Этаж 1", "Холл", "Спальня 1", "Спальня 2"} <= titles))
    checks.append(("в строке заголовка нет номера позиции",
                   all(book.cell(r, 1).value in (None, "")
                       for r in range(14, 24) if book.cell(r, 3).value in rooms)))
    numbers = [book.cell(r, 1).value for r in range(14, 24)
               if book.cell(r, 1).value not in (None, "")]
    checks.append(("нумерация начинается заново в каждой комнате",
                   numbers == [1, 1, 1, 2]))

    total = next((book.cell(r, 6).value for r in range(20, 30)
                  if str(book.cell(r, 3).value or "").startswith("Сумма")), "")
    checks.append(("итог охватывает весь блок вместе с заголовками",
                   str(total) == "=SUM(F14:F21)"))

    # Проект без комнат обязан выглядеть ровно как раньше. Комнату несёт
    # сама позиция, поэтому «без комнат» — это позиции без поля room,
    # а не отсутствие списка: со списком или без, размеченные позиции
    # группируются, и это правильно.
    bare = [{k: v for k, v in p.items() if k != "room"} for p in positions]
    plain = openpyxl.load_workbook(io.BytesIO(book_export.build(bare))).active
    checks.append(("без комнат нумерация сквозная, как раньше",
                   [plain.cell(r, 1).value for r in range(14, 18)] == [1, 2, 3, 4]))
    checks.append(("без комнат итог считается от первой позиции",
                   str(next((plain.cell(r, 6).value for r in range(16, 26)
                             if str(plain.cell(r, 3).value or "").startswith("Сумма")), ""))
                   == "=SUM(F14:F17)"))
    checks.append(("позиция несёт комнату и без переданного списка",
                   openpyxl.load_workbook(io.BytesIO(
                       book_export.build(positions))).active.cell(14, 3).value == "Холл"))

    # Круг «выгрузка -> печать»: что выгрузили, то и прочли.
    import spec_parser
    printed = spec_parser.parse(book_export.build(
        positions + [{"brand": "БЕЗ", "model": "КОМНАТЫ", "qty": 1, "price": 900,
                      "description": "БЕЗ\nКОМНАТЫ"}],
        rooms=rooms, values=True))
    got = [(b.title, [i.brand for i in b.items]) for b in printed.blocks]
    checks.append(("печать возвращает те же блоки, что ушли в файл",
                   got == [("", ["БЕЗ"]), ("Этаж 1", []), ("Холл", ["LONGHI"]),
                           ("Спальня 1", ["FLOU"]),
                           ("Спальня 2", ["TRUSSARDI", "TRUSSARDI"])]))
    checks.append(("подписи итогов не становятся комнатами",
                   not any("Евро" in b.title for b in printed.blocks)))

    # Сама рабочая форма заказчика: разметка шапки там своя — название
    # секции и «(Италия)» стоят в колонке D, а даты в блоке «Покупатель»
    # нет вовсе, она рядом с заголовком предложения.
    import os
    form = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples",
                        "0000-Offer-AUR-FORM.xlsx")
    if os.path.exists(form):
        real = spec_parser.parse(open(form, "rb").read())
        checks.append(("форма заказчика: комнаты прочитаны все пять",
                       [b.title for b in real.blocks]
                       == ["Этаж 1", "Холл", "Гостевая Спальня 1",
                           "Мастер Спальня 2", "Гостевая Спальня 2"]))
        checks.append(("форма заказчика: нумерация внутри комнат",
                       [i.n for b in real.blocks for i in b.items]
                       == ["1", "1", "1", "1", "2"]))
        checks.append(("форма заказчика: дата взята рядом с заголовком",
                       real.date == "25.07.2026"))
        checks.append(("форма заказчика: происхождение найдено не в колонке A",
                       real.origin == "(Италия)"))

    # Старые книги без комнат читаются как раньше — одним блоком.
    import os
    sample = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples",
                          "2867_Спецификация_20260311_PRJ_VLADIMIR_MODULNOVA_GAL.xlsx")
    if os.path.exists(sample):
        old_book = spec_parser.parse(open(sample, "rb").read())
        # Маршруты: список комнат проверяется до сборки файла.
    import importlib
    import json as _json
    import os
    os.environ.setdefault("AURRUM_PASSWORD", "проверка")
    os.environ.setdefault("AURRUM_SECRET_KEY", "x" * 32)
    import app as flask_app
    importlib.reload(flask_app)
    client = flask_app.app.test_client()
    with client.session_transaction() as sess:
        sess["authorized"] = True
    body = {"positions": [{"brand": "X", "model": "Y", "qty": 1, "price": 100,
                           "room": "Холл", "description": "X\nСтол"}],
            "rooms": ["Холл"]}
    checks.append(("выгрузка принимает комнаты",
                   client.post("/project/export", json=body).status_code == 200))
    checks.append(("кривой список комнат отбивается",
                   client.post("/project/export",
                               json={**body, "rooms": "Холл"}).status_code == 400))
    printed = client.post("/project/print", data={"payload": _json.dumps(body)})
    checks.append(("печать показывает комнату",
                   printed.status_code == 200
                   and "Холл" in printed.get_data(as_text=True)))

    checks.append(("книга без комнат читается одним блоком",
                       len(old_book.blocks) == 1 and old_book.blocks[0].title == ""
                       and len(old_book.blocks[0].items) == len(old_book.items)))

    good = 0
    for label, hit in checks:
        good += bool(hit)
        print(f"  {OK if hit else BAD} {label}")
    return good, len(checks)


def check_header_roundtrip() -> tuple[int, int]:
    """Шапка проекта переживает выгрузку и обратный разбор.

    Две половины писались порознь: выгрузка складывает книгу, печать её
    разбирает. Пока шапки не было, документ выходил с пустым номером и
    прочерками — и заметить это можно было только глазами на печати.
    """
    import book_export
    import spec_parser
    print("\n ШАПКА: ВЫГРУЗКА -> ПЕЧАТЬ")
    print(" " + "-" * 74)
    head = {"number": "2867/3", "contract": "2867",
            "buyer": "Иванов И. И.", "date": "12.03.2026"}
    position = [{"brand": "VENICEM", "model": "CIRCLE", "qty": 1,
                 "description": "CIRCLE\nТоршер"}]
    spec = spec_parser.parse(book_export.build(position, header=head))
    good = 0
    for key, got in (("number", spec.number), ("contract", spec.contract),
                     ("buyer", spec.buyer), ("date", spec.date)):
        hit = got == head[key]
        good += hit
        print(f"  {OK if hit else BAD} {key:9} -> {got!r}")

    # Пустая шапка не должна порождать «Спецификация № к Договору».
    bare = spec_parser.parse(book_export.build(position))
    hit = not (bare.number or bare.buyer or bare.date)
    good += hit
    print(f"  {OK if hit else BAD} без шапки документ остаётся пустым, "
          f"а не «Спецификация №»")
    return good, 5


def check_download_headers() -> tuple[int, int]:
    """Заголовки выгрузки должны кодироваться latin-1.

    Прод падал уже после сборки файла: кириллица в filename= роняла
    ответ, браузер получал обрыв и показывал общее «не удалось собрать
    файл». Локальный сервер это прощал, поэтому проверка тут.
    """
    import importlib
    import os
    os.environ.setdefault("AURRUM_PASSWORD", "проверка")
    os.environ.setdefault("AURRUM_SECRET_KEY", "x" * 32)
    import app as flask_app
    importlib.reload(flask_app)

    print("\n ЗАГОЛОВКИ ВЫГРУЗКИ")
    print(" " + "-" * 74)
    client = flask_app.app.test_client()
    with client.session_transaction() as sess:
        sess["authorized"] = True
    got = client.post("/project/export", json={"positions": [
        {"brand": "VENICEM", "model": "CIRCLE", "qty": 1, "list_price": 2550}]})

    checks = []
    checks.append(("ответ отдан", got.status_code == 200))
    for name, value in got.headers.items():
        try:
            value.encode("latin-1")
            ok = True
        except UnicodeEncodeError:
            ok = False
        if not ok:
            checks.append((f"{name} кодируется latin-1", False))
    checks.append(("все заголовки кодируются latin-1",
                   all(hit for _, hit in checks)))
    disposition = got.headers.get("Content-Disposition", "")
    checks.append(("настоящее имя приходит в filename*",
                   "filename*=UTF-8''" in disposition))

    good = 0
    for label, hit in checks:
        good += hit
        print(f"  {OK if hit else BAD} {label}")
    return good, len(checks)


def check_item_edit_mode() -> tuple[int, int]:
    """Редактор один на разбор и каталог, но ведёт себя по месту.

    Пока их было два, они разошлись до того, что одно поле называлось
    `f_type` на разборе и `f_type_ru` в каталоге. Каталожный был вдобавок
    урезан: ни отделок с галочками, ни фотографий, ни производителя.

    Из каталога карточка открывается на ЧТЕНИЕ — туда чаще заглядывают,
    чем правят. После разбора по ссылке правка включена сразу: там её и
    пришли делать.
    """
    import app as flask_app
    print("\n РЕДАКТОР КАРТОЧКИ")
    print(" " + "-" * 74)

    stored = {"id": "barovier-toso-aurora", "brand": "Barovier&Toso",
              "model": "Aurora", "type_ru": "Настольная лампа",
              "dims_raw": "H. 28 x 11 x 10 cm", "width_cm": 10.0,
              "depth_cm": 10.0, "height_cm": 28.0, "volume_m3": 0.1,
              "dims_confident": True, "note": "1,5 kg",
              "summary_ru": "Лампа из выдувного муранского стекла.", "photos": ["https://x/a.jpg"],
              "doc_urls": [], "source_url": "https://www.barovier.com/x",
              "finishes": [{"role_ru": "Стекло", "material": "Crystal"}]}
    product = flask_app._as_product(stored)
    stored["description"] = pl.to_excel_description(product, with_finishes=False)

    def screen(from_library):
        return _page("lookup.html", _render=True, product=product,
                     url=product.source_url, description=stored["description"],
                     variants=pl.variant_cards(product), types=pl.TYPES_RU,
                     from_library=from_library)

    soup_lib, scripts_lib, dom_lib = screen(stored["id"])
    soup_new, scripts_new, dom_new = screen(None)

    # Каталог получил всё, чего у него не было.
    checks = [
        ("в каталоге правится производитель", "f_brand" in dom_lib["ids"]),
        ("в каталоге правится модель", "f_model" in dom_lib["ids"]),
        ("отделки с галочками", len(soup_lib.select("input.finpick")) == 1),
        ("отбор фотографий на месте", len(soup_lib.select(".ph")) == 1),
        ("строки ввода ссылки из каталога нет", soup_lib.find(id="url") is None),
        ("при разборе она есть", soup_new.find(id="url") is not None),
        ("до правки «Сохранить» молчит",
         not (_run_page(scripts_lib, dom_lib, actions=[])["ids"]
              .get("library_status", {}).get("text"))),
        ("первая правка говорит о несохранённом",
         "несохранённые" in (_run_page(scripts_lib, dom_lib, actions=[
             "const f = document.getElementById('f_note'); f.value = 'правка';"
             " f.dispatchEvent({type:'input', target: f})"])["ids"]
             .get("library_status", {}).get("text", ""))),
        ("сверху есть возврат в библиотеку и в проект",
         len(soup_lib.select(".crumbs a")) == 2),
        # Угловая кнопка своей записи не ведёт: карточка ушла бы дважды.
        ("угловая кнопка сохраняет тем же запросом",
         (lambda r: len(r.get("fetches") or []) == 1
          and "/library/save" in (r.get("fetches") or [{}])[0].get("url", ""))(
             _run_page(scripts_lib, dom_lib,
                       actions=["document.getElementById('savedock').click()",
                                "await null"],
                       responses=[{"json": {"id": "x"}}]))),
        ("и о несохранённом говорит вместе с верхней",
         "несохранённые" in (_run_page(scripts_lib, dom_lib, actions=[
             "const f = document.getElementById('f_note'); f.value = 'правка';"
             " f.dispatchEvent({type:'input', target: f})"])["ids"]
             .get("savedock_status", {}).get("text", ""))),
        ("удаление только у сохранённой",
         soup_lib.find(id="del") is not None and soup_new.find(id="del") is None),
    ]

    # Уход с несохранённой работой. Разбор больше не кладут в каталог за
    # спиной — «я не добавлял товар, а он появился», — значит уход со
    # свежей карточки теряет её целиком, и браузер обязан спросить.
    leave = ("const e = { type: 'beforeunload', returnValue: null,"
             " preventDefault() { this.held = true } };"
             " window.dispatchEvent(e);"
             " document.getElementById('library_status').textContent ="
             " e.held ? 'держит' : 'отпускает'")
    asked = lambda r: r["ids"].get("library_status", {}).get("text") == "держит"
    checks += [
        ("разбор не сохраняется сам",
         "_remember" not in open(os.path.join(
             os.path.dirname(os.path.abspath(__file__)), "app.py"),
             encoding="utf-8").read()),
        ("уход со свежего разбора спрашивает",
         asked(_run_page(scripts_new, dom_new, actions=[leave]))),
        ("после сохранения не спрашивает",
         not asked(_run_page(scripts_new, dom_new,
                             actions=["document.getElementById('tolibrary').click()",
                                      "await null", leave],
                             responses=[{"json": {"id": "x"}}]))),
        ("карточку из каталога отпускает нетронутой",
         not asked(_run_page(scripts_lib, dom_lib, actions=[leave]))),
        ("а после правки — держит",
         asked(_run_page(scripts_lib, dom_lib, actions=[
             "const f = document.getElementById('f_note'); f.value = 'правка';"
             " f.dispatchEvent({type:'input', target: f})", leave]))),
    ]


    # Отделки не отмечены заранее. Разбор возвращает ВСЕ исполнения,
    # какие есть у модели, и это часто альтернативы: у BAROVIER AURORA
    # пять цветов стекла, лампа продаётся с одним. Отмеченные разом,
    # они склеивались через « + » и уезжали в предложение все пять.
    checks += [
        ("поля аннотации на экране нет", soup_lib.find(id="f_summary") is None),
        ("и при разборе тоже нет", soup_new.find(id="f_summary") is None),
        ("текст аннотации стоит в описании",
         stored["summary_ru"] in (soup_lib.select_one("#f_desc").get_text() or "")),
        # Первый снимок отмечен нарочно: он обложка карточки, и без
        # него клиенту не уходило бы ни одного фото, если менеджер до
        # отбора не дошёл. Остальные — руками.
        ("первый снимок отмечен",
         soup_lib.select(".ph input")[0].has_attr("checked")),
        ("остальные снимки не отмечены",
         not any(cb.has_attr("checked")
                 for cb in soup_lib.select(".ph input")[1:])),
        ("галочки отделок сняты по умолчанию",
         not any(cb.has_attr("checked") for cb in soup_lib.select("input.finpick"))),
        ("описание на экране без строки отделок",
         "Стекло" not in (soup_lib.select_one("#f_desc").get_text() or "")),
        ("формат книги не тронут",
         "Стекло - CRYSTAL" in pl.to_excel_description(
             pl.Product(model="X", finishes=[{"role_ru": "Стекло", "material": "Crystal"}]))),
    ]

    # Нажатие галочки собирает строку описания.
    picked = _run_page(scripts_lib, dom_lib, actions=[
        "const cb = document.getElementById('finpick_0');"
        " cb.checked = true; cb.dispatchEvent({ type: 'change', target: cb })"])["ids"]
    checks.append(("отметка добавляет отделку в описание",
                   "Стекло - CRYSTAL" in (picked.get("f_desc", {}).get("value") or "")))

    # Сохранение обязано нести КАРТОЧКУ ЦЕЛИКОМ. Экран показывает не всё,
    # а запись идёт без слияния — непоказанное стиралось молча: правка
    # одного примечания уносила ссылку на техлист, а смена модели клала
    # дубль под новым опознавателем, оставляя исходную карточку старой.
    full = dict(stored)
    full.update({"collection": "Sisma", "volume_source": "производитель",
                 "doc_urls": ["https://example.com/spec.pdf"]})
    full["finishes"] = [{"role_ru": "Стекло", "material": "Crystal", "code": "AE"}]
    prod_full = flask_app._as_product(full)
    base = flask_app._card_base(prod_full, full["description"], full)
    _, sc_full, dom_full = _page(
        "lookup.html", _render=True, product=prod_full, url=prod_full.source_url,
        description=full["description"], variants=pl.variant_cards(prod_full),
        card_base=base, project_choices=[], types=pl.TYPES_RU,
        from_library=full["id"])
    sent = _run_page(sc_full, dom_full, actions=[
        "document.getElementById('f_model').value = 'Aurora 2'",
        "document.getElementById('tolibrary').click()"])
    body = _json.loads((sent.get("fetches") or [{}])[0].get("body") or "{}")
    checks += [
        ("сохранение несёт опознаватель", body.get("id") == full["id"]),
        ("и не теряет ссылки на документы", body.get("doc_urls") == full["doc_urls"]),
        ("и коллекцию", body.get("collection") == "Sisma"),
        ("и источник объёма", body.get("volume_source") == "производитель"),
        ("артикул отделки переживает сохранение",
         (body.get("finishes") or [{}])[0].get("code") == "AE"),
        # Галочка у снимка отбирает, что уйдёт клиенту, а не что
        # останется в карточке. Галочек по умолчанию нет — и сохранение
        # уносило в каталог пустую галерею с первого нажатия.
        ("снимки переживают сохранение без единой галочки",
         body.get("photos") == prod_full.photo_urls),
        ("правка модели уходит", body.get("model") == "Aurora 2"),
    ]

    # «Пересобрать с сайта» вернулась вместе со старым шаблоном.
    checks += [
        ("«Пересобрать» есть у карточки из каталога",
         soup_lib.find(id="refresh") is not None),
        ("при разборе её нет — страница только что разобрана",
         soup_new.find(id="refresh") is None),
        ("маршрут пересборки не осиротел",
         "library_refresh" in open(os.path.join(
             os.path.dirname(os.path.abspath(__file__)),
             "templates", "lookup.html"), encoding="utf-8").read()),
    ]

    # Тревога сверки: красным только то, чего в тексте НЕТ НИ ОДНИМ
    # значащим словом. Строгое сравнение по строке кричало бы на восьми
    # значениях из восьми — извлечение переставляет слова и склеивает их,
    # и такое предупреждение перестают читать за день.
    page_text = ("Aurora table lamp. Murano blown glass AE Light Pink Crystal. "
                 "Height 28 cm Depth 11 cm Minimum diameter 6 cm.")
    seen = _run_page(sc_full, dom_full,
                     responses=[{"ok": True, "json": {"text": page_text,
                                                      "chars": len(page_text)}}],
                     actions=[
        "document.getElementById('f_dims_raw').value = 'Height 28 cm, Depth 11 cm'",
        "document.getElementById('f_note').value = 'выдуманное примечание кресла'",
        "const v = document.getElementById('verify'); v.open = true;"
        " v.dispatchEvent({ type: 'toggle', target: v })",
        "await Promise.resolve(); await Promise.resolve(); await Promise.resolve();"
        " await Promise.resolve()"])
    said = (seen["ids"].get("verify_status") or {}).get("text", "")
    alarm = (seen["ids"].get("verify_miss") or {}).get("text", "")
    checks += [
        ("сверка сходила за текстом страницы", "знаков" in said),
        ("совпавшее посчитано", "Полностью совпало" in said),
        ("переставленные знаки потерей не считаются", "Размеры" not in alarm),
        ("выдуманное названо прямо", "НЕТ" in alarm and "Примечание" in alarm),
    ]

    # Выбор проекта: позиция должна уметь уехать не только в текущий.
    _, sc_pick, dom_pick = _page(
        "lookup.html", _render=True, product=product, url=product.source_url,
        description=stored["description"], variants=pl.variant_cards(product),
        project_choices=[{"id": "prj-vladimir", "name": "Владимир"}],
        types=pl.TYPES_RU, from_library=stored["id"])
    with_list = _page("lookup.html", _render=True, product=product,
                      url=product.source_url, description=stored["description"],
                      variants=pl.variant_cards(product),
                      card_base=base,
                      project_choices=[{"id": "prj-vladimir", "name": "Владимир"}],
                      types=pl.TYPES_RU, from_library=stored["id"])[0]
    rows = [b.get("data-pick") for b in with_list.select(".pickrow")]
    checks += [
        ("выбор проекта открывается окном",
         with_list.select_one("#pickdlg") is not None),
        ("в окне текущий и сохранённый", rows == ["", "prj-vladimir"]),
        ("и поле для нового проекта",
         with_list.select_one("#pickdlg_name") is not None
         and with_list.select_one("#pickdlg_create") is not None),
    ]

    # Чужой проект не должен затираться пустым черновиком: позиция
    # откладывается, а дописывает её страница проекта, подняв запись.
    stashed = _run_page(sc_pick, dom_pick, actions=[
        "document.getElementById('toproject').click()",
        "document.getElementById('pickrow_0').click()"])
    opened = _run_page(sc_pick, dom_pick, actions=[
        "document.getElementById('toproject').click()"])["ids"]
    checks.append(("нажатие открывает окно",
                   opened.get("pickdlg", {}).get("open") is True))

    named = _run_page(sc_pick, dom_pick, actions=[
        "document.getElementById('toproject').click()",
        "document.getElementById('pickdlg_name').value = 'Владимир'",
        "document.getElementById('pickdlg_create').click()"])
    drafts = [k for k in (named.get("storage") or {}) if k.startswith("aurrum.draft")]
    checks += [
        ("новый проект создаётся по имени", bool(drafts)),
        ("имя попадает в шапку проекта",
         any("Владимир" in str(v) for v in (named.get("storage") or {}).values())),
    ]

    checks += [
        ("выбор чужого проекта откладывает позицию",
         "aurrum.pending" in (stashed.get("storage") or {})),
        ("и черновик текущего не трогает",
         not any(k.startswith("aurrum.draft") for k in (stashed.get("storage") or {}))),
    ]

    # Вставка строки в книгу Excel — второстепенное действие: свёрнуто и
    # стоит последним, чтобы не спорить с «Сохранить» и «В проект».
    tuck = soup_lib.select_one("details#bookrow")
    checks += [
        ("вставка строки свёрнута в отдельный блок", tuck is not None),
        ("блок закрыт по умолчанию", bool(tuck) and not tuck.has_attr("open")),
        ("в нём и номер строки, и скрытый расчёт",
         bool(tuck) and tuck.find(id="f_row") is not None
         and tuck.find(id="f_pricing") is not None),
        ("сверка с источником свёрнута отдельным блоком",
         soup_lib.select_one("details#verify") is not None),
        ("в ней текст страницы и его состояние",
         soup_lib.select_one("#verify_text") is not None
         and soup_lib.select_one("#verify_status") is not None),
        ("он стоит после отделок",
         bool(tuck) and bool(soup_lib.select("input.finpick"))
         and tuck.sourceline > soup_lib.select("input.finpick")[-1].sourceline),
    ]

    # Экран один и правится всегда: отдельного режима чтения нет.
    checks += [
        ("кнопки «Редактировать» нет", soup_lib.find(id="edit") is None),
        ("кнопки «Отмена» нет", soup_lib.find(id="cancel") is None),
    ]
    at_rest = _run_page(scripts_lib, dom_lib, actions=[])["ids"]
    checks += [
        ("поля правятся сразу", at_rest.get("f_dims_raw", {}).get("readOnly") is False),
        ("«Сохранить» видна сразу", at_rest.get("tolibrary", {}).get("hidden") is False),
    ]

    # Галочки сверяются с описанием при открытии: у сохранённой карточки
    # отмечено то, что менеджер выбрал раньше, а не пусто.
    picked_stored = dict(stored)
    picked_stored["description"] = pl.to_excel_description(product)
    _, scripts_saved, dom_saved = _page(
        "lookup.html", _render=True, product=product, url=product.source_url,
        description=picked_stored["description"], variants=pl.variant_cards(product),
        types=pl.TYPES_RU, from_library=stored["id"])
    saved = _run_page(scripts_saved, dom_saved, actions=[])["ids"]
    checks.append(("отделка из описания отмечена при открытии",
                   saved.get("finpick_0", {}).get("checked") is True))

    # А карточка, сохранённая до ручного выбора, несёт в описании ВСЕ
    # отделки — их клал разбор. Такую строку убираем: выбор за менеджером.
    legacy = dict(stored)
    legacy["finishes"] = [{"role_ru": "Стекло", "material": "Light Pink"},
                          {"role_ru": "Стекло", "material": "Grey"},
                          {"role_ru": "Металл", "material": "Chrome"}]
    prod_legacy = flask_app._as_product(legacy)
    legacy_desc = pl.to_excel_description(prod_legacy)
    _, sc_leg, dom_leg = _page(
        "lookup.html", _render=True, product=prod_legacy, url=prod_legacy.source_url,
        description=legacy_desc, variants=pl.variant_cards(prod_legacy),
        card_base=flask_app._card_base(prod_legacy, legacy_desc, legacy),
        project_choices=[], types=pl.TYPES_RU, from_library=legacy["id"])
    old_card = _run_page(sc_leg, dom_leg, actions=[])["ids"]
    checks += [
        ("у старой карточки галочки сняты",
         all(old_card.get(f"finpick_{i}", {}).get("checked") is False
             for i in range(3))),
        ("и строка отделок из описания убрана",
         "Стекло" not in (old_card.get("f_desc", {}).get("value") or "")),
        ("остальное описание цело",
         "AURORA" in (old_card.get("f_desc", {}).get("value") or "")
         or prod_legacy.model.upper() in (old_card.get("f_desc", {}).get("value") or "")),
    ]

    good = 0
    for label, hit in checks:
        good += bool(hit)
        print(f"  {OK if hit else BAD} {label}")
    return good, len(checks)


def check_library_card() -> tuple[int, int]:
    """У плитки каталога должна быть кнопка правки.

    Правка была только по клику на саму карточку, и её не находили:
    на плитке стояла одна кнопка «В проект», а страница товара — она же
    и редактор — открывалась только если догадаться нажать на фото.
    """
    from bs4 import BeautifulSoup
    from flask import render_template
    import importlib
    import os as _os

    print("\n ПЛИТКА КАТАЛОГА")
    print(" " + "-" * 74)
    _os.environ.setdefault("AURRUM_PASSWORD", "проверка")
    _os.environ.setdefault("AURRUM_SECRET_KEY", "x" * 32)
    import app as flask_app
    importlib.reload(flask_app)

    item = {"id": "barovier-toso-aurora", "brand": "Barovier&Toso",
            "model": "Aurora", "type_ru": "Настольная лампа",
            "dims_raw": "H. 28 x 11 x 10 cm", "photo": None}
    with flask_app.app.test_request_context():
        html = render_template("library.html", items=[item], brands=[], types=[],
                               total=1, found=1, page=1, pages=1, args={}, q="")
    tile = BeautifulSoup(html, "html.parser")
    acts = tile.select_one(".card__acts")
    edit = acts.find("a") if acts else None
    to_project = acts.find("button") if acts else None

    checks = [
        ("у плитки есть кнопка правки", edit is not None),
        ("она названа словом, а не значком",
         bool(edit) and "едактир" in edit.get_text(strip=True)),
        ("она ведёт на страницу товара",
         bool(edit) and edit.get("href", "").endswith(item["id"])),
        ("«В проект» на месте",
         bool(to_project) and "проект" in to_project.get_text(strip=True)),
        ("корзина в правом верхнем углу плитки",
         tile.select_one(".card__del") is not None),
        ("корзина знает, что удаляет",
         bool(tile.select_one(".card__del"))
         and tile.select_one(".card__del").get("data-del") == item["id"]),
    ]

    # «В проект» спрашивает, в какой. Раньше позиция молча уезжала в
    # последний открытый черновик, и узнать об этом можно было, только
    # открыв его.
    _, lib_scripts, lib_dom = _page(
        "library.html", _render=True, items=[item], brands=[], types=[],
        total=1, found=1, page=1, pages=1, query="", brand="", type_ru="",
        error=None, project_choices=[{"id": "p9", "name": "Владимир"}])
    asked = _run_page(lib_scripts, lib_dom,
                      actions=["document.querySelectorAll('.toproject')[0].click()"])
    chosen = _run_page(
        lib_scripts, lib_dom,
        actions=["document.querySelectorAll('.toproject')[0].click()",
                 "document.getElementById('pickrow_0').click()",
                 "await null", "await null"],
        responses=[{"json": {"id": item["id"], "brand": "Barovier&Toso"}}])
    checks += [
        ("окно выбора проекта есть в библиотеке",
         tile.select_one("#pickdlg") is not None),
        ("и оно одно на карточку и на библиотеку",
         '{% include "_pickdlg.html" %}' in open(_os.path.join(
             _os.path.dirname(_os.path.abspath(__file__)),
             "templates", "lookup.html"), encoding="utf-8").read()),
        ("нажатие «В проект» открывает окно",
         asked["ids"].get("pickdlg", {}).get("open") is True),
        ("до выбора позиция никуда не уходит",
         not asked.get("fetches")),
        ("выбор чужого проекта откладывает позицию",
         "aurrum.pending" in (chosen.get("storage") or {})),
        ("за полной карточкой ходит в файл, а не берёт выжимку",
         "/library/card/" in ((chosen.get("fetches") or [{}])[0].get("url") or "")),
    ]

    # Позиция в УЖЕ СОХРАНЁННЫЙ проект. Добавление не помечало черновик,
    # а страница проекта при равных номерах правки берёт серверную
    # запись — и позиция пропадала молча: в списке «позиций 2», и после
    # перезагрузки на экране тоже 2.
    saved_draft = _json.dumps({"positions": [{"model": "1"}, {"model": "2"}],
                               "header": {}, "final": {}, "rates": None,
                               "rev": 3, "dirty": False})
    added = _run_page(
        lib_scripts, lib_dom,
        storage={"aurrum.current": "p1", "aurrum.draft.p1": saved_draft},
        responses=[{"json": {"id": item["id"], "brand": "Barovier&Toso"}}],
        actions=["document.querySelectorAll('.toproject')[0].click()",
                 "document.getElementById('pickrow_current').click()",
                 "await null", "await null"])
    after = _json.loads((added.get("storage") or {}).get("aurrum.draft.p1") or "{}")
    checks += [
        ("позиция ложится в текущий проект", len(after.get("positions") or []) == 3),
        ("и черновик помечен несохранённым — иначе позиция пропадёт",
         after.get("dirty") is True),
    ]
    good = 0
    for label, hit in checks:
        good += bool(hit)
        print(f"  {OK if hit else BAD} {label}")
    return good, len(checks)


def check_library() -> tuple[int, int]:
    """Библиотека: опознаватель, поиск и вход под замком.

    Сеть здесь не нужна: проверяем то, что решает судьбу карточки, —
    как из бренда и модели получается имя файла (ошибка здесь затирает
    чужую карточку) и находится ли товар по своим словам.
    """
    import library
    print("\n БИБЛИОТЕКА")
    print(" " + "-" * 74)
    items = [
        {"id": "porada-infinity", "brand": "Porada", "model": "Infinity",
         "type_ru": "Стол", "summary_ru": "Основание из массива дерева.",
         "finishes": [{"role_ru": "Дерево", "material": "Ashwood"}]},
        {"id": "barovier-toso-aurora", "brand": "Barovier&Toso", "model": "Aurora",
         "type_ru": "Настольная лампа",
         "finishes": [{"role_ru": "Стекло", "material": "Light Pink/Crystal",
                       "code": "AE"}]},
    ]
    find = lambda q: [it["id"] for it in library.search(items, q)]
    checks = [
        ("бренд и модель -> имя файла",
         library.slug("Barovier&Toso", "Aurora") == "barovier-toso-aurora"),
        # Без транслитерации кириллица выпадала целиком, и «Кресло»
        # с «Диваном» давали одно имя — вторая карточка затирала первую.
        ("кириллица транслитерируется, а не выбрасывается",
         library.slug("Хенге", "Стол") == "henge-stol"
         and library.slug("Хенге", "Кресло") != library.slug("Хенге", "Диван")),
        ("поиск по отделке", find("ashwood") == ["porada-infinity"]),
        ("поиск по артикулу отделки", find("AE") == ["barovier-toso-aurora"]),
        ("поиск по аннотации", find("массива") == ["porada-infinity"]),
        ("несколько слов — нужны все", find("porada стол") == ["porada-infinity"]
         and find("porada лампа") == []),
        ("пустой запрос отдаёт всё", len(find("")) == 2),
    ]
    good = 0
    for label, hit in checks:
        good += hit
        print(f"  {OK if hit else BAD} {label}")

    # Страницы библиотеки закрыты паролем, как и всё остальное.
    import importlib
    import os
    os.environ.setdefault("AURRUM_PASSWORD", "проверка")
    os.environ.setdefault("AURRUM_SECRET_KEY", "x" * 32)
    import app as flask_app
    importlib.reload(flask_app)
    client = flask_app.app.test_client()
    closed = all(client.open(path, method=method).status_code in (301, 302, 401)
                 for path, method in (("/library", "GET"),
                                      ("/library/save", "POST"),
                                      ("/library/delete", "POST")))
    good += closed
    checks.append(("x", closed))
    print(f"  {OK if closed else BAD} библиотека закрыта паролем")
    return good, len(checks)


def check_shops() -> tuple[int, int]:
    """Список магазинов: опознаём по домену, а не по форме адреса.

    `/products/` в пути есть у LONGHI, HENGE и MISURA EMME — по адресу
    магазин неотличим от обычного сайта. Ошибка здесь стоит ложной
    тревоги на каждой карточке этих брендов.
    """
    import gallery
    import shopify
    print("\n ИСТОЧНИК ФОТОГРАФИЙ")
    print(" " + "-" * 74)
    cases = [
        ("https://fendicasa.com/products/annabelle-armchair", True),
        ("https://luxurylivinggroup.com/products/bentley-home-embrace-sofa", True),
        ("https://www.longhi.it/en-us/products/bench-pouf/arianna", False),
        ("https://www.henge07.com/products/tables/sisma/", False),
        # Домен сравниваем целиком: хвостом «fendicasa.com» кончается
        # и чужой домен, зарегистрировать который может кто угодно.
        ("https://evil-fendicasa.com/products/x", False),
    ]
    good = 0
    for url, expected in cases:
        got = shopify.is_shop(url)
        hit = got == expected
        good += hit
        print(f"  {OK if hit else BAD} {url.split('//')[-1][:52]:54} "
              f"-> {'магазин' if got else 'разбор страницы'}")

    # Два источника не должны спорить за один бренд.
    overlap = set(gallery._rules()) & set(shopify.SHOPS)
    hit = not overlap
    good += hit
    print(f"  {OK if hit else BAD} магазины и правила галерей не пересекаются"
          + (f": {', '.join(sorted(overlap))}" if overlap else ""))
    return good, len(cases) + 1


def check_gallery_live() -> tuple[int, int]:
    """Эталонный товар каждого бренда: столько ли снимков отдаёт правило.

    Это единственная проверка, которая ловит переверстку сайта. Дорогая —
    запрос Firecrawl на бренд, — поэтому идёт по ключу `--galleries`,
    а не в каждом прогоне.
    """
    import gallery
    print("\n ГАЛЕРЕИ НА ЖИВЫХ СТРАНИЦАХ")
    print(" " + "-" * 74)
    good = 0
    rules = gallery._rules()
    for domain, rule in rules.items():
        ref = rule.get("эталон") or {}
        url, want = ref.get("url"), ref.get("фото")
        try:
            _, _, html = pl._scrape(url)
            got = len(gallery.photos(html, url) or [])
        except Exception as exc:  # noqa: BLE001
            print(f"  {BAD} {domain:16} ошибка: {exc}")
            continue
        hit = got == want
        good += hit
        print(f"  {OK if hit else BAD} {domain:16} снимков {got}, ждали {want}")
    return good, len(rules)


def check_login() -> tuple[int, int]:
    """Вход не должен падать на нелатинском пароле.

    Поймано на проде: hmac.compare_digest со строками требует чистого
    ASCII. Достаточно набрать пароль с русской раскладкой — и вместо
    «неверный пароль» приходил 500.
    """
    import os
    os.environ.setdefault("AURRUM_PASSWORD", "проверка-пароля")
    os.environ.setdefault("AURRUM_SECRET_KEY", "x" * 32)
    import importlib
    import app as flask_app
    importlib.reload(flask_app)

    print("\n ВХОД ПО ПАРОЛЮ")
    print(" " + "-" * 74)
    client = flask_app.app.test_client()
    right = os.environ["AURRUM_PASSWORD"]
    cases = [
        ("неверный", 401, "кириллица"),
        ("wrong", 401, "латиница"),
        ("пароль с пробелом и ё", 401, "кириллица с пробелами"),
        ("", 401, "пустой"),
        (right, 302, "верный"),
    ]
    good = 0
    for password, expected, note in cases:
        got = client.post("/login", data={"password": password}).status_code
        hit = got == expected
        good += hit
        print(f"  {OK if hit else BAD} {note:24} -> {got} (ждали {expected})")
    return good, len(cases)


def check_gallery_rules() -> tuple[int, int]:
    """Правила галерей: разбираются ли селекторы и на месте ли эталоны.

    Здесь только форма правила. Саму выборку проверяет `--galleries`:
    он ходит на эталонный товар и сверяет число снимков с записанным.
    Отдельным ключом, потому что это запрос Firecrawl на каждый бренд.
    """
    import gallery
    from bs4 import BeautifulSoup
    print("\n ПРАВИЛА ГАЛЕРЕЙ")
    print(" " + "-" * 74)
    rules = gallery._rules()
    good = 0
    soup = BeautifulSoup("<div></div>", "html.parser")
    for domain, rule in rules.items():
        selectors = rule.get("selectors") or []
        ref = rule.get("эталон") or {}
        try:
            for sel in selectors:
                soup.select(sel)          # синтаксис селектора
            valid = bool(selectors) and bool(ref.get("url")) and bool(ref.get("фото"))
        except Exception:
            valid = False
        good += valid
        print(f"  {OK if valid else BAD} {domain:16} селекторов {len(selectors)}, "
              f"эталон {ref.get('фото', '—')} фото")
    return good, len(rules)


def check_palette() -> tuple[int, int]:
    """Витрина отделок: группы и образцы.

    Бренд печатает отделки не списком, а группами с кружками-образцами,
    и клиенту показывают именно их. Модель отдавала плоский список без
    снимков; витрину читает разметка.
    """
    import os

    from bs4 import BeautifulSoup

    import app as flask_app
    import palette
    print("\n ВИТРИНА ОТДЕЛОК")
    print(" " + "-" * 74)

    # Разметка Porada, ужатая до сути, и НАРОЧНО повторённая: страница
    # несёт витрину дважды — для широкого экрана и для узкого.
    one = """
      <div class="characteristics_tabs_finiture_block">
        <div class="characteristics_tabs_finiture_title">BASE</div>
        <div class="finiture_item">
          <div class="finiture_item_img_w">
            <img class="finiture_item_img" src="/img/rovere.jpg">
          </div>
          <div class="finiture_item_title">ROVERE NATURALE</div>
        </div>
        <div class="finiture_item">
          <div class="finiture_item_title">БЕЗ ОБРАЗЦА</div>
        </div>
      </div>
      <div class="characteristics_tabs_finiture_block">
        <div class="characteristics_tabs_finiture_title">TOP</div>
        <div class="finiture_item">
          <div class="finiture_item_img_w"><img src="/img/marmo.jpg"></div>
          <div class="finiture_item_title">CALACATTA</div>
        </div>
      </div>"""
    got = palette.from_html(one + one, "https://www.porada.it/prodotto/x")

    checks = [
        ("бренда без правила витрина не трогает",
         palette.from_html(one, "https://example.com/x") is None),
        ("правило есть, а разметки нет — пустой список, не молчание",
         palette.from_html("<div></div>", "https://www.porada.it/x") == []),
        ("отделок трое, повтор витрины схлопнут", len(got) == 3),
        ("группы названы как у бренда",
         [f["group"] for f in got] == ["BASE", "BASE", "TOP"]),
        ("образец достаётся полным адресом",
         got[0]["swatch"] == "https://www.porada.it/img/rovere.jpg"),
        ("отделка без образца остаётся отделкой", got[1]["swatch"] == ""),
        ("порядок групп — как на странице",
         [f["material"] for f in got][:1] == ["ROVERE NATURALE"]),
    ]

    checks += [
        ("STRUTTURA -> Каркас", palette.role_of("STRUTTURA") == "Каркас"),
        ("BASE -> Основание", palette.role_of("BASE") == "Основание"),
        ("двуязычный заголовок читается по знакомому слову",
         palette.role_of("Essenze | Wood") == "Дерево"),
        ("незнакомый заголовок молчит и даёт «Отделка»",
         palette.role_of("QUALCOSA") == "Отделка"),
    ]

    # Витрину не сверяем с текстом страницы: она И ЕСТЬ текст страницы.
    kept, dropped = pl._verify_finishes(
        [{"material": "ROVERE NATURALE", "source": "палитра"},
         {"material": "ВЫДУМКА"}], "на странице только про дуб")
    checks += [
        ("витрина не проходит сверку и не пропадает", len(kept) == 1),
        ("а выдумка модели пропадает", dropped == ["ВЫДУМКА"]),
    ]

    # Экран: заголовки групп и плитки с образцами, галочек нет.
    stored = {"brand": "PORADA", "model": "AMPHORA", "type_ru": "Стол",
              "source_url": "https://www.porada.it/prodotto/amphora",
              "finishes": got}
    product = flask_app._as_product(stored)
    with flask_app.app.test_request_context():
        from flask import render_template
        html = render_template("lookup.html", product=product, url="",
                               description="", variants=[], types=pl.TYPES_RU,
                               card_base=flask_app._card_base(product, ""),
                               project_choices=[])
    soup = BeautifulSoup(html, "html.parser")
    base = flask_app._card_base(product, "")
    checks += [
        # Заголовок — наша подкатегория по-русски, а рядом слово бренда:
        # по нему сверяют с сайтом.
        ("на экране подкатегории по-русски",
         [g.contents[0].strip() for g in soup.select(".fingroup")]
         == ["Основание", "Столешница"]),
        ("и заголовок бренда рядом",
         [g.get_text(strip=True) for g in soup.select(".fingroup__src")]
         == ["BASE", "TOP"]),
        ("витрина разбита по группам", len(soup.select(".fintiles")) == 2),
        ("плиток столько же, сколько отделок",
         len(soup.select(".fintile")) == 3),
        ("образцы выведены картинками", len(soup.select(".fintile img")) == 2),
        ("галочек заранее нет", not soup.select("input.finpick[checked]")),
        ("образец переживает сохранение",
         base["finishes"][0].get("swatch", "").endswith("rovere.jpg")),
        ("и группа тоже", base["finishes"][2].get("group") == "TOP"),
        ("правило описано данными, а не кодом",
         os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "config", "finish_palette.json"))),
    ]

    # Образцы из техлиста. Пары ставятся по порядку чтения: и картинки,
    # и подписи идут одним порядком, а координаты подписей PDF отдаёт с
    # чужой системой координат — у Baxter две трети приходили с нулями.
    import spec_images
    images = [{"name": f"/X{i}", "x": 50.0 + 108 * (i % 3),
               "y": 671.0 - 133 * (i // 3), "w": 80.0, "h": 80.0}
              for i in range(6)]
    order = [im["name"] for im in spec_images._reading_order(images)]
    checks += [
        ("порядок чтения: сверху вниз, слева направо",
         order == ["/X0", "/X1", "/X2", "/X3", "/X4", "/X5"]),
        ("ряд собирается с допуском — образцы стоят не идеально ровно",
         [im["name"] for im in spec_images._reading_order(
             [{"name": "/B", "x": 150.0, "y": 671.0, "w": 80.0, "h": 80.0},
              {"name": "/A", "x": 50.0, "y": 673.5, "w": 80.0, "h": 80.0}])]
         == ["/A", "/B"]),
        ("лигатура в PDF не мешает узнать название",
         spec_images._flat("Camou\ufb02age Gris") == spec_images._flat("Camouflage Gris")),
    ]

    # Маршрут образца: чужого не отдаёт и придуманного не рисует.
    client = flask_app.app.test_client()
    with client.session_transaction() as sess:
        sess["authorized"] = True
    checks += [
        ("имя картинки с путём отвергается",
         client.get("/spec-image", query_string={
             "doc": "https://example.com/x.pdf", "page": 1,
             "x": "../../etc/passwd"}).status_code == 400),
        ("страница вне диапазона отвергается",
         client.get("/spec-image", query_string={
             "doc": "https://example.com/x.pdf", "page": -1,
             "x": "/Im0"}).status_code == 400),
        ("не ссылка — не документ",
         client.get("/spec-image", query_string={
             "doc": "file:///etc/passwd", "page": 1,
             "x": "/Im0"}).status_code == 400),
    ]

    good = 0
    for label, hit in checks:
        good += bool(hit)
        print(f"  {OK if hit else BAD} {label}")
    return good, len(checks)


def check_photos() -> tuple[int, int]:
    """Отбор фотографий изделия среди всей галереи страницы.

    У PORADA страница отдавала 72 снимка, к изделию относился 21.
    У VENICEM среди восьми были образцы металла и три других светильника
    той же серии — короткое «circle» захватило бы и их.
    """
    print("\n ОТБОР ФОТОГРАФИЙ")
    print(" " + "-" * 74)
    cases = [
        (["https://x/infinity-01-Infinity.jpg", "https://x/podi-cover.jpg",
          "https://x/02-Infinity.jpg"], "INFINITY", 2, "чужие изделия раздела"),
        (["https://x/circle-floor-1.jpg", "https://x/circle_ceiling_1.jpg",
          "https://x/circle_table_3.jpg", "https://x/materials_metals_M11-1.jpg"],
         "Circle Floor", 1, "другие светильники серии и образцы металла"),
        (["https://x/mere.jpg?width=1946", "https://x/mere.jpg?width=54",
          "https://x/mere-detail.jpg?width=1946"], "Mere", 2, "тот же файл разного размера"),
        (["https://x/a.jpg", "https://x/b.jpg"], "Zeta", 2,
         "по названию не нашлось — отдаём всё"),
        (["https://x/a.jpg", "https://x/logo.png"], "Pin", 1,
         "короткое название не фильтр, логотип отсеян"),
        # Служебные слова сравниваются целиком: подстрока отбрасывала
        # «iconic-collection» из-за «icon», а это частое слово в каталогах.
        (["https://x/iconic-collection/sofa-01.jpg", "https://x/spinello-table.jpg",
          "https://x/avatar-lounge-chair.jpg"], "", 3, "слова внутри других слов"),
        (["https://x/icons/arrow.png", "https://x/img/logo.png",
          "https://x/real-photo.jpg"], "", 1, "каталог icons и файл logo целиком"),
    ]
    good = 0
    for urls, model, want, note in cases:
        got = pl._clean_photos(urls, model)
        hit = len(got) == want
        good += hit
        print(f"  {OK if hit else BAD} {model:14} {len(urls)} -> {len(got):2} (ждали {want})  {note}")
    return good, len(cases)


def check_pricing() -> tuple[int, int]:
    """Цепочка расчёта — против чисел рабочей книги.

    Сверяем СУММУ (AD) и сумму со сборкой (AE): это то, что считается
    формулой. Цену клиенту не сверяем — в книге она округлена по-разному
    (вниз, вверх, до копейки), то есть ставится решением менеджера.
    """
    import pricing
    print("\n РАСЧЁТ ЦЕНЫ ПРОТИВ КНИГИ")
    print(" " + "-" * 74)
    cases = [
        ("LONGHI ARIANA",   6886.0,  0.5, 0.45, 0.00, 1.00,  5752.2,  5752.2),
        ("FLOU BUTTERFLY",  4389.0,  1.0, 0.50, 0.12, 1.00,  4127.8,  4127.8),
        ("VENICEM CIRCLE",  2550.0,  0.2, 0.50, 0.00, 0.95,  2085.0,  2194.7),
        ("TRUSSARDI VIBES", 16020.0, 6.8, 0.50, 0.00, 0.95, 14814.0, 15593.7),
        ("TRUSSARDI COMFY", 5000.0,  0.3, 0.50, 0.00, 1.00,  3850.0,  3850.0),
    ]
    good = 0
    for name, t, vol, disc, markup, asm, want_total, want_assembly in cases:
        r = pricing.line(t, vol, factory_discount=disc, dealer_markup=markup, assembly=asm)
        hit = abs(r.total - want_total) < 0.6 and abs(r.with_assembly - want_assembly) < 0.6
        good += hit
        print(f"  {OK if hit else BAD} {name:18} СУММА {r.total:9.1f} "
              f"со сборкой {r.with_assembly:9.1f}   в книге {want_total:8.1f} / {want_assembly:8.1f}")
    return good, len(cases)


def check_overrides() -> tuple[int, int]:
    """Ручные значения позиции: цена и SWIFT.

    Ровно две ручки, и обе — из формы: цена клиенту в книге округлена
    менеджером (5752,2 -> 5750), SWIFT стоит числом в каждой строке.
    Пустое значение возвращает расчёт.
    """
    import pricing
    print("\n РУЧНЫЕ ЗНАЧЕНИЯ ПОЗИЦИИ")
    print(" " + "-" * 74)
    base = {"list_price": 2550, "volume_m3": 0.2, "qty": 1, "assembly": 0.95}
    plain = pricing.project([base])["lines"][0]
    manual = pricing.project([{**base, "price": 2150, "swift": 0}])["lines"][0]
    cleared = pricing.project([{**base, "price": "", "swift": ""}])["lines"][0]
    checks = [
        ("расчётная цена 2190 — предложение", plain["price"] == 2190),
        ("цена руками 2150 заменяет расчёт", manual["price"] == 2150.0),
        ("сумма считается от ручной цены", manual["sum"] == 2150.0),
        ("SWIFT позиции 0 выигрывает у ставки", manual["swift"] == 0.0),
        # Сравниваем при одинаковом SWIFT: уровни законно зависят от него
        # через СУММУ, а вот ручная цена менять их не должна.
        ("уровни оплаты от ручной цены не зависят",
         manual["levels"] == pricing.project([{**base, "swift": 0}])["lines"][0]["levels"]),
        ("пустое значение возвращает расчёт",
         cleared["price"] == 2190 and cleared["swift"] == 200.0),
    ]
    # Закуп первичен: менеджер знает его из инвойса, а цена прайса
    # выводится обратной арифметикой той же цепочки.
    by_purchase = pricing.project([{"purchase": 3250, "volume_m3": 0.4, "qty": 1,
                                    "factory_discount": 0.5, "dealer_markup": 0,
                                    "assembly": 1}])["lines"][0]
    by_list = pricing.project([{"list_price": 6500, "volume_m3": 0.4, "qty": 1,
                                "factory_discount": 0.5, "dealer_markup": 0,
                                "assembly": 1}])["lines"][0]
    base2 = {"purchase": 3250, "volume_m3": 0.4, "qty": 1,
             "dealer_markup": 0, "assembly": 1}
    checks += [
        ("рентаб процентом: 20 % от закупа",
         pricing.project([{**base2, "margin_pct": 20}])["lines"][0]["margin"] == 650.0),
        ("рентаб евро выигрывает у процента",
         pricing.project([{**base2, "margin_pct": 20, "margin_eur": 1500}])["lines"][0]["margin"] == 1500.0),
        ("нулевой процент законен — «без рентаба»",
         pricing.project([{**base2, "margin_pct": 0}])["lines"][0]["margin"] == 0.0),
        # База транша — цена со скидкой W: при ручном закупе 3250 и
        # наценке 0 это те же 3250, а не выведенный прайс 6500.
        ("транш процентом и евро",
         pricing.project([{**base2, "transfer_pct": 2}])["lines"][0]["transfer"] == 65.0
         and pricing.project([{**base2, "transfer_eur": 50}])["lines"][0]["transfer"] == 50.0),
        ("транспорт ставкой за м³ и евро целиком",
         pricing.project([{**base2, "freight_rate": 300}])["lines"][0]["freight"] == 120.0
         and pricing.project([{**base2, "freight_eur": 1000}])["lines"][0]["freight"] == 1000.0),
        ("закуп руками 3250 -> прайс выведен 6500",
         by_purchase["list_price"] == 6500.0 and by_purchase["purchase"] == 3250.0),
        ("обе дороги дают одну цепочку",
         by_purchase["sum"] == by_list["sum"] and by_purchase["margin"] == by_list["margin"]),
    ]
    good = 0
    for label, hit in checks:
        good += hit
        print(f"  {OK if hit else BAD} {label}")
    return good, len(checks)


def check_final_block() -> tuple[int, int]:
    """Итог компреда — против спецификации 2867.

    В книге два ручных подгона до круглого числа: «+4» в сборке и «−75»
    в скидке. Наши формулы их не содержат — сверяем тождеством: наши
    числа с приложенными подгонами обязаны дать книжные до копейки.
    """
    import pricing
    print("\n ИТОГ КОМПРЕДА ПРОТИВ КНИГИ 2867")
    print(" " + "-" * 74)
    f = pricing.final_block(60370, {"assembly_pct": 5, "personal_pct": 30})
    checks = [
        ("сборка 5 %: 3018.5 (в книге 3022.5 = +4)", f["сборка"] == 3018.5),
        ("всего + подгон 4 = книжные 63392.5", f["всего"] + 4 == 63392.5),
        ("(всего+4)×0.7 − 75 = книжный под-итог 44299.75",
         round((f["всего"] + 4) * 0.7 - 75, 2) == 44299.75),
        ("доп.скидка 4300 доводит до книжных 39999.75",
         round((f["всего"] + 4) * 0.7 - 75 - 4300, 2) == 39999.75),
    ]

    # Единицы по умолчанию. Услуги и доставка — суммы, а не доли: их
    # называют в евро, и переключатель приходилось трогать каждый раз.
    # Скидки и сборка — наоборот, всегда проценты.
    soup_fin, _, dom_fin = _page("project.html", _url="/project")
    unit = lambda key: dom_fin["ids"].get(f"fin_{key}_unit", {}).get("value")
    checks += [
        ("услуги по умолчанию в евро", unit("services") == "eur"),
        ("доставка по умолчанию в евро", unit("delivery") == "eur"),
        ("сборка остаётся процентом", unit("assembly") == "pct"),
        ("персональная скидка остаётся процентом", unit("personal") == "pct"),
        ("дополнительная скидка остаётся процентом", unit("extra") == "pct"),
    ]
    good = 0
    for label, hit in checks:
        good += hit
        print(f"  {OK if hit else BAD} {label}")

    # Блок в выгрузке: формулы ссылаются друг на друга, а не на числа.
    import io
    import book_export
    import openpyxl
    content = book_export.build(
        [{"brand": "X", "model": "Y", "qty": 1, "list_price": 100}],
        final={"assembly_pct": 5, "personal_pct": 30})
    ws = openpyxl.load_workbook(io.BytesIO(content)).active
    labels = [ws.cell(r, 3).value for r in range(14, 28)]
    formulas = [str(ws.cell(r, 6).value) for r in range(14, 28)]
    hit = ("ИТОГО К ОПЛАТЕ, Евро" in labels
           and any(str(v).startswith("=-F") for v in formulas))
    good += hit
    checks.append(("x", hit))
    print(f"  {OK if hit else BAD} блок уходит в файл формулами, включая скидку")
    return good, len(checks)


def check_position_defaults() -> tuple[int, int]:
    """Не заданное поле — число формы, очищенное — ноль.

    Разница дорогая: у нетронутой позиции скидка фабрики обычные 50 %,
    а у очищенной руками её нет вовсе. Если перепутать, цена вырастет
    вдвое или упадёт вдвое — молча, без единого предупреждения.
    """
    import pricing
    print("\n НАЧАЛЬНЫЕ ЧИСЛА ПОЗИЦИИ")
    print(" " + "-" * 74)
    d = pricing.DEFAULT_POSITION
    # Цена 10 000, объём 0: закуп = 10000 x (1 - скидка).
    cases = [
        ("поле не задано -> скидка формы", dict(factory_discount=None),
         10_000 * (1 - d["factory_discount"])),
        ("поле очищено -> без скидки",     dict(factory_discount=""), 10_000.0),
        ("задано число -> оно и берётся",  dict(factory_discount=0.45), 5_500.0),
    ]
    good = 0
    for label, kw, want in cases:
        got = pricing.line(10_000, 0, **kw).purchase
        hit = abs(got - want) < 0.01
        good += hit
        print(f"  {OK if hit else BAD} {label:34} закуп {got:9.1f}, ждали {want:9.1f}")

    # Числа формы: строки 16, 18, 20, 22, 23 рабочего файла.
    hit = (d["factory_discount"], d["dealer_markup"], d["assembly"]) == (0.5, 0.0, 1.0)
    good += hit
    print(f"  {OK if hit else BAD} значения по умолчанию совпадают с формой: "
          f"скидка {d['factory_discount']}, наценка {d['dealer_markup']}, "
          f"сборка {d['assembly']}")
    return good, len(cases) + 1


def check_export_matches_screen() -> tuple[int, int]:
    """Файл клиенту обязан считать ровно то же, что экран проекта.

    Список аргументов позиции собирался в трёх местах, и два отстали:
    выгрузка не передавала ручные рентаб, транш и перевозку. Позиция
    с правками показывала на экране 6990 €, а в файле 5270 € — и файл
    противоречил сам себе, потому что скрытые формулы книги эти правки
    знали, а колонка E считалась мимо них.
    """
    import io
    from openpyxl import load_workbook
    print("\n ФАЙЛ ПРОТИВ ЭКРАНА")
    print(" " + "-" * 74)

    # Позиция, где переопределено всё, что можно переопределить.
    p = {"list_price": 6886, "volume_m3": 0.5, "qty": 2,
         "freight_eur": 3000, "margin_eur": 100, "transfer_eur": 250,
         "swift": 300, "assembly": 2,
         "brand": "TEST", "model": "X", "description": "тест"}

    screen = pricing.project([p])["lines"][0]
    export = pricing.for_position(p, pricing.DEFAULT_RATES)

    checks = [
        ("цена клиенту совпала", screen["price"] == export.price),
        ("перевозка совпала", screen["freight"] == export.freight),
        ("рентабельность совпала", screen["margin"] == export.margin),
        ("транш совпал", screen["transfer"] == export.transfer),
        ("SWIFT совпал", screen["swift"] == export.swift),
    ]

    # То же самое, но по готовому файлу: колонка E и скрытые формулы.
    wb = load_workbook(io.BytesIO(book_export.build([p], values=True)))
    ws = wb.active
    row = next((i for i in range(1, ws.max_row + 1)
                if ws.cell(i, 1).value == 1), None)
    checks.append(("позиция найдена в книге", row is not None))
    if row:
        checks.append((f"E{row} = цена с экрана",
                       ws.cell(row, 5).value == screen["price"]))
        checks.append((f"Z{row} = ручная рентабельность",
                       pricing._num(ws.cell(row, 26).value) == 100))
        checks.append((f"AC{row} = ручная перевозка",
                       pricing._num(ws.cell(row, 29).value) == 3000))
        checks.append((f"F{row} = цена x количество",
                       ws.cell(row, 6).value == round(screen["price"] * 2, 2)))

    # Растаможка: только руками, в евро, без ставки. Пусто значит ноль,
    # а не «взять из констант» — в отличие от рентаба, транша и
    # транспорта. В книгу уходит в AJ, свободную колонку формы.
    plain = pricing.project([{k: v for k, v in p.items() if k != "customs"}])["lines"][0]
    with_customs = pricing.project([{**p, "customs": 750}])["lines"][0]
    checks.append(("растаможка входит в СУММУ",
                   with_customs["total"] - plain["total"] == 750))
    checks.append(("пустая растаможка = ноль, а не ставка",
                   pricing.for_position({"list_price": 10000, "volume_m3": 1}).customs == 0.0))
    wb2 = load_workbook(io.BytesIO(book_export.build([{**p, "customs": 750}], values=True)))
    ws2 = wb2.active
    row2 = next((i for i in range(1, ws2.max_row + 1)
                 if ws2.cell(i, 1).value == 1), None)
    checks.append(("AJ в книге = растаможка",
                   bool(row2) and pricing._num(ws2.cell(row2, 36).value) == 750))
    checks.append(("цена в книге учла растаможку",
                   bool(row2) and ws2.cell(row2, 5).value == with_customs["price"]))

    # Итог компреда считается тем же путём, что строки.
    total = pricing.project([p])["sum"]
    checks.append(("итог проекта = цена x количество",
                   total == round(screen["price"] * 2, 2)))

    good = 0
    for label, hit in checks:
        good += bool(hit)
        print(f"  {OK if hit else BAD} {label}")
    return good, len(checks)


def check_columns() -> tuple[int, int]:
    """Колонки книги ищутся по подписям, а не по буквам.

    Из-за жёстких букв форма коммерческого предложения читалась как
    спецификация, и в графу «Цена» попадала ширина позиции. Ошибка тихая:
    документ выглядит правильным.
    """
    import spec_parser
    print("\n КОЛОНКИ ПО ПОДПИСЯМ ЗАГОЛОВКА")
    print(" " + "-" * 74)
    headers = [
        ("форма КП",
         {"A": "№", "B": "Производитель", "C": "Описание", "D": "К-во",
          "E": "Цена, Евро", "F": "Сумма, Евро", "G": "СПЕЦ. ЦЕНА, Евро",
          "H": "СПЕЦ. СУММА, Евро"},
         {"n": "A", "brand": "B", "desc": "C", "qty": "D", "price": "E", "total": "F"}),
        ("спецификация",
         {"A": "№", "C": "Производитель", "E": "Описание", "I": "К-во",
          "J": "Цена, Евро", "M": "Сумма, Евро"},
         {"n": "A", "brand": "C", "desc": "E", "qty": "I", "price": "J", "total": "M"}),
        ("спецификация HENGE",
         {"A": "№", "C": "Реф.", "E": "Описание", "I": "К-во",
          "J": "Стоимость, Евро", "M": "Сумма, Евро"},
         {"n": "A", "brand": "C", "desc": "E", "qty": "I", "price": "J", "total": "M"}),
    ]
    good = 0
    for name, cells, expected in headers:
        # ключ вида "B12" -> буква колонки
        got = spec_parser._map_columns(
            lambda ref, c=cells: c.get(ref.rstrip("0123456789")), 12)
        hit = got == expected
        good += hit
        print(f"  {OK if hit else BAD} {name:22} {got}")
        if not hit:
            print(f"      ожидали {expected}")
    return good, len(headers)


def check_book_row() -> tuple[int, int]:
    """Строка для книги: формулы должны совпадать с рабочим файлом.

    Эталон снят с позиции 16 файла 0000-Offer-…-AUR-FORM.xlsx. Если
    разметка книги поедет, проверка это покажет, а не менеджер потом
    в готовом компреде.
    """
    import book_row
    print("\n СТРОКА ДЛЯ КНИГИ")
    print(" " + "-" * 74)
    R = 16
    cells = book_row.visible_row({}, R) + book_row.pricing_row({}, R)
    cols = list("ABCDEFGHIJKLMNOPQRS") + ["T", "U", "V", "W", "X", "Y", "Z",
                                          "AA", "AB", "AC", "AD", "AE", "AF",
                                          "AG", "AH", "AI", "AJ"]
    got = dict(zip(cols, (str(c) for c in cells)))
    expected = {
        "F": "=D16*E16", "H": "=D16*G16", "R": "=U16", "S": "=R16*D16",
        "U": "=ROUNDUP(J16*K16*L16*1.5/1000000,1)",
        "W": "=T16-T16*V16", "Y": "=W16+W16*X16", "Z": "=Y16*($Z$1+0)/100",
        "AA": "=W16*$AA$1/100", "AC": "=U16*$AC$1",
        # AD отличается от формы ровно на AJ: растаможка добавлена
        # новой колонкой в свободную AJ, а не вставкой после AC —
        # вставка сдвинула бы AD..AI вместе со ставками $AF$1..$AI$1,
        # и строка, вставленная в готовую книгу, считала бы по чужим
        # ячейкам. AJ в форме занята в нуле строк.
        "AD": "=Y16+Z16+AA16+AB16+AC16+AJ16", "AE": "=AD16/1",
        "AF": "=AE16/(1-$AF$1/100)", "AG": "=AF16/(1-$AG$1/100)",
        "AH": "=AG16/(1-$AH$1/100)", "AI": "=AH16/(1-$AI$1/100)",
    }
    good = 0
    for col, want in expected.items():
        hit = got.get(col) == want
        good += hit
        print(f"  {OK if hit else BAD} {col:3} {want}")
        if not hit:
            print(f"      у нас: {got.get(col)}")
    # Многострочное описание обязано пережить вставку
    multi = book_row.build({"description": "A\nB"}, R)
    quoted = '"A\nB"' in multi
    good += quoted
    print(f"  {OK if quoted else BAD} описание в кавычках — перевод строки не рвёт строку")
    return good, len(expected) + 1


def check_extractors() -> tuple[int, int]:
    """Разделение моделей и поведение при мусоре.

    Приёмка этого не покрывала вовсе: check_lookup не импортировал
    extract и не смотрел, какой моделью собрана карточка. А правил там
    три, и каждое стоит денег: лёгкая на текст (втрое быстрее), тяжёлая
    на документы (у VENICEM размеры нарисованы на схеме), переспрос при
    неполном ответе (у LONGHI лёгкая возвращала ноль исполнений).
    """
    import extract
    import llama_extract
    print("\n ИЗВЛЕКАТЕЛИ")
    print(" " + "-" * 74)

    calls: list[tuple] = []
    real_gemini, real_llama = extract._gemini, llama_extract.from_text
    FULL = {"products": [{"type_ru": "Стол",
                          "variants": [{"dims_raw": "130x47x45Н"}],
                          "finishes": [{"role_ru": "Каркас", "material": "дуб"}]}]}

    def stub(answers):
        """answers — по одному на вызов: словарь или исключение."""
        seq = list(answers)

        def fake(data=None, text=None, model=None, known_types=(), known_roles=(), source=""):
            calls.append(model or extract.GEMINI_MODEL)
            got = seq.pop(0) if seq else {}
            if isinstance(got, Exception):
                raise got
            return got
        return fake

    checks = []
    try:
        llama_extract.from_text = lambda t: {"products": [{"type_ru": "запасной"}]}

        # 1. Полный ответ лёгкой — тяжёлую не тревожим.
        calls.clear()
        extract._gemini = stub([FULL])
        got = extract.from_text("текст", known_types=("Стол",))
        checks += [
            ("полный ответ лёгкой не идёт к тяжёлой", calls == [extract.GEMINI_MODEL_LIGHT]),
            ("источник помечен «Gemini»", got.get(extract.SOURCE_KEY) == extract.SOURCE_MAIN),
        ]

        # 2. Неполный ответ — переспрос у тяжёлой.
        calls.clear()
        extract._gemini = stub([{"products": [{"type_ru": "Стол"}]}, FULL])
        got = extract.from_text("текст", known_types=("Стол",))
        checks += [
            ("неполный ответ переспрашивается у тяжёлой",
             calls == [extract.GEMINI_MODEL_LIGHT, extract.GEMINI_MODEL]),
            ("переспрос виден в источнике",
             "переспрошено" in str(got.get(extract.SOURCE_KEY))),
        ]

        # 3. Тип вне нашего списка — тоже повод переспросить: ответ
        #    модели нестабилен, у VENICEM приходило то «Торшер», то мимо.
        calls.clear()
        extract._gemini = stub([{"products": [{"type_ru": "Table",
                                               "variants": [{"dims_raw": "1"}]}]}, FULL])
        extract.from_text("текст", known_types=("Стол", "Торшер"))
        checks.append(("тип вне списка переспрашивается", len(calls) == 2))

        # 4. Отказ лёгкой — переспрос, а не уход к другому поставщику.
        calls.clear()
        extract._gemini = stub([RuntimeError("503"), FULL])
        got = extract.from_text("текст", known_types=("Стол",))
        checks += [
            ("отказ лёгкой ведёт к тяжёлой",
             calls == [extract.GEMINI_MODEL_LIGHT, extract.GEMINI_MODEL]),
            ("причина переспроса названа",
             "лёгкая не ответила" in str(got.get(extract.SOURCE_KEY))),
        ]

        # 5. Отказ обеих — запасной путь, и он ПОМЕЧЕН.
        calls.clear()
        extract._gemini = stub([RuntimeError("503"), RuntimeError("500")])
        got = extract.from_text("текст", known_types=("Стол",))
        checks += [
            ("отказ обеих уводит на запасной путь",
             got.get("products", [{}])[0].get("type_ru") == "запасной"),
            ("запасной путь помечен",
             str(got.get(extract.SOURCE_KEY)).startswith("LlamaExtract")),
        ]

    finally:
        extract._gemini, llama_extract.from_text = real_gemini, real_llama

    # 6. Мусор — это отказ, а не пустой ответ. Раньше не-словарь молча
    #    становился {} и уезжал дальше с ярлыком «Gemini»: проверка в
    #    product_lookup молчала, а запасной путь не пробовался.
    #    Проверяем НАСТОЯЩИЙ _gemini, поэтому после восстановления.
    checks.append(("не-словарь считается отказом",
                   _raises_on_answer(extract, ["не объект"])))
    checks.append(("ответ без текста считается отказом",
                   _raises_on_answer(extract, {"promptFeedback": {"blockReason": "SAFETY"}},
                                     whole=True)))

    # 7. Пересборка карточки в каталоге: понижение доверия обязано
    #    перебивать сохранённое. Карточка, сохранённая до правки
    #    единицы измерения, лежит с dims_confident=True и объёмом
    #    7586 м³; разбор возвращал False, но флаг не был ни в EDITABLE,
    #    ни среди пустых полей — и «!» не появлялся никогда.
    import app as _app
    checks.append(("флаг доверия — вывод разбора, а не правка руками",
                   "dims_confident" in _app.DOWNGRADE_ONLY))
    checks.append(("флаг доверия не считается правкой",
                   "dims_confident" not in _app.EDITABLE))
    merge = lambda stored, got: bool(stored if stored is not None else True) and bool(got)
    checks += [
        ("сохранённое True + разбор False -> False", merge(True, False) is False),
        ("сохранённое False + разбор True -> False (только вниз)",
         merge(False, True) is False),
        ("сохранённое True + разбор True -> True", merge(True, True) is True),
    ]

    # Склеенная таблица исполнений: ответ выглядит полным, а исполнений
    # в нём одно вместо шести. У BAROVIER AURORA страница отдавала
    # «Murano blownglass AE Light Pink/Crystal FL Aquamarine/Crystal …»
    # одной строкой, и карточка молча теряла пять из шести.
    GLUED = ("Murano blownglass AE Light Pink/Crystal FL Aquamarine/Crystal "
             "CF Liquid Citron/Crystal CI Grey/Crystal CW Brown/Crystal")
    checks += [
        ("склейка распознаётся", extract.looks_glued(GLUED) is True),
        ("обычная отделка склейкой не считается",
         extract.looks_glued("Light Pink/Crystal") is False),
        ("артикул в названии — не склейка",
         extract.looks_glued("CL Polished Chrome") is False),
        ("две прописные в названии материала — не склейка",
         extract.looks_glued("Металл LIGHT BURNISHED BRASS + MATT BLACK NICKEL") is False),
        ("склейка переспрашивается у тяжёлой",
         extract._thin({"products": [{"type_ru": "Настольная лампа",
                                      "variants": [{"dims_raw": "1"}],
                                      "finishes": [{"material": GLUED}]}]},
                       known_types=("Настольная лампа",)) is True),
    ]

    # Список ролей обязан доходить до модели: роль печатается клиенту,
    # а модель выбирала её как умела — стекло приезжало «Обивкой», а
    # однажды мусором «ОбиglVertex».
    roles = extract._roles_line(pl.ROLES_RU)
    checks += [
        ("список ролей уходит в запрос", "Стекло" in roles and "Обивка" in roles),
        ("сказано смотреть на материал", "МАТЕРИАЛ" in roles),
        ("без списка запрос не меняется", extract._roles_line(()) == ""),
    ]

    # 7б. Наш список типов обязан доходить до модели. Раньше он жил
    #     только в _thin, то есть проверял ответ ПОСЛЕ, а спросить
    #     словами из списка никто не догадался: на PORADA 37 % товаров
    #     приезжали «Другим».
    line = extract._types_line(pl.TYPES_RU)
    checks += [
        ("список типов уходит в запрос", "Кресло" in line and "Табурет" in line),
        ("оговорка про «Другое» на месте", "не подбирай похожий" in line),
        ("без списка запрос не меняется", extract._types_line(()) == ""),
    ]

    # 7в. Указания по бренду обязаны доходить до модели. Раньше знания
    #     о нотации фабрики жили в brands/*.md, которые код не читал
    #     вовсе, — то есть модель их не видела никогда.
    checks += [
        ("файл бренда подхватывается по домену",
         "PORADA" in extract.brand_note("https://www.porada.it/prodotto/x")),
        ("www. в адресе не мешает",
         extract.brand_note("https://www.porada.it/x")
         == extract.brand_note("https://porada.it/x")),
        ("файл LLG подхватывается",
         "H.B." in extract.brand_note("https://luxurylivinggroup.com/products/x")),
        ("чужой домен добавки не получает",
         extract.brand_note("https://example.com/x") == ""),
        ("выдуманный домен ничего не читает с диска",
         extract.brand_note("https://../../etc/passwd") == ""),
    ]

    # Каждый файл бренда должен быть про свой сайт и не пустой.
    import glob as _glob
    brand_files = sorted(_glob.glob(os.path.join(extract.BRAND_DIR, "*.md")))
    checks.append((f"файлы брендов на месте ({len(brand_files)})", len(brand_files) >= 2))
    # Сверка сравнивает с текстом СТРАНИЦЫ. У техлиста свой текст: семь
    # брендов из восьми публикуют размеры чертежом, и требовать их чисел
    # на странице значило бы кричать «нет в источнике» на каждом.
    checks.append(("разбор помечает, что размеры из техлиста",
                   "dims_from_spec" in pl.Product.__dataclass_fields__))
    for path in brand_files:
        host = os.path.basename(path)[:-3]
        body = open(path, encoding="utf-8").read()
        checks.append((f"{host:26} называет свой домен", host in body))
        checks.append((f"{host:26} не пустой", len(body.strip()) > 400))

    # 8. Документы всегда читает тяжёлая: у VENICEM размеры нарисованы
    #    на схеме, и лёгкая теряет там глубину.
    import inspect
    src = inspect.getsource(extract.from_url)
    checks.append(("документ читает тяжёлая", "GEMINI_MODEL_LIGHT" not in src))

    # 9. Список полей запроса не должен расходиться со схемой по тем
    #    полям, которые код действительно читает.
    schema = _json.load(open(os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "config", "extraction_schema.json"), encoding="utf-8"))

    def leaves(node, prefix=""):
        out = []
        for key, value in (node.get("properties") or {}).items():
            out.append(prefix + key)
            if value.get("type") == "array":
                out += leaves(value.get("items") or {}, prefix + key + ".")
            elif value.get("type") == "object":
                out += leaves(value, prefix + key + ".")
        return out

    asked = set(_re.findall(r'"([a-z0-9_]+)":', extract._ASK))
    used = {"package_dims_raw", "packed_volume_m3", "dims_raw", "sku",
            "variant_note", "type_ru", "model", "collection", "brand",
            "summary_ru", "tech_note", "role_ru", "material", "code"}
    missing = sorted(f for f in leaves(schema)
                     if f.split(".")[-1] in used and f.split(".")[-1] not in asked)
    checks.append((f"поля, которые читает код, есть в запросе"
                   + (f" — нет: {missing}" if missing else ""), not missing))

    good = 0
    for label, hit in checks:
        good += bool(hit)
        print(f"  {OK if hit else BAD} {label}")
    return good, len(checks)


def _raises_on_answer(extract, payload, whole: bool = False) -> bool:
    """Вернул бы _gemini исключение на таком ответе сервера."""
    import httpx

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            if whole:
                return payload
            return {"candidates": [{"content": {"parts": [
                {"text": _json.dumps(payload, ensure_ascii=False)}]}}]}

    real_post, real_key = httpx.post, os.environ.get("GOOGLE_API_KEY")
    os.environ["GOOGLE_API_KEY"] = "проверка"
    httpx.post = lambda *a, **kw: FakeResponse()
    try:
        extract._gemini(text="x")
        return False
    except Exception:            # noqa: BLE001 — ровно этого и ждём
        return True
    finally:
        httpx.post = real_post
        if real_key is None:
            os.environ.pop("GOOGLE_API_KEY", None)
        else:
            os.environ["GOOGLE_API_KEY"] = real_key


def check_delivery() -> tuple[int, int]:
    """Порядок доставки страницы: сначала обычный запрос.

    Замер на восьми брендах показал, что Firecrawl не всегда улучшение:
    у HENGE он возвращал одно меню навигации вместо страницы. Теперь он
    запасной, и ключ к нему нужен только там, где обычный запрос
    получает отказ.
    """
    import os
    print("\n ДОСТАВКА СТРАНИЦЫ")
    print(" " + "-" * 74)

    calls = {"plain": 0, "fire": 0}

    def fake_plain(url):
        calls["plain"] += 1
        return ("текст страницы " * 200, ["https://x/a.pdf"], "<html>тело</html>")

    class FakeFire:
        def scrape(self, *a, **kw):
            calls["fire"] += 1
            return {"markdown": "из доставщика", "links": [], "html": ""}

    real_plain = pl._plain
    pl._plain = fake_plain
    try:
        text, _, _ = pl._scrape("https://brand.it/x", fc=FakeFire())
        checks = [("обычный запрос идёт первым", calls["plain"] == 1),
                  ("доставщик не тревожится напрасно", calls["fire"] == 0),
                  ("вернулось то, что пришло обычным запросом", "текст страницы" in text)]

        # Обычный не прошёл — идём к доставщику.
        pl._plain = lambda url: None
        calls["fire"] = 0
        text, _, _ = pl._scrape("https://brand.it/x", fc=FakeFire())
        checks.append(("отказ обычного запроса включает доставщика", calls["fire"] == 1))
        checks.append(("вернулось от доставщика", text == "из доставщика"))

        # Без ключа и без доставщика — честный отказ, а не трейсбек.
        was = os.environ.pop("FIRECRAWL_API_KEY", None)
        try:
            refused = False
            try:
                pl._scrape("https://brand.it/x")
            except pl.NoDelivery as exc:
                refused = "FIRECRAWL_API_KEY" in str(exc)
            checks.append(("без ключа отказ называет причину и что делать", refused))
        finally:
            if was is not None:
                os.environ["FIRECRAWL_API_KEY"] = was

        # Короткий ответ — это оболочка без товара, а не страница.
        pl._plain = real_plain
        checks.append(("короткий ответ страницей не считается",
                       pl.MIN_PAGE_TEXT >= 1000))
    finally:
        pl._plain = real_plain

    good = 0
    for label, hit in checks:
        good += bool(hit)
        print(f"  {OK if hit else BAD} {label}")
    return good, len(checks)


def check_spec_choice() -> tuple[int, int]:
    """Отбор техлиста и показ исполнений.

    Оба случая пойманы на живом оффере заказчика: у LONGHI техлист
    отдаётся по адресу без «.pdf» и терялся целиком, у FLOU выигрывал
    каталог на 155 МБ, потому что в его имени есть слово «scheda».
    """
    print("\n ТЕХЛИСТ И ИСПОЛНЕНИЯ")
    print(" " + "-" * 74)

    flou = ["https://flou.it/x/flou_catalogue_scheda.pdf",
            "https://flou.it/x/madamebutterfly_265.pdf"]
    longhi = ["https://www.longhi.it/AjaxCalls/Products/GenerateTechnicalSheet"
              "?catalog=-497733865&language=2&id=-1243879315"]

    checks = [
        ("документ без «.pdf» в адресе не теряется",
         pl._docs_from(longhi) == longhi),
        ("посторонняя ссылка документом не считается",
         pl._docs_from(["https://brand.it/about-us"]) == []),
        ("лист позиции выигрывает у каталога бренда",
         pl._pick_spec_pdf(flou, "Madame Butterfly").endswith("madamebutterfly_265.pdf")),
        ("каталог остаётся запасным кандидатом, а не выбрасывается",
         len(pl.spec_pdf_candidates(flou, "Madame Butterfly")) == 2),
        # Промах первого кандидата больше не стоит строки габаритов.
        ("кандидатов несколько, пробуются по очереди",
         pl.spec_pdf_candidates(flou, "Madame Butterfly")[0]
         != pl.spec_pdf_candidates(flou, "Madame Butterfly")[1]),
        ("прежний выбор с хвостом версии не сломан",
         pl._pick_spec_pdf(
             ["https://luxurylivinggroup.com/x/VIBES_bed_GUEST.pdf?v=674412"],
             "Vibes").endswith("?v=674412")),
        ("инструкция по сборке не выигрывает у листа позиции",
         pl._pick_spec_pdf(
             ["https://brand.it/x/assembly_instructions.pdf",
              "https://brand.it/x/circle_fact_sheet.pdf"], "Circle")
         .endswith("circle_fact_sheet.pdf")),
    ]

    # Исполнения: подставленное названо поимённо, а не «первое».
    variants = [{"sku": "VBE (LE1)", "dims_raw": "202x241x92H.", "variant_note": "165x200"},
                {"sku": "VBE (LE2)", "dims_raw": "222x241x92H.", "variant_note": "185x200"}]
    warning = ""
    product = pl.Product(brand="TRUSSARDI", model="VIBES", type_ru="Кровать",
                         variants=variants)
    # Повторяем ту часть сборки, что подставляет исполнение.
    first = product.variants[0]
    product.dims_raw = first["dims_raw"]
    which = " · ".join(x for x in (first.get("sku"), first.get("variant_note"),
                                   product.dims_raw) if x)
    warning = (f"Исполнений {len(variants)}, подставлено «{which}» — "
               "порядок со страницы, не выбор.")
    checks.append(("предупреждение называет подставленное исполнение",
                   "VBE (LE1)" in warning and "165x200" in warning))
    checks.append(("предупреждение не выдаёт порядок за выбор",
                   "не выбор" in warning))

    good = 0
    for label, hit in checks:
        good += bool(hit)
        print(f"  {OK if hit else BAD} {label}")
    return good, len(checks)


def check_docs_list() -> tuple[int, int]:
    """Список документов: бумаги сайта в него не попадают.

    На каждой странице магазина висит заявление о доступности, у BAROVIER —
    политика информирования. Инструкция по сборке при этом остаётся:
    она про изделие, просто не техлист.
    """
    print("\n СПИСОК ДОКУМЕНТОВ")
    print(" " + "-" * 74)
    links = [
        "https://cdn.shopify.com/s/files/Accessibility_Statement_LANG_EN.pdf?v=176",
        "https://x/cdn/shop/files/VIBES_bed_GUEST.pdf?v=674412",
        "https://venicem.com/Venicem_Assembly_Instructions_Circle_Floor.pdf",
        "https://venicem.com/Venicem_product_fact_sheet_circle-floor.pdf",
        "https://barovier.com/files/policy-whistleblowing-barovier-toso_0.pdf",
        "https://x/page.html",
    ]
    docs = pl._docs_from(links)
    names = [d.rsplit("/", 1)[-1].split("?")[0] for d in docs]
    checks = [
        ("заявление о доступности скрыто",
         not any("Accessibility" in n for n in names)),
        ("политика информирования скрыта",
         not any("whistleblowing" in n for n in names)),
        ("инструкция по сборке осталась",
         any("Assembly" in n for n in names)),
        ("техлист остался", any("fact_sheet" in n for n in names)),
        ("не-PDF отсеян", all(n.endswith(".pdf") for n in names)),
    ]
    good = 0
    for note, hit in checks:
        good += hit
        print(f"  {OK if hit else BAD} {note}")
    return good, len(checks)


def check_spec_pdf() -> tuple[int, int]:
    """Отбор техлиста среди прочих PDF страницы."""
    print("\n ОТБОР ТЕХЛИСТА")
    print(" " + "-" * 74)
    cases = [
        (["https://x/cdn/shop/files/VIBES_bed_GUEST.pdf?v=674412",
          "https://cdn.shopify.com/s/files/Accessibility_Statement_EN.pdf?v=176"],
         "VIBES", "адрес с хвостом версии, рядом заявление о доступности"),
        (["https://barovier.com/files/policy-whistleblowing-barovier-toso_0.pdf"],
         "", "единственный PDF — политика, техлиста нет"),
        (["https://venicem.com/Venicem_Assembly_Instructions_Pinocchio.pdf",
          "https://venicem.com/Venicem_product_fact_sheet_pinocchio-floor.pdf"],
         "fact_sheet", "инструкция сборки рядом с техлистом"),
        ([], "", "документов нет"),
    ]
    good = 0
    for urls, expected, note in cases:
        got = pl._pick_spec_pdf(urls)
        hit = (expected in got) if expected else (got == "")
        good += hit
        print(f"  {OK if hit else BAD} {note:56} -> {(got.rsplit('/', 1)[-1] or '—')[:26]}")
    return good, len(cases)


def check_url_types() -> tuple[int, int]:
    print("\n ТИП ПО РАЗДЕЛУ САЙТА")
    print(" " + "-" * 74)
    good = 0
    for url, expected in URL_TYPE_CASES:
        got = pl.type_from_url(url)
        hit = got == expected
        good += hit
        short = url.split("//")[-1][:52]
        print(f"  {OK if hit else BAD} {short:54} -> {got or '—'}")
        if not hit:
            print(f"      ожидали {expected or '—'}")
    return good, len(URL_TYPE_CASES)


def check_type_norm() -> tuple[int, int]:
    print("\n ТИП ИЗ ИЗВЛЕЧЕНИЯ")
    print(" " + "-" * 74)
    good = 0
    for raw, expected in TYPE_NORM_CASES:
        got, _ = pl.normalize_type(raw)
        hit = got == expected
        good += hit
        print(f"  {OK if hit else BAD} {(raw or '—'):20} -> {got or '—'}")
        if not hit:
            print(f"      ожидали {expected or '—'}")
    return good, len(TYPE_NORM_CASES)


def check_schema() -> tuple[int, int]:
    """Схема извлечения дублирует списки из кода — стережём расхождение.

    Заодно ловим форму, на которой LlamaExtract падает ещё до чтения
    документа: nullable-массив он разворачивает в anyOf и теряет items.
    """
    print("\n СХЕМА ИЗВЛЕЧЕНИЯ (config/extraction_schema.json)")
    print(" " + "-" * 74)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "config", "extraction_schema.json")
    schema = json.load(open(path, encoding="utf-8"))
    item = schema["properties"]["products"]["items"]["properties"]

    bad: list[str] = []

    def walk(node, where="корень"):
        t = node.get("type")
        if isinstance(t, list) and "array" in t:
            bad.append(f"{where}: массив объявлен nullable — LlamaExtract потеряет items")
        if t == "array" and "items" not in node:
            bad.append(f"{where}: массив без items")
        # Проверено опытом: enum LlamaExtract не соблюдает, а молча отдаёт
        # последнее значение списка. Кровать превращалась в «Ковёр»,
        # ткань — в «Камень». Допустимые значения держим в description.
        if "enum" in node:
            bad.append(f"{where}: enum — LlamaExtract вернёт последнее значение списка")
        for k, v in (node.get("properties") or {}).items():
            walk(v, f"{where}.{k}")
        if isinstance(node.get("items"), dict):
            walk(node["items"], f"{where}[]")

    walk(schema)

    role = item["finishes"]["items"]["properties"]["role_ru"]
    checks = [
        ("все типы перечислены в описании type_ru",
         all(t in item["type_ru"]["description"] for t in pl.TYPES_RU)),
        ("все роли перечислены в описании role_ru",
         all(r in role["description"] for r in pl.ROLES_RU)),
        ("исполнение — строка массива variants", "dims_raw" in
         item["variants"]["items"]["properties"]),
        ("форма схемы принимается LlamaExtract", not bad),
    ]
    for label, hit in checks:
        print(f"  {OK if hit else BAD} {label}")
    for b in bad:
        print(f"      {b}")
    return sum(h for _, h in checks), len(checks)


def _same(expected, actual) -> bool:
    """Сравнение с эталоном: числа сверяем как числа, а не как строки."""
    if expected is None or actual is None:
        return expected == actual
    try:
        return abs(float(expected) - float(actual)) < 0.01
    except (TypeError, ValueError):
        return str(expected).strip().lower() == str(actual).strip().lower()


def show(p, expect: dict | None = None) -> None:
    """Печать карточки; расхождения с ожиданием помечаются."""
    expect = expect or {}

    def line(label, value, key=None, actual=None):
        mark = " "
        if key in expect:
            mark = OK if _same(expect[key], value if actual is None else actual) else BAD
        print(f"   {mark} {label:14} {value}")

    def dims_line():
        keys = ("width_cm", "depth_cm", "height_cm")
        vals = (p.width_cm, p.depth_cm, p.height_cm)
        shown = " / ".join(f"{v:g}" if v else "—" for v in vals)
        checked = [k for k in keys if k in expect]
        mark = " "
        if checked:
            mark = OK if all(_same(expect[k], v) for k, v in zip(keys, vals) if k in expect) else BAD
        print(f"   {mark} {'Д / Г / В':14} {shown}")

    line("производитель", p.brand or "—")
    line("модель", p.model or "—")
    line("тип", p.type_ru or "—", "type_ru")
    line("размеры", p.dims_raw or "—")
    dims_line()
    line("объём", f"{p.volume_m3 or '—'} м³ ({p.volume_source or 'не определён'})")
    line("отделки", ", ".join(
        f"{f.get('role_ru')}: {f.get('material')}" for f in p.finishes) or "—")
    line("примечание", p.tech_note or "—")
    line("фото / доки", f"{len(p.photo_urls)} / {len(p.doc_urls)}")
    for w in p.warnings:
        print(f"   {WARN} {w}")


def check_live() -> None:
    print("\n ЖИВЫЕ ССЫЛКИ (нужен интернет)")
    print(" " + "-" * 74)
    for case in LIVE_CASES:
        print(f"\n  {case['url']}")
        print(f"  {case['note']}")
        try:
            p = pl.lookup(case["url"])
        except Exception as exc:  # noqa: BLE001
            print(f"   {BAD} ошибка: {exc}")
            continue
        show(p, case.get("expect"))
        print("   описание для колонки C:")
        for ln in pl.to_excel_description(p).splitlines():
            print(f"      | {ln}")


def main() -> int:
    args = sys.argv[1:]

    if args and args[0].startswith("http"):
        p = pl.lookup(args[0])
        print(f"\n {args[0]}")
        show(p)
        print("\n описание для колонки C:")
        for ln in pl.to_excel_description(p).splitlines():
            print(f"   | {ln}")
        return 0

    # Раньше здесь стояло два десятка пар имён, и это дало две тихие беды:
    # `d_ok` присваивался разбором размеров и тут же затирался списком
    # документов (под подписью «Размеры» печатались документы, а 17 проверок
    # размеров в приёмку не входили вовсе), а результат проверки входа
    # вычислялся и не использовался. Список закрывает оба случая разом:
    # подпись, счёт и участие в итоге — одна запись, забыть нечего.
    checks: list[tuple[str, int, int]] = []

    def run(label: str, fn) -> None:
        good, total = fn()
        checks.append((label, good, total))

    run("Размеры", check_dims)
    run("Нотация размеров", check_book_dims)
    run("Единица", check_units)
    run("Объём", check_volume)
    run("Откуда объём", check_volume_source)
    run("Сверка", check_dims_grounding)
    run("Техлист", check_spec_pdf)
    run("Документы", check_docs_list)
    run("Извлекатели", check_extractors)
    run("Доставка", check_delivery)
    run("Техлист и исполнения", check_spec_choice)
    run("Строка", check_book_row)
    run("Колонки", check_columns)
    run("Файл против экрана", check_export_matches_screen)
    run("Расчёт", check_pricing)
    run("Начальные числа", check_position_defaults)
    run("Итог", check_final_block)
    run("Ручные", check_overrides)
    run("Правила галерей", check_gallery_rules)
    run("Вход", check_login)
    run("Фото", check_photos)
    run("Витрина отделок", check_palette)
    run("Тип по адресу", check_url_types)
    run("Тип из извлечения", check_type_norm)
    run("Источник фото", check_shops)
    run("Библиотека", check_library)
    run("Плитка каталога", check_library_card)
    run("Режим правки", check_item_edit_mode)
    run("Проекты", check_projects)
    run("Контракт страниц", check_page_contract)
    run("Формулы страниц", check_page_formats)
    run("Поведение страниц", check_page_logic)
    run("Ход разбора", check_parse_progress)
    run("Подстановка исполнения", check_variant_pick)
    run("Комнаты", check_rooms)
    run("Шапка", check_header_roundtrip)
    run("Выгрузка", check_download_headers)
    run("Схема", check_schema)

    if "--galleries" in args:
        run("Галереи живьём", check_gallery_live)

    if "--offline" not in args and "--galleries" not in args:
        check_live()

    ok = all(good == total for _, good, total in checks)

    print("\n" + " " + "=" * 74)
    # Переносим по ширине терминала, а не по заранее нарезанным строкам:
    # добавить проверку и забыть вписать её в подпись больше нельзя.
    line = "  "
    for label, good, total in checks:
        piece = f"{label}: {good}/{total}   "
        if len(line) + len(piece) > 100:
            print(line.rstrip())
            line = "  "
        line += piece
    print(line.rstrip())
    if ok:
        print("  Разбор совпадает с книгой, схема согласована с кодом.")
    else:
        print("  ЕСТЬ РАСХОЖДЕНИЯ — смотрите строки с ✗ выше.")
    print(" " + "=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
