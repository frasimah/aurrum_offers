# -*- coding: utf-8 -*-
"""
Библиотека товаров: хранилище карточек между сессиями и людьми.

Первое место в приложении, где данные переживают браузер. Проект и
ставки живут в localStorage — они у каждого свои и меняются каждый
день; собранная карточка товара, наоборот, одна на всех и стоит
дорого: запрос Firecrawl, разбор Gemini, ручная выверка отделок.
Второй раз платить за неё незачем.

Почему Vercel Blob, а не база: на serverless диска нет, а ставить
Postgres ради нескольких сотен карточек — это отдельная инфраструктура
с миграциями и паролями. Blob — это HTTP-хранилище файлов, у которого
есть ровно то, что нужно: положить, прочитать, перечислить, удалить.

Устройство: одна карточка — один JSON по адресу `items/<id>.json`.
Отдельного индекса нет намеренно — он немедленно разошёлся бы с
файлами; список собирается перечислением, а поиск идёт по нему же.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

# Адрес API и его версия сняты с официального клиента @vercel/blob 2.8.0:
# документации на HTTP-протокол нет, и угадывать здесь дороже, чем
# прочитать. Три вещи, на которых спотыкается наивная реализация:
# путь блоба идёт параметром `pathname`, а не в адресе; приватному
# хранилищу обязателен заголовок доступа; версия API — 12.
API = "https://vercel.com/api/blob"
API_VERSION = "12"
ACCESS = "private"
PREFIX = "items/"
TIMEOUT = 30
# Больше тысячи карточек — уже повод для настоящей базы, а не для
# перечисления на каждый запрос. Предел стоит, чтобы это заметить.
MAX_ITEMS = 1000


class NotConfigured(RuntimeError):
    """Хранилище не подключено — говорим об этом прямо, а не молчим."""


def _token() -> str:
    token = os.environ.get("BLOB_READ_WRITE_TOKEN", "").strip()
    if not token:
        raise NotConfigured(
            "Библиотека не подключена: нет BLOB_READ_WRITE_TOKEN. "
            "Хранилище создаётся командой «vercel blob create-store»."
        )
    return token


def _headers() -> dict:
    return {"authorization": f"Bearer {_token()}",
            "x-api-version": API_VERSION}


def slug(brand: str, model: str) -> str:
    """Опознаватель карточки: бренд и модель латиницей.

    Кириллицу транслитерируем, а не выбрасываем: без этого «Кресло»
    и «Диван» дали бы одинаковый пустой хвост и затёрли друг друга.
    """
    table = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
        "ы": "y", "э": "e", "ю": "yu", "я": "ya", "ь": "", "ъ": "",
    }
    text = f"{brand} {model}".strip().lower()
    text = "".join(table.get(ch, ch) for ch in text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "item"


def save(item: dict) -> dict:
    """Кладём карточку. Тот же бренд и модель — та же запись, не копия."""
    brand = str(item.get("brand") or "").strip()
    model = str(item.get("model") or "").strip()
    if not (brand or model):
        raise ValueError("У карточки нет ни производителя, ни модели.")

    item = {**item}
    item["id"] = item.get("id") or slug(brand, model)
    item["saved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    got = httpx.put(
        f"{API}/",
        params={"pathname": f"{PREFIX}{item['id']}.json"},
        headers={**_headers(),
                 "content-type": "application/json",
                 "x-vercel-blob-access": ACCESS,
                 # Иначе Blob дописывает к имени случайный хвост, и
                 # повторное сохранение плодит копии вместо замены.
                 "x-add-random-suffix": "0",
                 # Пересохранение карточки — обычное дело: менеджер
                 # поправил отделки и положил обратно. Без этого Blob
                 # отвечает «blob already exists».
                 "x-allow-overwrite": "1",
                 "x-cache-control-max-age": "0"},
        content=json.dumps(item, ensure_ascii=False).encode("utf-8"),
        timeout=TIMEOUT,
    )
    got.raise_for_status()
    return item


def _list_blobs() -> list[dict]:
    out: list[dict] = []
    cursor = None
    while True:
        params = {"prefix": PREFIX, "limit": "1000"}
        if cursor:
            params["cursor"] = cursor
        got = httpx.get(f"{API}/", headers=_headers(), params=params, timeout=TIMEOUT)
        got.raise_for_status()
        data = got.json()
        out += data.get("blobs") or []
        cursor = data.get("cursor")
        if not cursor or len(out) >= MAX_ITEMS:
            return out[:MAX_ITEMS]


def _read(url: str, stamp: str = "") -> dict | None:
    """Читаем карточку. `stamp` — метка версии, она же сбиватель кэша.

    Адрес карточки постоянный (иначе правка плодила бы копии), поэтому
    CDN отдаёт по нему прежнее содержимое: поправленная карточка
    показывалась старой, и это невозможно было объяснить глазами.
    Метка загрузки из списка меняется при каждой записи и заставляет
    отдать свежее.
    """
    if stamp:
        url += ("&" if "?" in url else "?") + "v=" + quote(str(stamp), safe="")
    try:
        got = httpx.get(url, headers={**_headers(), "cache-control": "no-cache"},
                        timeout=TIMEOUT, follow_redirects=True)
        got.raise_for_status()
        data = got.json()
        return data if isinstance(data, dict) else None
    except Exception:      # noqa: BLE001 — битая карточка не роняет список
        return None


def all_items() -> list[dict]:
    """Все карточки, новые сверху."""
    # Чужие файлы в том же префиксе пропускаем: карточка без опознавателя
    # не карточка, а список не должен падать из-за постороннего блоба.
    items = [it for it in (_read(b["url"], b.get("uploadedAt", ""))
                          for b in _list_blobs())
             if it and it.get("id")]
    items.sort(key=lambda it: str(it.get("saved_at") or ""), reverse=True)
    return items


def get(item_id: str) -> dict | None:
    for blob in _list_blobs():
        if blob.get("pathname") == f"{PREFIX}{item_id}.json":
            return _read(blob["url"], blob.get("uploadedAt", ""))
    return None


def delete(item_id: str) -> bool:
    for blob in _list_blobs():
        if blob.get("pathname") == f"{PREFIX}{item_id}.json":
            httpx.post(f"{API}/delete", headers={**_headers(),
                                                 "content-type": "application/json"},
                       content=json.dumps({"urls": [blob["url"]]}).encode(),
                       timeout=TIMEOUT).raise_for_status()
            return True
    return False


def search(items: list[dict], query: str) -> list[dict]:
    """Поиск по словам: все слова запроса должны найтись в карточке.

    Ищем по всем текстовым полям сразу — менеджер помнит товар то по
    бренду, то по отделке, то по обрывку описания, и заставлять его
    выбирать поле значит заставлять угадывать.
    """
    words = [w for w in re.split(r"\s+", (query or "").strip().lower()) if w]
    if not words:
        return items

    def haystack(it: dict) -> str:
        parts = [str(it.get(k) or "") for k in
                 ("brand", "model", "type_ru", "dims_raw", "description",
                  "note", "summary_ru", "source_url")]
        parts += [f"{f.get('role_ru', '')} {f.get('material', '')} {f.get('code') or ''}"
                  for f in (it.get("finishes") or []) if isinstance(f, dict)]
        return " ".join(parts).lower()

    return [it for it in items if all(w in haystack(it) for w in words)]
