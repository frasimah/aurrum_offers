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

Устройство: одна карточка — один JSON по адресу `items/<id>.json`,
плюс `index.json` — короткая выжимка всех карточек в одном файле.

Хранилище доходит с задержкой: чтение сразу после записи может вернуть
предыдущее содержимое. Отсюда правило — карточку после сохранения не
перечитываем, а показываем ту, что сохранили.

Индекса сначала не было намеренно: он способен разойтись с файлами.
Но каталог на сотни товаров читать по файлу на карточку нельзя — это
сотни запросов на открытие страницы. Поэтому истина по-прежнему в
файлах карточек, а индекс — их кэш: пишется вместе с карточкой и
пересобирается из файлов по требованию (`rebuild_index`), если
разошёлся. Список и поиск идут по индексу, полная карточка — по файлу.
"""

from __future__ import annotations

import json
import os
import re
import time
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
INDEX = "index.json"
TIMEOUT = 30
# Поля выжимки: всё, по чему каталог ищет и что показывает списком.
# Полное описание и фотографии сверх первой остаются в файле карточки.
INDEX_FIELDS = ("id", "brand", "model", "type_ru", "dims_raw", "volume_m3",
                "source_url", "summary_ru", "saved_at", "collection")
# Предел на перечисление файлов. Читает их только пересборка
# индекса; каталог обходится одним запросом за индексом.
MAX_ITEMS = 5000


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


def _put(pathname: str, payload) -> None:
    got = httpx.put(
        f"{API}/", params={"pathname": pathname},
        headers={**_headers(),
                 "content-type": "application/json",
                 "x-vercel-blob-access": ACCESS,
                 # Иначе Blob дописывает к имени случайный хвост, и
                 # повторное сохранение плодит копии вместо замены.
                 "x-add-random-suffix": "0",
                 # Пересохранение карточки — обычное дело: менеджер
                 # поправил отделки и положил обратно.
                 "x-allow-overwrite": "1",
                 "x-cache-control-max-age": "0"},
        content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=TIMEOUT,
    )
    got.raise_for_status()


def brief(item: dict) -> dict:
    """Карточка -> строка индекса: то, что видно списком и по чему ищут."""
    out = {k: item.get(k) for k in INDEX_FIELDS if item.get(k) not in (None, "")}
    photos = item.get("photos") or []
    if photos:
        out["photo"] = photos[0]
    # Отделки в выжимке нужны: по ним ищут не реже, чем по названию.
    out["finishes"] = [{"role_ru": f.get("role_ru"), "material": f.get("material"),
                        "code": f.get("code")}
                       for f in (item.get("finishes") or []) if isinstance(f, dict)]
    return out


def _index_blob() -> dict | None:
    for blob in _list_blobs(INDEX):
        if blob.get("pathname") == INDEX:
            return blob
    return None


def read_index() -> list[dict]:
    """Индекс читаем всегда свежим, без оглядки на кэш.

    Это то, что видно в каталоге, и то, поверх чего пишется следующая
    правка: устаревший индекс не просто показал бы старое, а затёр бы
    чужую только что сохранённую карточку. Один лишний запрос против
    такой цены — дёшево.
    """
    if not _index_blob():
        return []
    data = _read(_url(INDEX))
    items = (data or {}).get("items") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


def _write_index(items: list[dict]) -> None:
    items = sorted(items, key=lambda it: str(it.get("saved_at") or ""), reverse=True)
    _put(INDEX, {"items": items,
                 "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})


def rebuild_index() -> int:
    """Пересобрать индекс из файлов карточек — они и есть истина."""
    items = [it for it in (_read(b["url"])
                           for b in _list_blobs(PREFIX))
             if it and it.get("id")]
    _write_index([brief(it) for it in items])
    return len(items)


def save(item: dict, reindex: bool = True) -> dict:
    """Кладём карточку. Тот же бренд и модель — та же запись, не копия.

    `reindex=False` — для пакетной загрузки: индекс переписывается один
    раз в конце, а не после каждой из сотен карточек.
    """
    brand = str(item.get("brand") or "").strip()
    model = str(item.get("model") or "").strip()
    if not (brand or model):
        raise ValueError("У карточки нет ни производителя, ни модели.")

    item = {**item}
    item["id"] = item.get("id") or slug(brand, model)
    item["saved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    _put(f"{PREFIX}{item['id']}.json", item)

    if reindex:
        rows = [it for it in read_index() if it.get("id") != item["id"]]
        rows.append(brief(item))
        _write_index(rows)
    return item


def _list_blobs(prefix: str = PREFIX) -> list[dict]:
    out: list[dict] = []
    cursor = None
    while True:
        params = {"prefix": prefix, "limit": "1000"}
        if cursor:
            params["cursor"] = cursor
        got = httpx.get(f"{API}/", headers=_headers(), params=params, timeout=TIMEOUT)
        got.raise_for_status()
        data = got.json()
        out += data.get("blobs") or []
        cursor = data.get("cursor")
        if not cursor or len(out) >= MAX_ITEMS:
            return out[:MAX_ITEMS]


_HOST: str | None = None


def _host() -> str:
    """Адрес хранилища — постоянный, узнаётся один раз из любого блоба.

    Нужен, чтобы обращаться к карточке по имени, не спрашивая список:
    список отстаёт от записи, и только что созданной карточки в нём
    может не быть.
    """
    global _HOST
    if _HOST:
        return _HOST
    for blob in _list_blobs(""):
        url = blob.get("url") or ""
        if "://" in url:
            _HOST = url.split("/", 3)[0] + "//" + url.split("/", 3)[2]
            return _HOST
    raise NotConfigured("Хранилище пустое — адрес карточки не из чего собрать.")


def _url(pathname: str) -> str:
    return f"{_host()}/{pathname}"


def _read(url: str, stamp: str = "") -> dict | None:
    """Читаем карточку. `stamp` — метка версии, она же сбиватель кэша.

    Адрес карточки постоянный (иначе правка плодила бы копии), поэтому
    CDN отдаёт по нему прежнее содержимое: поправленная карточка
    показывалась старой, и объяснить это глазами было невозможно.

    Меткой служит время запроса, а не версия из списка. Пробовал по
    очереди `uploadedAt` и `etag` — обе врут: список отстаёт от самих
    файлов (видел uploadedAt 11:26:24 при last-modified 11:27:53), метка
    повторялась, и CDN честно отдавал прежнее с пометкой HIT. Заголовок
    no-cache он игнорирует, так что разный адрес — единственное, что
    помогает против кэша.

    Против чего он не помогает: само хранилище доходит с задержкой.
    Проверено пятью записями подряд — чтение сразу после записи иногда
    возвращает предыдущее содержимое даже по заведомо новому адресу.
    Поэтому список и поиск идут по индексу (его пишем мы и рядом), а
    файл карточки читается при открытии — через секунды после правки,
    когда запись уже дошла.
    """
    url += ("&" if "?" in url else "?") + "v=" + quote(stamp or str(time.time_ns()), safe="")
    try:
        got = httpx.get(url, headers={**_headers(), "cache-control": "no-cache"},
                        timeout=TIMEOUT, follow_redirects=True)
        got.raise_for_status()
        data = got.json()
        return data if isinstance(data, dict) else None
    except Exception:      # noqa: BLE001 — битая карточка не роняет список
        return None


def all_items() -> list[dict]:
    """Выжимки всех карточек, новые сверху — одним запросом за индексом."""
    items = read_index()
    if items:
        return items
    # Индекса нет (первый запуск или его снесли) — собираем из файлов.
    return [brief(it) for it in
            sorted((it for it in (_read(b["url"])
                                  for b in _list_blobs())
                    if it and it.get("id")),
                   key=lambda it: str(it.get("saved_at") or ""), reverse=True)]


def get(item_id: str) -> dict | None:
    """Карточка по имени — прямым адресом, без обращения к списку."""
    return _read(_url(f"{PREFIX}{item_id}.json"))


def delete(item_id: str) -> bool:
    """Убрать карточку. Адрес строим сами: в списке её может ещё не быть."""
    got = httpx.post(f"{API}/delete",
                     headers={**_headers(), "content-type": "application/json"},
                     content=json.dumps({"urls": [_url(f"{PREFIX}{item_id}.json")]}).encode(),
                     timeout=TIMEOUT)
    got.raise_for_status()
    _write_index([it for it in read_index() if it.get("id") != item_id])
    return True


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
