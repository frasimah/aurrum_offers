# -*- coding: utf-8 -*-
"""
Картинки из техлиста: образцы отделок и чертежи.

Техлист печатает отделки витриной — квадратик материала и подпись под
ним. Текстом из него достаются только названия; сам образец, то есть
единственное, что показывают клиенту, оставался внутри PDF.

Берём его геометрией. Где нарисована картинка, PDF говорит точно
(матрица преобразования перед `Do`), где напечатана подпись — тоже
(матрица строки). Подпись под картинкой и по её ширине — значит она
про неё. Ничего не угадывается: не нашлось — образца нет.
"""

from __future__ import annotations

import io
import re
import unicodedata

# Подпись стоит ПОД образцом и не дальше этого по вертикали.
CAPTION_BAND = 48.0
# ...и не шире половины шага между образцами по горизонтали.
CAPTION_SIDE = 34.0
# Меньше этой доли слов названия в подписи — не наша подпись.
MATCH = 0.6
# Образец — квадратик. Крупная картинка на странице это фотография или
# чертёж, и подпись под ней означает совсем другое.
SWATCH_MAX = 120.0
SWATCH_MIN = 15.0


def _mul(a: list[float], b: list[float]) -> list[float]:
    return [a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5]]


def placements(page, reader) -> list[dict]:
    """Картинки страницы с местом и размером на листе."""
    from pypdf.generic import ContentStream

    try:
        stream = ContentStream(page.get_contents(), reader)
    except Exception:            # noqa: BLE001 — битый поток не повод падать
        return []

    ctm: list[float] = [1, 0, 0, 1, 0, 0]
    stack: list[list[float]] = []
    out: list[dict] = []
    for operands, op in stream.operations:
        code = op.decode() if isinstance(op, bytes) else str(op)
        if code == "q":
            stack.append(list(ctm))
        elif code == "Q":
            ctm = stack.pop() if stack else [1, 0, 0, 1, 0, 0]
        elif code == "cm":
            try:
                ctm = _mul([float(x) for x in operands], ctm)
            except (TypeError, ValueError):
                continue
        elif code == "Do" and operands:
            out.append({"name": str(operands[0]), "x": ctm[4], "y": ctm[5],
                        "w": abs(ctm[0]), "h": abs(ctm[3])})
    return out


_WORD = re.compile(r"[^\w]+", re.UNICODE)


def _words(value: str) -> list[str]:
    # Разбираем лигатуры: в PDF «Camouflage» напечатано через «ﬂ», и
    # без этого название не сходится с тем же словом со страницы.
    plain = unicodedata.normalize("NFKD", value or "")
    return [w for w in _WORD.sub(" ", plain.casefold()).split() if w]


def _flat(value: str) -> str:
    return " ".join(_words(value))


def _reading_order(images: list[dict]) -> list[dict]:
    """Картинки в порядке чтения: сверху вниз, слева направо.

    Строку определяем с допуском: у Baxter образцы одного ряда стоят с
    расхождением в доли пункта, и точное сравнение рассыпало бы ряд.
    """
    rows: list[list[dict]] = []
    for image in sorted(images, key=lambda i: -i["y"]):
        for row in rows:
            if abs(row[0]["y"] - image["y"]) <= 4:
                row.append(image)
                break
        else:
            rows.append([image])
    out: list[dict] = []
    for row in rows:
        out.extend(sorted(row, key=lambda i: i["x"]))
    return out


def swatches(raw: bytes, names: list[str]) -> dict[str, dict]:
    """Название отделки -> где в этом PDF лежит её образец.

    Возвращает {название: {"page": n, "xobject": "/Im3"}}. Чего не
    нашлось — в ответе нет вовсе: пустой квадратик хуже, чем его
    отсутствие.

    Порядок, а не координаты. Подписи внутри форм PDF отдаёт с чужой
    системой координат — у Baxter две трети приходили с нулями и
    налезали друг на друга. Зато и картинки, и текст идут в одном
    порядке чтения, и если на странице образцов ровно столько же,
    сколько подписей, соответствие однозначно. Не сошлось числом —
    страницу пропускаем: угадывать тут нечего.
    """
    from pypdf import PdfReader

    known = [n for n in names if _words(n)]
    if not known:
        return {}

    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception:            # noqa: BLE001
        return {}

    by_flat = {}
    for name in known:
        by_flat.setdefault(_flat(name), name)

    found: dict[str, dict] = {}
    for number, page in enumerate(reader.pages):
        images = _reading_order([im for im in placements(page, reader)
                                 if SWATCH_MIN <= im["w"] <= SWATCH_MAX
                                 and SWATCH_MIN <= im["h"] <= SWATCH_MAX])
        if not images:
            continue
        try:
            lines = [ln.strip() for ln in (page.extract_text() or "").split("\n")]
        except Exception:        # noqa: BLE001
            continue
        # Подписи — это строки, которые И ЕСТЬ названия отделок. Так с
        # листа уходит всё постороннее: у Baxter внизу стоит дата
        # печати, и без отсева число строк с числом образцов не сходится.
        captions = [by_flat[_flat(ln)] for ln in lines if _flat(ln) in by_flat]
        if len(captions) != len(images):
            continue
        for name, image in zip(captions, images):
            found.setdefault(name, {"page": number, "xobject": image["name"]})
    return found


def image_bytes(raw: bytes, page: int, xobject: str) -> tuple[bytes, str] | None:
    """Одна картинка из PDF: содержимое и тип. None — такой нет."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(raw))
        images = reader.pages[page].images
    except Exception:            # noqa: BLE001
        return None

    stem = xobject.lstrip("/")
    for image in images:
        if image.name.rsplit(".", 1)[0] == stem:
            kind = image.name.rsplit(".", 1)[-1].lower()
            mime = "image/png" if kind == "png" else "image/jpeg"
            return image.data, mime
    return None


# Один и тот же техлист нужен и разбору, и потом каждому образцу на
# экране. Держим последние два: страница карточки просит сто с лишним
# картинок подряд, и качать под каждую по семь мегабайт нельзя.
_OPEN: dict[str, tuple[bytes, object]] = {}
KEEP = 2


def opened(url: str, fetch) -> tuple[bytes, object]:
    """(содержимое, читатель) техлиста. `fetch` качает, если ещё не качали."""
    from pypdf import PdfReader

    if url not in _OPEN:
        raw = fetch(url)
        _OPEN[url] = (raw, PdfReader(io.BytesIO(raw)))
        while len(_OPEN) > KEEP:
            _OPEN.pop(next(iter(_OPEN)))
    return _OPEN[url]


def image_from(reader, page: int, xobject: str) -> tuple[bytes, str] | None:
    """Картинка из уже открытого документа."""
    try:
        images = reader.pages[page].images
    except Exception:            # noqa: BLE001
        return None
    stem = xobject.lstrip("/")
    for image in images:
        if image.name.rsplit(".", 1)[0] == stem:
            kind = image.name.rsplit(".", 1)[-1].lower()
            return image.data, ("image/png" if kind == "png" else "image/jpeg")
    return None
