# -*- coding: utf-8 -*-
"""
Карточка позиции по ссылке на сайт бренда.

Страницу и spec-sheet PDF забирает Firecrawl, он же извлекает данные по
JSON-схеме. Объём и текст описания считает Python — детерминированное
модели не отдаём.

Зачем PDF: на страницах производителей габариты часто нарисованы в SVG
кривыми (текста нет), а фактический объём упаковки публикуется только
в spec sheet. У VENICEM CIRCLE, например, `125 / Ø25 / 31 cm` и `0,23 м³`
есть исключительно там.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field

from firecrawl import Firecrawl

# Контролируемый словарь: модель выбирает из списка, а не переводит свободно.
TYPES_RU = [
    "Кресло", "Диван", "Кровать", "Торшер", "Люстра", "Бра",
    "Настольная лампа", "Стол", "Стул", "Банкетка", "Пуф",
    "Тумбочка прикроватная", "Комод", "Шкаф", "Стеллаж", "Ковёр",
    "Зеркало", "Мебель для кухни", "Другое",
]

ROLES_RU = [
    "Обивка", "Отделка", "Металл", "Топ", "Кант", "Камень",
    "Стекло", "Дерево", "Ножки", "Каркас", "Фасады",
]

# Коэффициент упаковки и шаг округления — как в рабочей книге:
# U = ROUNDUP(Д×Г×В×1.5/1000000; 1)
PACKING_FACTOR = 1.5
VOLUME_STEP = 0.1

_EXTRACT_RULES = f"""
Извлеки карточку предмета мебели или света для коммерческого предложения.

Правила:
- Названия материалов, отделок и коллекций НЕ переводи — копируй ровно теми
  символами, что стоят на источнике, вместе с артикулом и номером категории.
- Ничего не выдумывай: если название материала на источнике не указано,
  не добавляй эту отделку вообще. Пустой список лучше правдоподобной догадки.
- Тип предмета и роль отделки выбирай строго из списка допустимых значений.
- Для светильников тип определяй по способу установки:
  floor/pavimento → Торшер; ceiling/suspension/soffitto → Люстра;
  wall/parete → Бра; table/tavolo → Настольная лампа.
  Слово «Floor» в названии модели означает напольный светильник — Торшер.
- Габариты бери ТОЛЬКО из раздела размеров изделия (DIMENSIONS, DIMENSIONI,
  MISURE, РАЗМЕРЫ). В техлистах подписи и значения часто идут разными
  колонками — сопоставляй их аккуратно.
  Категорически не бери в габариты: CABLE LENGTH (длина провода),
  PACKAGE / BOX (размеры упаковки), высоту подвеса, диапазон регулировки.
  Если в разделе размеров несколько значений — верни их все в dims_raw
  и выбери из них наибольший горизонтальный и высоту.
- Если данных на источнике нет — оставь поле пустым. Не догадывайся.

Допустимые типы: {", ".join(TYPES_RU)}.
Допустимые роли отделок: {", ".join(ROLES_RU)}.
""".strip()

_FINISH_ITEM = {
    "type": "object",
    "properties": {
        "role_ru": {"type": "string", "enum": ROLES_RU},
        "material": {"type": "string", "description": "как на источнике, без перевода"},
        "code": {"type": "string", "description": "артикул отделки, если указан"},
    },
    "required": ["role_ru", "material"],
}

PAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "brand": {"type": "string"},
        "model": {"type": "string", "description": "название модели без бренда"},
        "collection": {"type": "string"},
        "designer": {"type": "string"},
        "type_ru": {"type": "string", "enum": TYPES_RU},
        "finishes": {"type": "array", "items": _FINISH_ITEM},
        "tech_note": {
            "type": "string",
            "description": "мощность, люмены, цветовая температура, особенности",
        },
        "photo_urls": {
            "type": "array",
            "items": {"type": "string"},
            "description": "фотографии самого изделия; без логотипов, иконок и образцов материалов",
        },
        "doc_urls": {
            "type": "array",
            "items": {"type": "string"},
            "description": "ссылки на PDF: spec sheet, техлист, чертежи",
        },
    },
    "required": ["brand", "model"],
}

PDF_SCHEMA = {
    "type": "object",
    "properties": {
        "dims_raw": {
            "type": "string",
            "description": (
                "габариты изделия одной строкой ДОСЛОВНО как в документе, "
                "со всеми значениями и обозначениями (Ø, H), без перевода "
                "единиц на русский. Пример: '125 cm, Ø 25 cm, 31 cm'"
            ),
        },
        "width_cm": {
            "type": "number",
            "description": (
                "наибольший горизонтальный габарит изделия в см. Если указано "
                "несколько диаметров или ширин — бери максимальный"
            ),
        },
        "depth_cm": {
            "type": "number",
            "description": (
                "глубина в см. Для круглых изделий равна наибольшему диаметру"
            ),
        },
        "height_cm": {
            "type": "number",
            "description": (
                "ВЕРТИКАЛЬНЫЙ размер изделия в см — то, насколько оно высокое. "
                "В документах обычно помечен H или Н. У напольного светильника "
                "это самое большое из значений"
            ),
        },
        "volume_m3_declared": {
            "type": "number",
            "description": "объём упаковки TOTAL VOL в м³, если указан",
        },
        "package_note": {"type": "string", "description": "размеры короба и вес"},
        "tech_note": {"type": "string"},
        "finishes": {"type": "array", "items": _FINISH_ITEM},
    },
}


@dataclass
class Product:
    source_url: str = ""
    brand: str = ""
    model: str = ""
    collection: str = ""
    designer: str = ""
    type_ru: str = ""
    dims_raw: str = ""
    width_cm: float | None = None
    depth_cm: float | None = None
    height_cm: float | None = None
    dims_confident: bool = False     # оси разобраны по пометке H, а не угаданы
    volume_m3: float | None = None
    volume_source: str = ""          # "производитель" | "расчёт по габаритам"
    package_note: str = ""
    finishes: list[dict] = field(default_factory=list)
    tech_note: str = ""
    photo_urls: list[str] = field(default_factory=list)
    doc_urls: list[str] = field(default_factory=list)
    spec_pdf_url: str = ""
    warnings: list[str] = field(default_factory=list)


def normalize_type(value: str) -> tuple[str, str | None]:
    """Тип из извлечения -> (значение из нашего списка, предупреждение).

    Проверять обязательно: значение вне списка не отмечает ни одного пункта
    в выпадающем поле, и браузер показывает первый — «Диван-кровать» молча
    превращается в «Кресло». Отдельная причина: LlamaExtract не соблюдает
    enum в схеме и возвращает последнее значение списка.
    """
    value = (value or "").strip()
    if not value or value in TYPES_RU:
        return value, None
    return "Другое", f"Тип «{value}» не из нашего списка — поставлен «Другое»."


# Раздел сайта — сильный сигнал о типе, и он не используется извлечением:
# у EMMEMOBILI страница /prodotti/contenitori/ («Storage units», в тексте
# прямо «sideboard») приезжала как «Стол». Список нарочно короткий — только
# однозначные разделы; свет сюда не годится, там тип задаёт способ монтажа.
_URL_TYPES = {
    "Комод": ("contenitori", "storage", "sideboard", "credenz", "madia", "dresser"),
    "Кровать": ("letti", "bed", "beds"),
    "Стул": ("sedie", "chair", "chairs"),
    "Кресло": ("poltrone", "armchair", "armchairs"),
    "Стеллаж": ("librerie", "bookcase", "shelving"),
    "Диван": ("divani", "sofa", "sofas"),
    "Стол": ("tavoli", "table", "tables"),
    "Тумбочка прикроватная": ("comodini", "bedside", "nightstand"),
    "Ковёр": ("tappeti", "rug", "rugs", "carpet"),
    "Зеркало": ("specchi", "mirror", "mirrors"),
}


def type_from_url(url: str) -> str:
    """Тип по разделу сайта в адресе. Пусто, если раздел неоднозначен.

    Совпадение только с начала слова: иначе «chairs» находится внутри
    «armchairs» и раздел с креслами уезжает в «Стул».
    """
    low = (url or "").lower()
    hits = {
        type_ru for type_ru, tokens in _URL_TYPES.items()
        if any(re.search(rf"(?<![a-z]){tok}", low) for tok in tokens)
    }
    return hits.pop() if len(hits) == 1 else ""


def normalize_role(value: str) -> tuple[str, str | None]:
    """Роль отделки -> (значение из нашего списка, предупреждение)."""
    value = (value or "").strip()
    if value in ROLES_RU:
        return value, None
    if not value:
        return "Отделка", None
    return "Отделка", f"Роль «{value}» не из нашего списка — поставлена «Отделка»."


def volume_m3(w: float | None, d: float | None, h: float | None) -> float | None:
    """Объём по формуле рабочей книги: ROUNDUP(Д×Г×В×1.5/1e6; 1)."""
    if not (w and d and h):
        return None
    raw = w * d * h * PACKING_FACTOR / 1_000_000
    # round снимает артефакт float: 6.800000000000001 -> 6.8
    return round(math.ceil(raw / VOLUME_STEP) * VOLUME_STEP, 2)


def to_excel_description(p: Product) -> str:
    """Текст для колонки «Описание» — в том же формате, что в книге.

    Отделки с одной ролью книга пишет одной строкой через « + », а названия
    материалов — заглавными: «Отделка - Металл LIGHT BURNISHED BRASS + ...».
    """
    lines = [p.model.upper()] if p.model else []
    if p.type_ru:
        lines.append(p.type_ru)
    if p.dims_raw:
        lines.append(p.dims_raw)

    by_role: dict[str, list[str]] = {}
    for f in p.finishes:
        material = str(f.get("material") or "").strip()
        if not material:
            continue
        role = str(f.get("role_ru") or "Отделка").strip()
        by_role.setdefault(role, [])
        if material.upper() not in by_role[role]:
            by_role[role].append(material.upper())
    for role, materials in by_role.items():
        lines.append(f"{role} - {' + '.join(materials)}")

    lines += ["", "Фото из Каталога"]
    return "\n".join(lines).strip()


def _client() -> Firecrawl:
    key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "Не задан FIRECRAWL_API_KEY. Добавьте его в .env "
            "(образец — в .env.example)."
        )
    return Firecrawl(api_key=key)


def _as_dict(value) -> dict:
    """Ответ SDK бывает объектом и словарём — приводим к словарю."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    for attr in ("model_dump", "dict"):
        if hasattr(value, attr):
            try:
                return getattr(value, attr)()
            except Exception:
                pass
    return getattr(value, "__dict__", {}) or {}


def _scrape_json(
    fc: Firecrawl, url: str, schema: dict, timeout_ms: int = 120_000
) -> tuple[dict, str]:
    """Возвращает (структура, исходный markdown).

    Markdown нужен, чтобы сверить извлечённое с первоисточником: модель
    способна выдумать правдоподобное название материала, которого на
    странице нет (проверено на LONGHI ARIANNA).
    """
    doc = fc.scrape(
        url,
        formats=["markdown", {"type": "json", "prompt": _EXTRACT_RULES, "schema": schema}],
        only_main_content=True,
        timeout=timeout_ms,
    )
    data = _as_dict(doc)
    return _as_dict(data.get("json")), str(data.get("markdown") or "")


def _grounded(value: str, sources: str) -> bool:
    """Есть ли значение в тексте источника.

    Совпадения по целой строке недостаточно: модель переставляет слова и
    дописывает пояснения. Поэтому требуем, чтобы хотя бы один значащий
    токен (от 4 символов) реально встречался в источнике.
    """
    tokens = [t for t in re.findall(r"[\w\-/.]+", value) if len(t) >= 4]
    if not tokens:
        return True  # нечего проверять — не отбрасываем
    low = sources.lower()
    return any(t.lower() in low for t in tokens)


def _verify_finishes(finishes: list[dict], sources: str) -> tuple[list[dict], list[str]]:
    """Отсеиваем отделки, которых нет в источнике."""
    kept, dropped = [], []
    for f in finishes:
        material = str(f.get("material") or "").strip()
        if not material:
            continue
        if _grounded(material, sources):
            kept.append(f)
        else:
            dropped.append(material)
    return kept, dropped


def _pick_spec_pdf(doc_urls: list[str]) -> str:
    """Из ссылок на документы выбираем техлист, а не инструкцию по сборке."""
    pdfs = [u for u in doc_urls if u.lower().endswith(".pdf")]
    if not pdfs:
        return ""
    preferred = ("fact_sheet", "fact-sheet", "spec", "scheda", "technical", "datasheet")
    for url in pdfs:
        low = url.lower()
        if any(token in low for token in preferred):
            return url
    skip = ("assembly", "instruction", "montaggio", "manual", "warranty")
    for url in pdfs:
        if not any(token in url.lower() for token in skip):
            return url
    return pdfs[0]


# Категории, у которых высота обычно наибольший размер — нужно, чтобы
# развести оси там, где в источнике нет пометки H.
_TALL_TYPES = {"Торшер", "Люстра", "Бра", "Стеллаж", "Шкаф", "Зеркало"}


def parse_dims(raw: str, type_ru: str = "") -> tuple[float | None, float | None, float | None, bool]:
    """Строка размеров -> (Д, Г, В, уверенно ли).

    Модель раскладывает оси нестабильно: на одной и той же странице высота
    уезжает то в ширину, то в глубину. Набор чисел при этом всегда верный,
    поэтому оси разбираем сами по нотации источника.

    Разбираем уверенно: «130x47x45Н» и «D25/31x125Н» (высота помечена H/Н).
    Всё остальное — догадка, и вызывающий обязан её показать как непроверенную.
    """
    if not raw:
        return None, None, None, False

    s = raw.replace(",", ".").replace("×", "x").replace("Х", "x")
    num = r"\d+(?:\.\d+)?"

    # Дюймовые двойники выкидываем целиком. Раньше отбрасывались только те,
    # что в скобках, и у PORADA «43 1/4"» через запятую попадало в общий
    # котёл чисел — высотой оказывался второй диаметр вместо 75 см.
    s = re.sub(r"\d+(?:\s+\d+\s*/\s*\d+)?\s*(?:\"|''|″|\bin\b)", " ", s)

    # Явная пометка высоты: «...x125Н», «H 125», «125 cm H»
    h_match = (
        re.search(rf"({num})\s*(?:cm|см|mm|мм)?\s*[HНh](?![a-zA-Zа-яА-Я])", s)
        or re.search(rf"[HНh]\s*({num})", s)
    )
    height = float(h_match.group(1)) if h_match else None

    # Диаметр бывает диапазоном — и через дробь «D25/31», и через тире
    # «Ø110 - 120». Берём наибольшее значение.
    diameters: list[float] = []
    for group in re.findall(rf"[ØøD]\s*({num}(?:\s*[/\-–—]\s*{num})*)", s):
        diameters += [float(x) for x in re.findall(num, group)]
    all_nums = [float(x) for x in re.findall(num, s)]
    # Размеры в дюймах идут в скобках — они не нужны
    inches = [float(x) for x in re.findall(rf"\(\s*({num})", s)]
    all_nums = [n for n in all_nums if n not in inches]

    if height is not None:
        horizontal = [n for n in all_nums if n != height]
        if diameters:
            d = max(diameters)
            return d, d, height, True
        if len(horizontal) >= 2:
            return horizontal[0], horizontal[1], height, True
        if horizontal:
            return horizontal[0], horizontal[0], height, True
        return None, None, height, False

    # Пометки высоты нет — приходится догадываться.
    rest = [n for n in all_nums if n not in diameters]
    if diameters and rest:
        d = max(diameters + [n for n in rest if n < max(rest)]) if len(rest) > 1 else max(diameters)
        return d, d, max(rest), False
    if len(all_nums) >= 3:
        w, dep, h = all_nums[0], all_nums[1], all_nums[2]
        if type_ru in _TALL_TYPES:
            h = max(all_nums)
            others = [n for n in all_nums if n != h] or [h]
            w, dep = others[0], others[-1]
        return w, dep, h, False
    return None, None, None, False


def _brand_from_url(url: str) -> str:
    """Запасной вариант, когда модель не вернула бренд: домен второго уровня."""
    m = re.search(r"https?://(?:www\.)?([^./]+)\.", url)
    return m.group(1).upper() if m else ""


def _dims_disagree(p: "Product") -> bool:
    """Числа, разложенные по осям, не сходятся со строкой размеров.

    Извлечение габаритов — самое нестабильное место: в техлистах подписи и
    значения идут разными колонками, и модель periodически путает оси.
    Строке `dims_raw` доверяем больше, поэтому сверяем набор чисел с ней.
    """
    if not p.dims_raw:
        return False
    in_raw = {round(float(x)) for x in re.findall(r"\d+(?:[.,]\d+)?", p.dims_raw.replace(",", "."))}
    assigned = {round(float(v)) for v in (p.width_cm, p.depth_cm, p.height_cm) if v}
    if not assigned:
        return False
    return not assigned.issubset(in_raw)


def _clean_photos(urls: list[str]) -> list[str]:
    """Отсекаем иконки, логотипы и служебную графику."""
    junk = ("logo", "icon", "sprite", "placeholder", "spin", "avatar", "favicon")
    out, seen = [], set()
    for url in urls:
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        low = url.lower()
        if any(token in low for token in junk):
            continue
        if low.endswith(".svg"):        # схемы, а не фото
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def lookup(url: str) -> Product:
    """Ссылка на товар -> карточка. Тянет страницу, затем spec-sheet PDF."""
    fc = _client()
    p = Product(source_url=url)

    page, page_md = _scrape_json(fc, url, PAGE_SCHEMA)
    if not page:
        raise RuntimeError(
            "Со страницы не удалось извлечь данные. Проверьте ссылку — "
            "возможно, сайт закрыт от автоматических запросов."
        )

    p.brand = str(page.get("brand") or "").strip()
    p.model = str(page.get("model") or "").strip()
    p.collection = str(page.get("collection") or "").strip()
    p.designer = str(page.get("designer") or "").strip()
    p.type_ru, type_warning = normalize_type(str(page.get("type_ru") or ""))
    if type_warning:
        p.warnings.append(type_warning)

    # Раздел сайта надёжнее догадки извлечения — но подмену показываем.
    by_url = type_from_url(url)
    if by_url and by_url != p.type_ru:
        p.warnings.append(
            f"Раздел сайта говорит «{by_url}», извлечение вернуло "
            f"«{p.type_ru or 'ничего'}» — поставлен «{by_url}», проверьте."
        )
        p.type_ru = by_url
    p.tech_note = str(page.get("tech_note") or "").strip()
    p.finishes = [_as_dict(f) for f in (page.get("finishes") or [])]
    p.photo_urls = _clean_photos(page.get("photo_urls") or [])
    p.doc_urls = [u for u in (page.get("doc_urls") or []) if isinstance(u, str)]

    # Габариты и фактический объём живут в техлисте, а не на странице.
    sources = page_md
    p.spec_pdf_url = _pick_spec_pdf(p.doc_urls)
    if p.spec_pdf_url:
        try:
            pdf, pdf_md = _scrape_json(fc, p.spec_pdf_url, PDF_SCHEMA)
            sources += "\n" + pdf_md
        except Exception as exc:  # noqa: BLE001 — техлист не критичен
            pdf = {}
            p.warnings.append(f"Техлист не прочитался: {exc}")
        p.dims_raw = str(pdf.get("dims_raw") or "").strip()
        p.width_cm = pdf.get("width_cm")
        p.depth_cm = pdf.get("depth_cm")
        p.height_cm = pdf.get("height_cm")
        # Оси берём из строки размеров: модель их путает между запусками.
        w, d, h, sure = parse_dims(p.dims_raw, p.type_ru)
        if w and d and h:
            p.width_cm, p.depth_cm, p.height_cm = w, d, h
            p.dims_confident = sure
        p.package_note = str(pdf.get("package_note") or "").strip()
        declared = pdf.get("volume_m3_declared")
        if declared:
            p.volume_m3 = round(float(declared), 3)
            p.volume_source = "производитель"
        if not p.tech_note:
            p.tech_note = str(pdf.get("tech_note") or "").strip()
        if not p.finishes:
            p.finishes = [_as_dict(f) for f in (pdf.get("finishes") or [])]
    else:
        p.warnings.append(
            "Техлист (spec sheet) не найден — габариты и объём заполните вручную."
        )

    if p.volume_m3 is None:
        calc = volume_m3(p.width_cm, p.depth_cm, p.height_cm)
        if calc:
            p.volume_m3 = round(calc, 2)
            p.volume_source = "расчёт по габаритам"

    if not p.brand:
        p.brand = _brand_from_url(url)

    # Роли приводим к нашему списку — извлечение выдаёт и то, чего в нём нет.
    # Заодно схлопываем повторы: у MISURAEMME одна и та же ткань приезжала
    # четырьмя строками. В описании для Excel они и так склеивались, а
    # в карточке дублировались.
    unique: list[dict] = []
    seen_finishes: set[tuple[str, str]] = set()
    for f in p.finishes:
        f["role_ru"], role_warning = normalize_role(str(f.get("role_ru") or ""))
        if role_warning and role_warning not in p.warnings:
            p.warnings.append(role_warning)
        key = (f["role_ru"], str(f.get("material") or "").strip().casefold())
        if key in seen_finishes:
            continue
        seen_finishes.add(key)
        unique.append(f)
    p.finishes = unique

    # Сверка с первоисточником: выдуманные отделки убираем, а не показываем.
    p.finishes, invented = _verify_finishes(p.finishes, sources)
    if invented:
        p.warnings.append(
            "Не найдены в источнике и убраны из карточки: "
            + ", ".join(invented)
            + ". Заполните отделки вручную."
        )

    if not (p.width_cm and p.depth_cm and p.height_cm):
        p.warnings.append("Габариты найдены не полностью — проверьте по источнику.")
    elif not p.dims_confident:
        p.warnings.append(
            "В источнике нет пометки высоты — Д/Г/В расставлены предположительно, "
            "сверьте со строкой размеров."
        )
    elif _dims_disagree(p):
        p.warnings.append(
            "Габариты по осям расходятся со строкой размеров — сверьте Д/Г/В."
        )
    if not p.photo_urls:
        p.warnings.append("Фотографии не найдены.")
    if not p.type_ru:
        p.warnings.append("Тип предмета не определён — выберите вручную.")

    return p
