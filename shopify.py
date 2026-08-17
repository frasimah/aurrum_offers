# -*- coding: utf-8 -*-
"""
Штатная карточка товара Shopify.

Магазины на Shopify отдают товар как JSON по тому же адресу с `.js`.
Это не разбор страницы и не догадка по именам файлов — это карточка
в том виде, в каком её ведёт сам магазин. Оттуда достоверно приходят
производитель, название, раздел каталога и **точный список фотографий
изделия**: ни чужих товаров, ни образцов материалов, ни интерьерных
сцен из соседних разделов.

Так работают семь брендов из двенадцати: шесть марок Luxury Living
Group и Fendi Casa.

Чего в этом JSON нет — исполнений с габаритами: у изделия там одна
позиция «Default Title», а размеры живут в описании и техлисте.
"""

from __future__ import annotations

import re

import safe_fetch

# Адрес товара: .../products/<handle>[?...][#...]
_PRODUCT_URL = re.compile(r"^(https?://[^/]+(?:/[^/]+)*?/products/[^/?#]+)", re.I)

TIMEOUT = 30


def product_url(url: str) -> str:
    """Адрес карточки без хвостов, если он вообще похож на товар Shopify."""
    match = _PRODUCT_URL.match((url or "").strip())
    return match.group(1) if match else ""


def fetch(url: str) -> dict | None:
    """Карточка товара или None, если магазин не на Shopify.

    Молча возвращаем None на любой сбой: это не основной путь, а удача,
    когда она есть. Не нашлось — работают обычные разбор и отбор.
    """
    base = product_url(url)
    if not base:
        return None
    try:
        got = safe_fetch.get(base + ".js", timeout=TIMEOUT,
                             headers={"User-Agent": "Mozilla/5.0"})
        data = got.json()
    except Exception:            # noqa: BLE001 — не Shopify либо магазин закрыт
        return None
    if not isinstance(data, dict) or "images" not in data:
        return None
    return data


def photos(data: dict) -> list[str]:
    """Фотографии изделия. Адреса у Shopify относительные к схеме."""
    out = []
    for src in data.get("images") or []:
        if not isinstance(src, str):
            continue
        if src.startswith("//"):
            src = "https:" + src
        if src.startswith("http"):
            out.append(src)
    return out
