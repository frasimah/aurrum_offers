# -*- coding: utf-8 -*-
"""
Фотографии изделия по разметке страницы.

Вторая ступень отбора. Первая — штатная карточка магазина (`shopify`),
она точнее всего; третья — запасная эвристика по имени файла.

Почему не эвристика: страница отдаёт всю галерею раздела. У PORADA это
72 снимка при 21 нужном, у EMMEMOBILI каталог соседних изделий, а у
VENICEM рядом лежат другие светильники той же серии — по именам файлов
они неотличимы, а структурно лежат снаружи галереи.

Правила смотрятся глазами один раз и живут в `config/photo_selectors.json`
как данные. Ключ — домен второго уровня, чтобы `www` и языковые
поддомены не приходилось перечислять.
"""

from __future__ import annotations

import json
import os
import re
from urllib.parse import urljoin, urlparse

CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "config", "photo_selectors.json")

# Пустышки-распорки: у BAROVIER такой лежит первым в галерее.
_SPACERS = ("spacer", "blank", "dummy", "1x1", "pixel")


def _rules() -> dict:
    with open(CONFIG, encoding="utf-8") as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _domain(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def rule_for(url: str) -> dict | None:
    """Правило для этого сайта или None, если бренда в конфиге нет."""
    return _rules().get(_domain(url))


def photos(html: str, page_url: str) -> list[str] | None:
    """Фотографии изделия по селекторам бренда.

    None — правила для сайта нет, работает следующая ступень.
    Пустой список — правило есть, но ничего не нашло: скорее всего сайт
    переверстали, и вызывающий обязан сказать об этом вслух.
    """
    rule = rule_for(page_url)
    if not rule:
        return None
    if not html:
        return []

    try:
        from bs4 import BeautifulSoup
    except ImportError:          # pragma: no cover — пакет в requirements
        return None

    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for selector in rule.get("selectors") or []:
        for img in soup.select(selector):
            src = (img.get("src") or img.get("data-src")
                   or img.get("data-lazy") or "").strip()
            if not src or src.startswith("data:"):
                continue
            name = src.split("?")[0].rsplit("/", 1)[-1].lower()
            if any(word in name for word in _SPACERS):
                continue
            if name.endswith(".svg"):        # схемы и значки
                continue
            full = urljoin(page_url, src)
            if full in seen:
                continue
            seen.add(full)
            out.append(full)
    return out
