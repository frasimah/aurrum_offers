# -*- coding: utf-8 -*-
"""
Палитра отделок со страницы бренда: группы и образцы.

Бренды печатают отделки не списком, а витриной: заголовок группы
(«BASE», «STRUTTURA», «TOP») и под ним кружки-образцы с подписями.
Модель отдаёт из этого плоский список названий — группа и образец
теряются, а менеджеру показывать клиенту нечего.

Читаем сами, разметкой. Названия перечислимы, адрес снимка выдумать
нельзя — значит это работа Python, а не модели.

Правила смотрятся глазами один раз и живут в
`config/finish_palette.json` как данные, тем же порядком, что и
селекторы фотографий в `gallery.py`.
"""

from __future__ import annotations

import json
import os
from urllib.parse import urljoin, urlparse

CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "config", "finish_palette.json")

# Заголовок группы — это часть предмета или семейство материала, как их
# называет сам бренд. Наши роли короче и по-русски; сопоставление
# перечислимо, поэтому лежит здесь, а не в промпте.
#
# Чего в таблице нет, получает «Отделка» — молча. Ругаться на каждый
# незнакомый заголовок значит ругаться восемнадцать раз на одном
# товаре, а тревога, которая звучит всегда, не тревога.
ROLE_BY_GROUP = {
    "struttura": "Каркас", "structure": "Каркас", "frame": "Каркас",
    "base": "Основание", "basamento": "Основание",
    "top": "Столешница", "piano": "Столешница",
    "piano top": "Столешница", "tavolo": "Столешница",
    "gambe": "Ножки", "legs": "Ножки",
    "specchio": "Стекло", "mirror": "Стекло", "vetro": "Стекло",
    "glass": "Стекло",
    "cuscino": "Обивка", "cuscini": "Обивка", "seduta": "Обивка",
    "schienale": "Обивка", "imbottitura": "Обивка", "rivestimento": "Обивка",
    "upholstery": "Обивка", "tessuto": "Обивка", "fabric": "Обивка",
    "pelle": "Обивка", "cuoio": "Обивка", "leather": "Обивка",
    "essenze": "Дерево", "legno": "Дерево", "wood": "Дерево",
    "marmo": "Камень", "marble": "Камень", "pietra": "Камень",
    "stone": "Камень",
    "metallo": "Металл", "metal": "Металл",
    "ripiani": "Полки", "shelves": "Полки",
    "ante": "Фасады", "fronts": "Фасады",
}


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


def role_of(group: str) -> str:
    """Заголовок группы -> наша роль. Незнакомый — «Отделка»."""
    key = (group or "").strip().lower()
    if key in ROLE_BY_GROUP:
        return ROLE_BY_GROUP[key]
    # Заголовки бывают двуязычными и с уточнением: «Essenze | Wood»,
    # «Laccato opaco poro chiuso». Берём первое знакомое слово.
    for word in key.replace("|", " ").replace("/", " ").split():
        if word in ROLE_BY_GROUP:
            return ROLE_BY_GROUP[word]
    return "Отделка"


def from_html(html: str, page_url: str) -> list[dict] | None:
    """Отделки витриной: группа, название, образец.

    None — правила для сайта нет, работает разбор моделью.
    Пустой список — правило есть, но ничего не нашло: сайт переверстали,
    и вызывающий обязан сказать об этом вслух.
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
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for block in soup.select(rule["block"]):
        head = block.select_one(rule["title"])
        group = head.get_text(" ", strip=True) if head else ""
        for item in block.select(rule["item"]):
            name_node = item.select_one(rule["name"])
            name = name_node.get_text(" ", strip=True) if name_node else ""
            if not name:
                continue
            # Повтор — не редкость: страница несёт витрину дважды, для
            # широкого экрана и для узкого. Разметка разная, отделки те же.
            key = (group.casefold(), name.casefold())
            if key in seen:
                continue
            seen.add(key)
            img = item.select_one("img")
            src = (img.get("src") or img.get("data-src") or "").strip() if img else ""
            out.append({
                "role_ru": role_of(group),
                "group": group,
                "material": name,
                "code": None,
                "swatch": urljoin(page_url, src) if src else "",
                # Источник помечаем: сверять с текстом страницы такие
                # незачем — они и есть текст страницы, слово в слово.
                "source": "палитра",
            })
    return out
