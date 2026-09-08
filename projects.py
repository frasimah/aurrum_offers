# -*- coding: utf-8 -*-
"""
Проекты на сервере.

Проект — это то, что менеджер собирает часами: позиции, шапка компреда,
параметры итога. До сих пор он жил только в localStorage: чистка данных
сайта его стирала, второй менеджер его не видел, между устройствами он
не переносился. Теперь он лежит там же, где каталог, — в Vercel Blob.

Устройство повторяет каталог: `projects/<id>.json` — истина,
`projects/index.json` — её кэш для списка. Транспорт берём прямым
импортом из `library`: выносить его в общий модуль ради одного нового
потребителя незачем, а `_list_blobs` там завязан на свой префикс по
умолчанию — нейтральный дефолт заставил бы запасной путь каталога
прочитать и записи проектов.

Сервер ничего не перечитывает перед записью — правило то же, что у
каталога, и по той же причине: хранилище доходит с задержкой. Целое
состояние приносит клиент. Против затирания чужой работы стоит счётчик
`rev`: клиент возвращает тот номер, с которым открывал, и запись с
устаревшим номером отклоняется, а не ложится поверх.

Автосохранения нет намеренно. Каждая запись переписывает ОБЩИЙ индекс
целиком, а читается он с задержкой; десятки записей в минуту от двух
менеджеров съедали бы чужие строки — проект остался бы в файле, но
пропал из списка. Между нажатиями «Сохранить» работу держит черновик
в браузере, он пишется на каждую правку и не теряет ничего.
"""

from __future__ import annotations

from datetime import datetime, timezone

from library import (NotConfigured, _list_blobs, _put, _read,  # noqa: F401
                     _url)

PREFIX = "projects/"
INDEX = "projects/index.json"

# Строка списка: всё, что видно в перечне проектов и по чему ищут.
# Позиции, шапка и параметры итога остаются в файле записи.
INDEX_FIELDS = ("id", "title", "number", "contract", "buyer", "date",
                "count", "rooms", "sum", "rev", "saved_at")


class Conflict(RuntimeError):
    """Запись устарела: проект успели изменить или удалить."""

    def __init__(self, message: str, current: dict | None = None):
        super().__init__(message)
        self.current = current


def title(project: dict) -> str:
    """Имя для списка — из шапки, по которой менеджер его и узнаёт.

    Своё название сильнее собранного: проект часто заводят до того, как
    появились номер спецификации и покупатель, — «Владимир, гостиная»
    понятнее, чем «Без имени».
    """
    header = project.get("header") or {}
    own = str(header.get("name") or "").strip()
    if own:
        return own
    parts = []
    number = str(header.get("number") or "").strip()
    if number:
        parts.append(f"Спецификация № {number}")
    buyer = str(header.get("buyer") or "").strip()
    if buyer:
        parts.append(buyer)
    return " · ".join(parts) or "Без имени"


def brief(project: dict) -> dict:
    """Запись -> строка индекса."""
    header = project.get("header") or {}
    positions = project.get("positions") or []
    out = {
        "id": project.get("id"),
        "title": title(project),
        "number": str(header.get("number") or "").strip(),
        "contract": str(header.get("contract") or "").strip(),
        "buyer": str(header.get("buyer") or "").strip(),
        "date": str(header.get("date") or "").strip(),
        "count": len(positions),
        "rooms": len(project.get("rooms") or []),
        "rev": int(project.get("rev") or 0),
        "saved_at": project.get("saved_at"),
    }
    # Сумма считается той же цепочкой, что на экране: два места, где
    # считают деньги по-разному, рано или поздно разойдутся.
    try:
        import pricing
        out["sum"] = pricing.project(positions, rates=project.get("rates"),
                                     final=project.get("final"))["final"]["к_оплате"]
    except Exception:            # noqa: BLE001 — список важнее суммы в нём
        out["sum"] = None
    return {k: v for k, v in out.items() if k in INDEX_FIELDS}


def read_index() -> list[dict]:
    """Список проектов, свежий. Как и у каталога, мимо кэша: поверх него
    пишется следующая запись, и устаревший список стёр бы чужую строку."""
    import time
    for blob in _list_blobs(INDEX):
        if blob.get("pathname") == INDEX:
            data = _read(_url(INDEX), f"{time.time_ns()}")
            rows = (data or {}).get("items")
            return rows if isinstance(rows, list) else []
    return []


def _write_index(rows: list[dict]) -> None:
    rows = sorted(rows, key=lambda r: str(r.get("saved_at") or ""), reverse=True)
    _put(INDEX, {"items": rows,
                 "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})


def rebuild_index() -> int:
    """Пересобрать список из файлов проектов — они и есть истина.

    Нужен, когда строка пропала из списка: файл цел, а в перечне его нет.
    Сам список этого не лечит, поэтому кнопка есть в интерфейсе.
    """
    rows = [p for p in (_read(b["url"]) for b in _list_blobs(PREFIX))
            if p and p.get("id")]
    _write_index([brief(p) for p in rows])
    return len(rows)


def get(project_id: str) -> dict | None:
    """Проект по имени — прямым адресом, минуя список."""
    return _read(_url(f"{PREFIX}{project_id}.json"))


def save(project: dict) -> dict:
    """Записать проект, если присланный `rev` не устарел.

    Сверка живёт здесь, а не в маршруте: иначе любой другой вызов —
    приёмка, будущая пакетная запись — обошёл бы её молча, и мы вернулись
    бы к затиранию чужой работы.
    """
    project_id = str(project.get("id") or "").strip()
    if not project_id:
        raise ValueError("У проекта нет опознавателя.")

    rev = int(project.get("rev") or 0)
    rows = read_index()
    known = next((r for r in rows if r.get("id") == project_id), None)

    # Номер правки берём У ЗАПИСИ, а не у строки списка. Список — отдельный
    # файл, он пишется следом за записью и у хранилища с отложенной
    # согласованностью отстаёт: менеджер получал «у вас правка № 3, в
    # хранилище № 2» на СВОЁ ЖЕ сохранение и не мог записать проект вовсе.
    record = get(project_id)
    if record is not None:
        stored_rev = int(record.get("rev") or 0)
    elif known is not None:
        stored_rev = int(known.get("rev") or 0)
    else:
        stored_rev = None

    # Спорим только когда в хранилище НОВЕЕ нашего: это чужая правка, и
    # молча затирать её нельзя. Если там старее — это наша собственная
    # запись, ещё не разошедшаяся по хранилищу, и спорить не с чем.
    if stored_rev is not None and stored_rev > rev:
        raise Conflict(
            f"Проект успели изменить: у вас правка № {rev}, "
            f"в хранилище № {stored_rev}.", known or record)
    if stored_rev is None and rev > 0:
        raise Conflict("Проекта нет в хранилище: его удалили, "
                       "либо запись ещё не дошла.", None)

    saved = {**project, "rev": rev + 1,
             "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _put(f"{PREFIX}{project_id}.json", saved)
    _write_index([r for r in rows if r.get("id") != project_id] + [brief(saved)])
    return saved


def delete(project_id: str) -> bool:
    """Убрать проект. Адрес строим сами: в списке его может ещё не быть."""
    import json

    import httpx

    from library import API, _headers
    httpx.post(f"{API}/delete",
               headers={**_headers(), "content-type": "application/json"},
               content=json.dumps({"urls": [_url(f"{PREFIX}{project_id}.json")]}).encode(),
               timeout=30).raise_for_status()
    _write_index([r for r in read_index() if r.get("id") != project_id])
    return True


def search(rows: list[dict], query: str) -> list[dict]:
    """Поиск по всем полям строки сразу: менеджер помнит проект то по
    номеру, то по фамилии покупателя, то по дате."""
    words = [w for w in (query or "").lower().split() if w]
    if not words:
        return rows
    out = []
    for row in rows:
        blob = " ".join(str(v) for v in row.values() if v is not None).lower()
        if all(word in blob for word in words):
            out.append(row)
    return out
