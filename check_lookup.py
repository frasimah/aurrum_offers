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
]

# --- Эталоны объёма: габариты -> м³ по колонке R книги -----------------
VOLUME_CASES = [
    ((130, 47, 45),  0.5, "R16"),
    ((90, 84, 80),   1.0, "R18"),
    ((31, 31, 125),  0.2, "R20"),
    ((202, 241, 92), 6.8, "R22"),
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
    s_ok, s_all = check_schema()

    if "--offline" not in args:
        check_live()

    ok = d_ok == d_all and v_ok == v_all and s_ok == s_all
    print("\n" + " " + "=" * 74)
    print(f"  Размеры: {d_ok}/{d_all}   Объём: {v_ok}/{v_all}   Схема: {s_ok}/{s_all}")
    if ok:
        print("  Разбор совпадает с книгой, схема согласована с кодом.")
    else:
        print("  ЕСТЬ РАСХОЖДЕНИЯ — смотрите строки с ✗ выше.")
    print(" " + "=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
