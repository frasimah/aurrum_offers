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

import product_lookup as pl  # noqa: E402

OK, BAD, WARN = "✓", "✗", "~"

# --- Эталоны разбора размеров: строка из книги -> Д, Г, В --------------
DIMS_CASES = [
    ("130x47x45Н",             "Банкетка",              (130, 47, 45),  "LONGHI ARIANA, R16"),
    ("90x84x80Н",              "Кресло",                (90, 84, 80),   "FLOU MADAME BUTTERFLY, R18"),
    ("D25/31x125Н",            "Торшер",                (31, 31, 125),  "VENICEM CIRCLE, R20"),
    ("202x241x36/92Н",         "Кровать",               (202, 241, 92), "TRUSSARDI VIBES, R22"),
    ("66x60x44Н SPECIAL",      "Тумбочка прикроватная", (66, 60, 44),   "TRUSSARDI COMFY, R23"),
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
    # «chairs» лежит внутри «armchairs» — раздел смешанный, тип не навязываем
    ("https://www.henge07.com/products/sofas-and-armchairs/radical", ""),
    ("https://www.porada.it/prodotto/infinity",                   ""),
    # У света тип задаёт способ монтажа, а не раздел
    ("https://flos.com/en/us/ic-lights-floor/M-ic-lights-floor.html", ""),
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
    print("\n ЖИВЫЕ ССЫЛКИ (нужен FIRECRAWL_API_KEY)")
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

    d_ok, d_all = check_dims()
    v_ok, v_all = check_volume()
    g_ok, g_all = check_dims_grounding()
    f_ok, f_all = check_spec_pdf()
    t_ok, t_all = check_url_types()
    s_ok, s_all = check_schema()

    if "--offline" not in args:
        check_live()

    ok = (d_ok == d_all and v_ok == v_all and s_ok == s_all
          and t_ok == t_all and g_ok == g_all and f_ok == f_all)
    print("\n" + " " + "=" * 74)
    print(f"  Размеры: {d_ok}/{d_all}   Объём: {v_ok}/{v_all}   "
          f"Сверка: {g_ok}/{g_all}   Техлист: {f_ok}/{f_all}   "
          f"Тип по адресу: {t_ok}/{t_all}   Схема: {s_ok}/{s_all}")
    if ok:
        print("  Разбор совпадает с книгой, схема согласована с кодом.")
    else:
        print("  ЕСТЬ РАСХОЖДЕНИЯ — смотрите строки с ✗ выше.")
    print(" " + "=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
