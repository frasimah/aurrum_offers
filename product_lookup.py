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
from urllib.parse import urljoin
from dataclasses import dataclass, field


import extract
import gallery
import safe_fetch
import shopify

# Контролируемый словарь: модель выбирает из списка, а не переводит свободно.
TYPES_RU = [
    "Кресло", "Диван", "Кровать", "Торшер", "Люстра", "Бра",
    "Настольная лампа", "Стол", "Стул", "Банкетка", "Пуф",
    "Тумбочка прикроватная", "Комод", "Шкаф", "Стеллаж", "Ковёр",
    "Зеркало", "Мебель для кухни", "Другое",
]

_TYPES_LOWER = {t.lower(): t for t in TYPES_RU}

# «Основание» и «Полки» добавлены после живых прогонов: BENTLEY EMBRACE
# отдал «Основание» (лакированное основание дивана), FENDI ARPEGGIO —
# «Полки». Обе роли настоящие и различимые, а падали в общую «Отделку».
ROLES_RU = [
    "Обивка", "Отделка", "Металл", "Топ", "Кант", "Камень",
    "Стекло", "Дерево", "Ножки", "Каркас", "Фасады",
    "Основание", "Полки",
]

# Коэффициент упаковки и шаг округления — как в рабочей книге:
# U = ROUNDUP(Д×Г×В×1.5/1000000; 1)
class NoDelivery(RuntimeError):
    """Обычный запрос не прошёл, а доставщик не настроен."""


# Единица выводится по величине только при двух условиях сразу: самое
# крупное число не меньше MM_FLOOR (мебели длиной 10 метров одной
# позицией не бывает) И самое мелкое не меньше MM_MIN_AXIS. Второе
# условие обязательно: одного крупного числа мало, потому что в строке
# размеров попадается артикул, год или вес — «ART.1200 145x45x47».
# По одному только максимуму такая строка делилась на десять и давала
# 120 x 14,5 x 4,5 вместо 145 x 45 x 47, то есть объём терялся, а вместе
# с ним и перевозка. У настоящей миллиметровой записи крупны все оси:
# в книге 2867 это 2415 / 780 / 2685 и 2136 / 331 / 1216.
MM_FLOOR = 1000
MM_MIN_AXIS = 100
# Потолок правдоподобия объёма. Самое крупное из книг — 6,8 м³
# (TRUSSARDI VIBES 202x241x92) и 7,6 м³ у кухни MODULNOVA. Двенадцать
# оставляет полуторный запас и при этом ловит настоящие ошибки:
# «L2415/2805/2415 H2685 мм» разбирается в 27,3 м³ и до сих пор шло
# молча. Это порог для знака, а не для пересчёта: переводим только
# когда уверены, а сомневаемся — вслух.
MAX_PLAUSIBLE_M3 = 12

PACKING_FACTOR = 1.5
VOLUME_STEP = 0.1

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
    summary_ru: str = ""             # русское саммари фактов, до 400 знаков
    photo_urls: list[str] = field(default_factory=list)
    doc_urls: list[str] = field(default_factory=list)
    spec_pdf_url: str = ""
    variants: list[dict] = field(default_factory=list)   # исполнения со страницы
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
    # Модель отвечает и парой через слэш («Банкетка / пуф» у LONGHI), и с
    # уточнением («Стол обеденный» у HENGE). Оба слова наши, а строка целиком
    # не совпадает ни с чем — и товар молча уезжал в «Другое». Берём первое
    # узнанное слово, но говорим вслух, что взяли не всё.
    for part in re.split(r"[/,;]|\bили\b", value):
        for word in (part.strip(), part.strip().split()[0] if part.strip() else ""):
            hit = _TYPES_LOWER.get(word.lower())
            if hit:
                return hit, f"Тип со страницы — «{value}», взят «{hit}»."
    return "Другое", f"Тип «{value}» не из нашего списка — поставлен «Другое»."


# Раздел сайта — сильный сигнал о типе, и он не используется извлечением:
# у EMMEMOBILI страница /prodotti/contenitori/ («Storage units», в тексте
# прямо «sideboard») приезжала как «Стол». Список нарочно короткий —
# только однозначные разделы. Токены — регулярные выражения: голое
# «table» совпадало с /table-lamps/, и настольная лампа BAROVIER AURORA
# становилась «Столом» — раздел перекрывал верное извлечение.
#
# Свет здесь есть, но только разделы с способом монтажа в имени:
# chandeliers, table-lamps, floor-lamps, wall-lamps однозначны сами.
_URL_TYPES = {
    "Комод": ("contenitori", "storage", "sideboard", "credenz", "madia", "dresser"),
    "Кровать": ("letti", "bed", "beds"),
    "Стул": ("sedie", "chair", "chairs"),
    "Кресло": ("poltrone", "armchair", "armchairs"),
    "Стеллаж": ("librerie", "bookcase", "shelving"),
    "Диван": ("divani", "sofa", "sofas"),
    # «table» — только не перед «-lamp»; «tavolo» — только не после «da-»
    "Стол": (r"(?<!da-)tavol", r"tables?(?!-?lamps?)"),
    "Тумбочка прикроватная": ("comodini", "bedside", "nightstand"),
    "Ковёр": ("tappeti", "rug", "rugs", "carpet"),
    "Зеркало": ("specchi", "mirror", "mirrors"),
    "Люстра": ("chandelier", "sospensioni", "suspension"),
    "Настольная лампа": ("table-lamp", "lampade-da-tavolo"),
    "Торшер": ("floor-lamp", "lampade-da-terra"),
    "Бра": ("wall-lamp", "applique"),
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


_NUM = r"\d+(?:\.\d+)?"


def _strip_noise(s: str) -> str:
    """Убрать из строки всё, что размером не является.

    Артикулы и коды моделей («ART.1200», «cod. 8801») стоят рядом с
    размерами и становились осью. Числа с нелинейной размерностью —
    вес, световой поток, мощность, цветовая температура — тоже.
    """
    s = re.sub(r"\b(?:art|cod|ref|sku|mod|model|арт|код|модель)\.?\s*\d+\S*",
               " ", s, flags=re.I)
    s = re.sub(rf"{_NUM}\s*(?:kg|g|гр|кг|lm|лм|вт|mah|pcs|шт)\b", " ", s, flags=re.I)
    s = re.sub(rf"{_NUM}\s*°\s*[KК]\b", " ", s)
    # Цветовая температура пишется и без градуса: «3000K», «2700 K».
    s = re.sub(rf"{_NUM}\s*[KК](?![a-zA-Zа-яА-Я])", " ", s)
    return s


def _dimension_numbers(raw: str) -> list[float]:
    """Числа строки, которые действительно являются размерами.

    Ровно те же, что уйдут в оси. Решение о единице измерения обязано
    приниматься по ним, а не по всей строке: иначе артикул «ART. 1200»
    рядом со шкафом 200x120x240 делает из сантиметров миллиметры, и
    объём падает с 8,7 м³ до 0,1 — то есть перевозка исчезает.
    """
    s = _strip_noise((raw or "").replace(",", ".").replace("×", "x").replace("Х", "x"))
    s = re.sub(rf"\d+(?:\s+\d+\s*/\s*\d+)?\s*(?:\"|''|″|\bin\b)", " ", s)
    chain = _dimension_chain(s, _NUM)
    if len(chain) >= 2:
        return chain
    inches = [float(x) for x in re.findall(rf"\(\s*({_NUM})", s)]
    return [float(x) for x in re.findall(_NUM, s) if float(x) not in inches]


def _dimension_chain(s: str, num: str) -> list[float]:
    """Самая длинная цепочка чисел, связанных знаком «x».

    Числа, соединённые «x», — это заведомо габариты. Всё, что стоит в
    строке само по себе (артикул, год, вес), к осям отношения не имеет,
    но раньше попадало в общий котёл и вытесняло настоящие размеры:
    «ART.1200 145x45x47» давало длину 1200 вместо 145.
    """
    best: list[float] = []
    for m in re.finditer(rf"{num}(?:\s*x\s*{num})+", s, re.I):
        got = [float(x) for x in re.findall(num, m.group(0))]
        if len(got) > len(best):
            best = got
    return best


# Границы ищем по буквам, а не через \b: в «H100cm» между «0» и «c»
# границы слова нет, и единица, честно напечатанная на сайте, считалась
# неназванной — а дальше цветовая температура «3000K» из той же строки
# делала из сантиметров миллиметры.
_UNIT = r"(?<![a-zA-Zа-яА-Я])(?:{})(?![a-zA-Zа-яА-Я])"
_MM_WORD = re.compile(_UNIT.format("mm|мм"), re.I)
_CM_WORD = re.compile(_UNIT.format("cm|см"), re.I)


def dims_unit_stated(raw: str) -> bool:
    """Названа ли единица измерения в самой строке размеров."""
    s = raw or ""
    return bool(_MM_WORD.search(s) or _CM_WORD.search(s))


def _looks_like_millimetres(numbers: list[float]) -> bool:
    """Числа настолько крупны, что сантиметрами быть не могут.

    Смотрим на весь набор, а не на максимум: крупным должно быть и самое
    мелкое число тоже. Иначе артикул «ART.1200» или год «Mod. 2024»
    внутри строки делает миллиметры из обычных сантиметров.
    """
    # Двух чисел довольно: «Ø1200 H750» — это полный набор для круглого
    # стола, и раньше он проходил мимо правила и давал 1620 м³.
    if len(numbers) < 2:
        return False
    return min(numbers) >= MM_MIN_AXIS and max(numbers) >= MM_FLOOR


def dims_unit_guessed(raw: str) -> bool:
    """В строке размеров нет единицы измерения, а числа для сантиметров велики.

    Перевод мм->см держался на одном слове «mm» внутри свободной строки,
    которую пишет модель, — притом что промпт ей прямо запрещает
    нормализовать единицы («COPY, DO NOT REWRITE… do not convert units»).
    Потеряется слово — объём вырастает в тысячу раз: «L2415 D780 H2685»
    без «мм» давало 7586 м³ и счёт за перевозку 3 793 350 € вместо 3 800 €.
    И помечалось это «уверенно», потому что оси-то были подписаны: флаг
    уверенности отвечал за раскладку по осям и про единицу ничего не знал.

    Порог MM_FLOOR оставляет в покое всё, что бывает в сантиметрах:
    ковёр 500x400 и стол на четыре метра читаются как раньше.
    """
    if dims_unit_stated(raw):
        return False
    return _looks_like_millimetres(_dimension_numbers(raw))


def volume_m3(w: float | None, d: float | None, h: float | None) -> float | None:
    """Объём по формуле рабочей книги: ROUNDUP(Д×Г×В×1.5/1e6; 1)."""
    if not (w and d and h):
        return None
    raw = w * d * h * PACKING_FACTOR / 1_000_000
    # round снимает артефакт float: 6.800000000000001 -> 6.8
    return round(math.ceil(raw / VOLUME_STEP) * VOLUME_STEP, 2)


@dataclass
class Measured:
    """Оси, объём и повод усомниться — всё, что даёт строка размеров."""
    width_cm: float | None = None
    depth_cm: float | None = None
    height_cm: float | None = None
    volume_m3: float | None = None
    volume_source: str = ""
    confident: bool = False
    warnings: list[str] = field(default_factory=list)


def measure(dims_raw: str, type_ru: str = "", declared=None) -> Measured:
    """Строка размеров -> оси, объём и предупреждения.

    Единственное место, где из строки получается объём. Раньше это
    считалось в трёх — на странице товара, при разборе техлиста и в
    запасном разборе по тексту, — и отбивки, поставленные в одну,
    в двух других не работали. А техлист как раз и есть родина
    миллиметровой записи: «Ø1200 H750» давало 1620 м³ и перевозку
    810 000 € при чистой карточке без единого знака.
    """
    m = Measured()
    if not dims_raw:
        return m

    w, d, h, sure = parse_dims(dims_raw, type_ru)
    m.width_cm, m.depth_cm, m.height_cm, m.confident = w, d, h, sure

    if isinstance(declared, (int, float)) and declared > 0:
        m.volume_m3, m.volume_source = round(float(declared), 2), "производитель"
    else:
        calc = volume_m3(w, d, h)
        if calc:
            m.volume_m3, m.volume_source = round(calc, 2), "расчёт по габаритам"

    # Единица не названа, но числа для сантиметров велики. Пересчёт уже
    # сделан в parse_dims — здесь только говорим об этом вслух: вывод
    # однозначен, но это вывод, а цена ошибки тысячекратная.
    if dims_unit_guessed(dims_raw):
        m.confident = False
        m.warnings.append(
            f"В строке «{dims_raw}» единица измерения не указана, а числа "
            "для сантиметров велики — прочитаны как миллиметры. Сверьте "
            "с источником."
        )

    # Потолок правдоподобия — последняя отбивка, на всё остальное,
    # включая объём, объявленный производителем.
    if m.volume_m3 and m.volume_m3 > MAX_PLAUSIBLE_M3:
        m.confident = False
        m.warnings.append(
            f"Объём {m.volume_m3} м³ неправдоподобен для одной позиции "
            f"(потолок {MAX_PLAUSIBLE_M3} м³). Чаще всего это единица "
            "измерения. Перевозка считается от объёма, проверьте габариты."
        )
    return m


def variant_cards(p: "Product") -> list[dict]:
    """Исполнения со страницы — с посчитанными осями и объёмом.

    Ровно та же форма, что у кандидатов из техлиста
    (extract_agent.to_candidates). Раньше кнопка «Подставить» у
    исполнения несла только строку размеров, и оси с объёмом
    оставались от ПЕРВОГО исполнения: у HENGE Sisma подстановка
    320x150x75 считалась по 3,1 м³ вместо 5,4 — перевозка 1550 €
    вместо 2700, недобор 1150 € на позиции. Хуже, что в описании
    клиенту при этом печаталось 320x150x75, то есть файл заявлял
    один размер и был оценён по другому.
    """
    cards = []
    for v in p.variants:
        raw = str(v.get("dims_raw") or "").strip()
        m = measure(raw, p.type_ru, v.get("packed_volume_m3"))
        cards.append({
            "sku": str(v.get("sku") or "").strip() or None,
            "value": raw,
            "context": str(v.get("variant_note") or "").strip(),
            "width_cm": m.width_cm, "depth_cm": m.depth_cm,
            "height_cm": m.height_cm,
            "volume_m3": m.volume_m3, "volume_source": m.volume_source,
            "dims_confident": m.confident, "warnings": m.warnings,
        })
    return cards


def to_excel_description(p: Product) -> str:
    """Текст для колонки «Описание» — в том же формате, что в книге.

    Отделки с одной ролью книга пишет одной строкой через « + », а названия
    материалов — заглавными: «Отделка - Металл LIGHT BURNISHED BRASS + ...».

    Хвоста «Фото из Каталога» здесь нет намеренно: в книге он встречается,
    но приписывать его каждой позиции не нужно — ставится по месту.
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

    return "\n".join(lines).strip()


def _client():
    """Доставщик Firecrawl. Создаётся лениво — только для запасного пути.

    Раньше клиент делался в начале каждого разбора, и без ключа не
    работало НИЧЕГО, включая семь брендов из восьми, которые отдают
    страницу обычным запросом. Теперь ключ нужен ровно там, где без
    него не обойтись: сайты, отвечающие на простой запрос отказом.
    """
    key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if not key:
        raise NoDelivery(
            "Сайт не отдал страницу обычным запросом, а доставщик не "
            "настроен: добавьте FIRECRAWL_API_KEY в .env (образец — "
            "в .env.example). Остальные бренды работают и без него."
        )
    from firecrawl import Firecrawl
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


# Сколько текста должно прийти обычным запросом, чтобы считать страницу
# полученной. Ниже этого — оболочка без товара, идём за отрисовкой.
MIN_PAGE_TEXT = 1500
_TAGS = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)
_ANY_TAG = re.compile(r"<[^>]+>")
_HREF = re.compile(r'(?:href|src)\s*=\s*["\']([^"\']+)', re.I)
BROWSER = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/120.0 Safari/537.36",
           "Accept-Language": "en,it;q=0.9,ru;q=0.8"}


def _plain(url: str) -> tuple[str, list[str], str] | None:
    """Страница обычным запросом. None — не вышло, нужен доставщик."""
    try:
        got = safe_fetch.get(url, timeout=40, headers=BROWSER)
    except Exception:            # noqa: BLE001 — 403, таймаут, что угодно
        return None
    html = got.text
    text = " ".join(_ANY_TAG.sub(" ", _TAGS.sub(" ", html)).split())
    if len(text) < MIN_PAGE_TEXT:
        return None
    links = [urljoin(url, u) for u in _HREF.findall(html)]
    return text, links, html


def _scrape(url: str, fc=None, timeout_ms: int = 120_000) -> tuple[str, list[str], str]:
    """Доставка страницы: сначала обычным запросом, потом Firecrawl.

    Порядок изменён после замера на восьми брендах. Firecrawl — не
    всегда улучшение: у HENGE он возвращал ОДНО меню навигации, тысячу
    знаков вместо тридцати пяти, и карточка выходила пустой; у
    EMMEMOBILI терял размеры. Обычный запрос отдал товар у семи брендов
    из восьми и на порядок быстрее (0,6 с против 8).

    Firecrawl остаётся для того, ради чего он и брался: VENICEM отвечает
    на простой запрос 403, и без отрисовки с антиботом его не взять.
    """
    plain = _plain(url)
    if plain is not None:
        return plain

    doc = (fc or _client()).scrape(url, formats=["markdown", "links", "html"],
                    only_main_content=False, timeout=timeout_ms)
    data = _as_dict(doc)
    links = [u for u in (data.get("links") or []) if isinstance(u, str)]
    return str(data.get("markdown") or ""), links, str(data.get("html") or "")


_MD_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")
_IMAGE_EXT = re.compile(r"\.(?:jpe?g|png|webp|avif)(?:$|\?)", re.I)


def _photos_from(markdown: str, links: list[str]) -> list[str]:
    """Все адреса картинок со страницы, без отбора.

    Собираем сами: адрес картинки выдумать нельзя, а извлечение на этом
    месте теряло часть галереи. Отбор — отдельным шагом, когда известна
    модель (см. `_clean_photos`).
    """
    urls = _MD_IMAGE.findall(markdown or "")
    urls += [u for u in links if _IMAGE_EXT.search(u.split("?")[0])]
    return urls


# Адреса, за которыми лежит документ, хотя расширения в них нет.
# Список нарочно узкий: сюда попадает не всякая ссылка с «sheet» в тексте,
# а только та, что сама себя объявляет генератором технического листа.
_GENERATED_DOC = ("generatetechnicalsheet", "technicalsheet", "/scheda-tecnica",
                  "generatepdf", "download=pdf", "format=pdf")


def _docs_from(links: list[str]) -> list[str]:
    """Документы изделия. Бумаги сайта сюда не попадают.

    На каждой странице магазина висит заявление о доступности, у BAROVIER —
    политика информирования о нарушениях. К предмету они отношения не имеют
    и в списке документов только мешают.

    Инструкция по сборке при этом остаётся: она про изделие, просто
    не техлист (см. `_pick_spec_pdf`).
    """
    seen, out = set(), []
    for u in links:
        low = u.lower()
        path = low.split("?")[0]
        # Документ не обязан оканчиваться на .pdf: LONGHI отдаёт техлист
        # по адресу вида …/GenerateTechnicalSheet?catalog=…&id=… — обычный
        # PDF, но без расширения. Пока правило было «оканчивается на .pdf»,
        # каталожные габариты терялись у целого бренда.
        looks_like_doc = path.endswith(".pdf") or any(
            token in low for token in _GENERATED_DOC)
        if not looks_like_doc or u in seen:
            continue
        name = path.rsplit("/", 1)[-1]
        if any(token in name for token in _SITE_PAPERS):
            continue
        seen.add(u)
        out.append(u)
    return out


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


def _dims_grounded(dims_raw: str, sources: str) -> bool:
    """Встречаются ли числа габаритов в тексте источника.

    Проверять обязательно, и это не перестраховка. У FENDI CASA страница
    не публикует размеров вовсе — ни одного «NN cm», — а извлечение вернуло
    «высота: 83 см, ширина: 81 см, глубина: 88 см». Правдоподобные числа
    для кресла, которых на сайте нет. Карточка показала их как уверенные
    и посчитала объём, а объём — это деньги за транспорт.

    Сверяем строку размеров, а не разложенные оси: оси считает Python,
    и если исходная строка настоящая, то и они настоящие.
    """
    numbers = [n for n in re.findall(r"\d{2,4}", dims_raw or "")]
    if not numbers:
        return True                      # нечего проверять
    present = set(re.findall(r"\d{2,4}", sources or ""))
    # Хватает большинства: источник может округлять или опускать одно значение.
    hits = sum(1 for n in numbers if n in present)
    return hits >= max(1, len(numbers) - 1)


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


# Документы, которые к товару отношения не имеют. У Shopify-сайтов на
# каждой странице висит заявление о доступности, у BAROVIER — политика
# информирования о нарушениях. Взять «первый PDF» означает искать
# габариты в юридическом тексте.
# Бумаги самого сайта: к изделию отношения не имеют, в список документов
# не попадают вовсе.
_SITE_PAPERS = ("accessibility", "privacy", "cookie", "policy",
                "whistleblowing", "terms", "gdpr")

# Документы изделия, которые техлистом не являются: инструкцию по сборке
# показываем, но габариты ищем не в ней.
_NOT_A_SPEC = _SITE_PAPERS + ("warranty", "assembly", "instruction",
                              "montaggio", "manual")
_LOOKS_LIKE_SPEC = ("fact_sheet", "fact-sheet", "spec", "scheda", "technical",
                    "datasheet", "tech")


# Имена, за которыми лежит общий каталог бренда, а не лист позиции.
# У FLOU это `flou_catalogue_scheda.pdf` — в имени есть «scheda», из-за
# чего он выигрывал у настоящего листа `madamebutterfly_265.pdf`. Весит
# такой каталог 155 МБ и в разбор не помещается вовсе.
_WHOLE_CATALOGUE = ("catalogue", "catalogo", "katalog", "cataloghi",
                    "lookbook", "pricelist", "listino")


def spec_pdf_candidates(doc_urls: list[str], model: str = "") -> list[str]:
    """Техлисты по убыванию правдоподобия.

    Список, а не один адрес: угадать с первого раза нельзя, зато можно
    попробовать следующий, если предыдущий не разобрался. Раньше выбор
    был единственным, и промах стоил целой строки габаритов.
    """
    named = [(u, u.split("?")[0].rsplit("/", 1)[-1].lower()) for u in doc_urls]
    clean = [(u, n) for u, n in named if not any(t in n for t in _NOT_A_SPEC)]
    if not clean:
        return []

    slug = _slug(model).replace("-", "")

    def rank(pair) -> int:
        url, name = pair
        flat = name.replace("_", "").replace("-", "")
        # Каталог целиком — всегда последний: он и не про эту позицию,
        # и не помещается в разбор.
        if any(t in name for t in _WHOLE_CATALOGUE):
            return 4
        # Имя модели в файле — самый сильный признак листа позиции.
        if slug and len(slug) >= 4 and slug in flat:
            return 0
        if any(t in name for t in _LOOKS_LIKE_SPEC):
            return 1
        return 2

    return [u for u, _ in sorted(clean, key=rank)]


def _pick_spec_pdf(doc_urls: list[str], model: str = "") -> str:
    """Самый правдоподобный техлист. Пусто, если подходящего нет."""
    candidates = spec_pdf_candidates(doc_urls, model)
    return candidates[0] if candidates else ""


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

    # Подписи словами приводим к символам: на сайтах пишут «Height 60 cm,
    # Diameter 93 cm», и без этого два числа неразличимы — у люстры высота
    # уезжала в ширину. Слово H в «Height» разбору не мешает: пометку
    # высоты мы ищем только там, где за H не идёт буква.
    _H = r"height|altezza|hauteur|h[oö]he|высота|выс\.?"
    _D = r"diameter|diametro|diam[eè]tre|durchmesser|диаметр|диам\.?"
    # Подпись после числа: «90 cm (высота)» — тоже встречается, и часто
    # в одном ответе с обратным порядком.
    s = re.sub(rf"({num})\s*(?:cm|см|mm|мм)?\s*\(\s*(?:{_H})\s*\)", r" H \1 ", s, flags=re.I)
    s = re.sub(rf"({num})\s*(?:cm|см|mm|мм)?\s*\(\s*(?:{_D})\s*\)", r" Ø \1 ", s, flags=re.I)
    # Подпись перед числом: «Height 60 cm, Diameter 93 cm»
    s = re.sub(rf"\b(?:{_H})\b\s*:?\s*", " H ", s, flags=re.I)
    s = re.sub(rf"\b(?:{_D})\b\s*:?\s*", " Ø ", s, flags=re.I)

    # Пометка высоты в скобках вплотную к числу: «300x90x75(h)cm» у HENGE.
    # Скобка не давала сработать ни одному шаблону, и оси у настоящего
    # трёхметрового стола помечались как расставленные наугад.
    s = re.sub(r"\(\s*([HНh])\s*\)", r" \1 ", s)

    # Артикулы, вес, световой поток, цветовая температура — всё, что
    # размером не является, но стоит в той же строке.
    s = _strip_noise(s)

    # Дюймовые двойники выкидываем целиком. Раньше отбрасывались только те,
    # что в скобках, и у PORADA «43 1/4"» через запятую попадало в общий
    # котёл чисел — высотой оказывался второй диаметр вместо 75 см.
    s = re.sub(r"\d+(?:\s+\d+\s*/\s*\d+)?\s*(?:\"|''|″|\bin\b)", " ", s)

    # Явная пометка высоты: «H 125», «...x125Н», «125 cm H».
    # Подпись перед числом проверяем первой: у PORADA пишут
    # «Ø130 - 140 - 150 - 160 h 75», и обратный шаблон принимал «160 h»
    # за высоту, хотя 160 — это диаметр, а высота 75.
    h_match = (
        re.search(rf"[HНh]\s*({num})", s)
        or re.search(rf"({num})\s*(?:cm|см|mm|мм)?\s*[HНh](?![a-zA-Zа-яА-Я])", s)
    )
    height = float(h_match.group(1)) if h_match else None

    # Диаметр бывает диапазоном — и через дробь «D25/31», и через тире
    # «Ø110 - 120». Берём наибольшее значение.
    # Голая буква D — диаметр только там, где нет пометок длины и
    # ширины. В нотации «L2415 D780 H2685» (стандарт итальянских
    # техлистов) D — это глубина, и чтение её диаметром съедало длину:
    # кухня 241x78x268 выходила 78x78x268. Знак Ø однозначен всегда.
    has_l_or_w = re.search(r"\b(?:L|W|length|width|larghezza|lunghezza)\b|"
                           r"\bL\d|\bW\d", s, re.I) is not None
    diameter_marks = "Øø" if has_l_or_w else "ØøD"

    diameters: list[float] = []
    for group in re.findall(rf"[{diameter_marks}]\s*({num}(?:\s*[/\-–—]\s*{num})*)", s):
        diameters += [float(x) for x in re.findall(num, group)]
    all_nums = [float(x) for x in re.findall(num, s)]
    # Размеры в дюймах идут в скобках — они не нужны
    inches = [float(x) for x in re.findall(rf"\(\s*({num})", s)]
    all_nums = [n for n in all_nums if n not in inches]
    # Если размеры записаны через «x», берём ровно их: постороннее
    # число рядом (год, номер) больше не участвует в раскладке осей.
    chain = _dimension_chain(s, num)
    if len(chain) >= 2:
        all_nums = chain

    # Единица измерения. Названа — верим строке. Не названа — решаем по
    # тем же числам, что уйдут в оси. Раньше решение принималось по
    # сырой строке ДО чистки, и артикул «ART. 1200» рядом со шкафом
    # 200x120x240 делал из сантиметров миллиметры: объём падал с 8,7 м³
    # до 0,1, то есть перевозка исчезала. Недобор тише перебора и
    # потому опаснее — счёт на миллион менеджер заметит, а пропавшую
    # тысячу нет.
    if dims_unit_stated(raw):
        millimetres = bool(_MM_WORD.search(s)) and not _CM_WORD.search(s)
    else:
        millimetres = _looks_like_millimetres(
            all_nums + [n for n in diameters if n not in all_nums])

    if millimetres:
        # Делим до раскладки по осям: иначе пометки и диаметры пришлось
        # бы пересчитывать в трёх местах.
        height = height / 10 if height is not None else None
        diameters = [n / 10 for n in diameters]
        all_nums = [n / 10 for n in all_nums]

    if height is not None:
        # Убираем ОДНО вхождение высоты, а не все числа, равные ей.
        # Отбор по значению съедал горизонталь, численно равную высоте:
        # «Height 51 Width 51 Depth 4» давало 4 x 4 x 51, а шкаф
        # 80 x 40 x 80 — 40 x 40 x 80, то есть вдвое уже и вдвое дешевле
        # по транспорту. И всё это помечалось «уверенно», так что повода
        # перепроверить не возникало. Совпадение размеров у мебели —
        # обычное дело: квадратный пуф, куб-тумба, зеркало, стол 90x90.
        horizontal = list(all_nums)
        if height in horizontal:
            horizontal.remove(height)
        if diameters:
            d = max(diameters)
            return d, d, height, True
        if len(horizontal) >= 2:
            return horizontal[0], horizontal[1], height, True
        if horizontal:
            return horizontal[0], horizontal[0], height, True
        return None, None, height, False

    # Пометки высоты нет. Числа при этом настоящие — со страницы, их
    # стережёт _dims_grounded, — вопрос только в раскладке по осям.
    # Когда все числа, кроме одного, помечены диаметрами, оставшемуся
    # больше нечем быть, кроме высоты: это вывод, а не догадка.
    rest = [n for n in all_nums if n not in diameters]
    if diameters and len(rest) == 1:
        d = max(diameters)
        return d, d, rest[0], True
    if diameters and rest:
        d = max(diameters + [n for n in rest if n < max(rest)])
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


def _slug(text: str) -> str:
    """«Circle Floor» -> «circlefloor». Разделители снимаем, чтобы
    «circle-floor» в адресе и «Circle Floor» в названии сошлись."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


_WIDTH_PARAM = re.compile(r"[?&]width=(\d+)", re.I)

# Служебная графика: логотипы, иконки, заглушки.
_JUNK_WORDS = {"logo", "logos", "icon", "icons", "sprite", "sprites",
               "placeholder", "spinner", "avatar", "favicon", "badge", "flag"}


def _is_junk(url: str) -> bool:
    """Служебная ли это картинка.

    Сравниваем целыми словами, а не подстрокой. Подстрока ошибалась
    дорого и незаметно: «iconic-collection» отбрасывался из-за «icon»,
    «spinello-table» из-за «spin». В мебельных каталогах «iconic»
    встречается постоянно, и так терялись снимки самого изделия.

    Правило нарочно узкое: отбрасываем, только если служебным словом
    названо всё изображение целиком («logo.png») или так назван каталог
    («/icons/»). Из двух ошибок дороже вторая: лишний снимок менеджер
    снимет галочкой, а пропавший он не увидит вовсе — и не узнает, что
    тот был. Поэтому кресло с именем «avatar-lounge-chair» остаётся.
    """
    path = url.split("?")[0].lower()
    segments = [s for s in path.split("/") if s]
    if any(seg in _JUNK_WORDS for seg in segments[:-1]):
        return True
    name = segments[-1] if segments else ""
    return name.rsplit(".", 1)[0] in _JUNK_WORDS


def _largest_of_each(urls: list[str]) -> list[str]:
    """Один снимок — одна плитка, в наибольшем доступном размере.

    Магазины отдают одну и ту же картинку несколько раз с разным
    `width`: у BENTLEY рядом лежали варианты на 1946 и на 54 пикселя.
    Порядок сохраняем: галерея выстроена не случайно.
    """
    best: dict[str, tuple[int, str]] = {}
    order: list[str] = []
    for url in urls:
        path = url.split("?")[0]
        match = _WIDTH_PARAM.search(url)
        width = int(match.group(1)) if match else 0
        if path not in best:
            order.append(path)
            best[path] = (width, url)
        elif width > best[path][0]:
            best[path] = (width, url)
    return [best[path][1] for path in order]


def _clean_photos(urls: list[str], model: str = "") -> list[str]:
    """Фотографии именно этого изделия.

    Одного отсева служебной графики мало: страница отдаёт всю галерею
    раздела. У PORADA приезжало 72 снимка, из них к изделию относился
    21 — остальное чужие товары. У VENICEM из восьми лишними оказались
    образцы металла и три других светильника той же серии.

    Разделяет их название модели в имени файла. Признак сильный и
    дешёвый: производители называют файлы по модели. Полное название
    важно — короткое «circle» захватило бы потолочный, настольный и
    настенный светильники, а «circle-floor» оставляет только напольный.

    Если по названию не нашлось ничего, отдаём всё: лучше показать
    лишнее, чем пустой блок.
    """
    out, seen = [], set()
    for url in urls:
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        if url.split("?")[0].lower().endswith(".svg"):   # схемы, а не фото
            continue
        if _is_junk(url) or url in seen:
            continue
        seen.add(url)
        out.append(url)

    out = _largest_of_each(out)

    # Короткое название не фильтр, а лотерея: «Pin» найдётся в любом адресе.
    slug = _slug(model)
    if len(slug) < 4:
        return out
    matched = [u for u in out if slug in _slug(u.rsplit("/", 1)[-1])]
    return matched or out


def _first_product(data: dict) -> dict:
    products = data.get("products") or []
    return _as_dict(products[0]) if products else {}


def lookup(url: str) -> Product:
    """Ссылка на товар -> карточка.

    Firecrawl доставляет страницу, `extract` её разбирает. Если
    у товара есть техлист — он забирается тем же извлекателем и имеет
    приоритет: там исполнения приходят с артикулами, а объём бывает
    указан производителем.
    """
    p = Product(source_url=url)

    page_md, links, page_html = _scrape(url)
    if not page_md.strip():
        raise RuntimeError(
            "Страница не отдала содержимого. Проверьте ссылку — возможно, "
            "сайт закрыт от автоматических запросов."
        )

    p.doc_urls = _docs_from(links)

    # Если магазин на Shopify, список фотографий отдаёт он сам — точный,
    # без чужих товаров и образцов материалов. Отбор по именам файлов
    # остаётся только там, где такого источника нет.
    shop = shopify.fetch(url)
    if not shop and shopify.is_shop(url):
        # Магазин известен, а карточка не пришла: ответил ошибкой или сменил
        # движок. Для семи марок это основной источник фотографий, и
        # подменять его угадыванием молча нельзя.
        p.warnings.append(
            "Штатная карточка магазина не открылась — фотографии отобраны "
            "запасным способом. Проверьте их состав."
        )
    by_selector = None if shop else gallery.photos(page_html, url)
    if shop:
        photo_candidates, photo_source = shopify.photos(shop), "магазин"
    elif by_selector:
        photo_candidates, photo_source = by_selector, "разметка"
    else:
        photo_candidates, photo_source = _photos_from(page_md, links), "имена файлов"
        if by_selector == []:
            # Правило есть, но не сработало: сайт переверстали. Молчать
            # нельзя — иначе мы вернёмся к угадыванию, не зная об этом.
            p.warnings.append(
                "Разметка галереи изменилась — фотографии отобраны запасным "
                "способом, по именам файлов. Проверьте их состав."
            )

    # Список типов передаём: ответ вне его — повод переспросить у тяжёлой
    # модели, а не молча уронить тип в «Другое».
    extracted = extract.from_text(page_md, known_types=TYPES_RU)
    source = str(extracted.get(extract.SOURCE_KEY) or "")
    if source and not source.startswith(extract.SOURCE_MAIN):
        p.warnings.append(
            f"Основной разбор не ответил, карточку собрал запасной путь — "
            f"{source}. Он читает только текст: размеры с чертежей и схем "
            f"в него не попадают. Сверьте габариты и отделки с источником."
        )
    page = _first_product(extracted)
    p.brand = str(extracted.get("brand") or "").strip()
    p.model = str(page.get("model") or "").strip()
    p.collection = str(page.get("collection") or "").strip()

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

    if shop:
        # Магазин знает своё лучше извлечения: производителя и название
        # берём у него, отбор фотографий не нужен вовсе.
        p.brand = str(shop.get("vendor") or "").strip() or p.brand
        p.model = str(shop.get("title") or "").strip() or p.model
        p.photo_urls = _clean_photos(photo_candidates)
    elif photo_source == "разметка":
        p.photo_urls = _clean_photos(photo_candidates)
    else:
        # Отбираем фото по названию модели: оно в имени файла отделяет
        # изделие от остальной галереи раздела.
        p.photo_urls = _clean_photos(photo_candidates, p.model)
        if photo_candidates and len(p.photo_urls) < len(photo_candidates):
            p.warnings.append(
                f"Из {len(photo_candidates)} снимков на странице оставлено "
                f"{len(p.photo_urls)} — с названием модели в имени файла. "
                "Если нужного нет, проверьте страницу."
            )

    p.tech_note = str(page.get("tech_note") or "").strip()

    # Саммари — единственное поле, которое модель пишет сама, а не
    # переписывает из источника. Держим его коротким принудительно:
    # просьба в промпте — не гарантия, а 400 знаков — обещание интерфейса.
    summary = str(page.get("summary_ru") or "").strip()
    if len(summary) > 400:
        cut = summary[:400]
        # По границе предложения, если она есть в хвосте; иначе по слову.
        dot = cut.rfind(". ")
        summary = cut[:dot + 1] if dot > 200 else cut[:cut.rfind(" ")]
        p.warnings.append("Саммари пришло длиннее 400 знаков — обрезано.")
    p.summary_ru = summary
    p.finishes = [_as_dict(f) for f in (page.get("finishes") or [])]
    p.variants = [v for v in (_as_dict(x) for x in (page.get("variants") or []))
                  if str(v.get("dims_raw") or "").strip()]

    # Техлист: исполнения с артикулами и объём от производителя.
    dims_from_page = True
    axes_sure = True
    candidates = spec_pdf_candidates(p.doc_urls, p.model)
    pdf, failures = {}, []
    # Пробуем по очереди: первый кандидат бывает каталогом на 155 МБ,
    # а лист позиции лежит рядом. Раньше промах стоил строки габаритов.
    for candidate in candidates[:3]:
        try:
            pdf = _first_product(extract.from_url(candidate))
            p.spec_pdf_url = candidate
            break
        except Exception as exc:  # noqa: BLE001 — техлист не критичен
            failures.append(f"{candidate.rsplit('/', 1)[-1][:40]}: {exc}")
            pdf = {}
    if failures and not p.spec_pdf_url:
        p.warnings.append("Техлист не прочитался — " + "; ".join(failures[:2]))
    if p.spec_pdf_url:
        pdf_variants = [v for v in (_as_dict(x) for x in (pdf.get("variants") or []))
                        if str(v.get("dims_raw") or "").strip()]
        if pdf_variants:
            p.variants = pdf_variants
            dims_from_page = False
        if not p.finishes:
            p.finishes = [_as_dict(f) for f in (pdf.get("finishes") or [])]
        if not p.tech_note:
            p.tech_note = str(pdf.get("tech_note") or "").strip()
    else:
        p.warnings.append(
            "Техлист (spec sheet) не найден — габариты и объём проверьте по источнику."
        )

    # Оси и объём считает Python: набор чисел извлечение отдаёт верный,
    # а раскладывает их нестабильно от запроса к запросу.
    if p.variants:
        first = p.variants[0]
        p.dims_raw = str(first.get("dims_raw") or "").strip()
        m = measure(p.dims_raw, p.type_ru, first.get("packed_volume_m3"))
        p.width_cm, p.depth_cm, p.height_cm = m.width_cm, m.depth_cm, m.height_cm
        p.dims_confident = m.confident
        p.volume_m3, p.volume_source = m.volume_m3, m.volume_source
        axes_sure = parse_dims(p.dims_raw, p.type_ru)[3]
        p.warnings.extend(m.warnings)
        p.package_note = str(first.get("package_dims_raw") or "").strip()
        if len(p.variants) > 1:
            # Говорим ИМЕННО какое подставлено, а не «первое». «Первое» —
            # это порядок вёрстки страницы, а не совпадение с заказом: у
            # кровати под другой матрац карточка напечатала бы те же числа
            # и была бы молча неверной. Список показан рядом, в карточке.
            which = " · ".join(x for x in (
                str(first.get("sku") or "").strip(),
                str(first.get("variant_note") or "").strip(),
                p.dims_raw) if x)
            p.warnings.append(
                f"Исполнений {len(p.variants)}, подставлено «{which}» — "
                "порядок со страницы, не выбор. Сверьте с заказом и при "
                "необходимости возьмите другое из списка ниже."
            )

    # Габариты со страницы сверяем с её текстом: извлечение способно вернуть
    # правдоподобные числа, которых на странице нет вовсе (FENDI CASA).
    # У техлиста своего текста под рукой нет, и там проверка не применяется.
    if dims_from_page and p.dims_raw and not _dims_grounded(p.dims_raw, page_md):
        p.warnings.append(
            f"Габариты «{p.dims_raw}» не найдены в тексте страницы — похоже, "
            "извлечение их придумало. Поля очищены, заполните вручную."
        )
        p.dims_raw = ""
        p.width_cm = p.depth_cm = p.height_cm = None
        p.dims_confident = False
        p.variants = []

    if p.volume_m3 is None:
        calc = volume_m3(p.width_cm, p.depth_cm, p.height_cm)
        if calc:
            p.volume_m3, p.volume_source = round(calc, 2), "расчёт по габаритам"

    if not p.brand:
        p.brand = _brand_from_url(url)

    # Роли приводим к нашему списку и схлопываем повторы.
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
    p.finishes, invented = _verify_finishes(p.finishes, page_md)
    if invented:
        p.warnings.append(
            "Не найдены в источнике и убраны из карточки: "
            + ", ".join(invented) + ". Заполните отделки вручную."
        )

    if not (p.width_cm and p.depth_cm and p.height_cm):
        p.warnings.append("Габариты найдены не полностью — проверьте по источнику.")
    elif not axes_sure:
        p.warnings.append(
            "Числа взяты из источника, но пометки высоты там нет — оси "
            "расставлены по порядку и помечены знаком «!». Проверьте раскладку."
        )
    if not p.photo_urls:
        p.warnings.append("Фотографии не найдены.")
    if not p.type_ru:
        p.warnings.append("Тип предмета не определён — выберите вручную.")

    return p
