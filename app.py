# -*- coding: utf-8 -*-
"""
AURRUM — конвертер спецификаций: Excel на входе, готовый документ для печати
(HTML + печать в PDF средствами браузера) на выходе.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import time
from collections import defaultdict
from datetime import timedelta
from urllib.parse import quote

import httpx
from markupsafe import Markup, escape

from dotenv import load_dotenv
from flask import (Flask, Response, redirect, render_template, request,
                   session, url_for)

# Ключи и пароли живут в .env (в git не попадает; образец — .env.example).
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import book_export  # noqa: E402
import library  # noqa: E402
import book_row  # noqa: E402
import doc_parser  # noqa: E402
import extract_agent  # noqa: E402
import pricing  # noqa: E402
import product_lookup  # noqa: E402
import projects  # noqa: E402 — после load_dotenv, читают переменные окружения
import safe_fetch  # noqa: E402
import spec_parser  # noqa: E402

ALLOWED = {".xlsx", ".xlsm"}
MAX_MB = 25

# Общий пароль на вход — только из окружения.
#
# Дефолта нет намеренно: пока он был («123»), приложение работало и без
# настройки переменных, а значит на проде стоял известный пароль, за
# которым лежат ключи API. Без переменной вход закрыт совсем — это
# заметно сразу, в отличие от тихо открытой двери.
PASSWORD = os.environ.get("AURRUM_PASSWORD", "").strip()

# Фиксированный ключ подписи был не менее опасен: зная его, сессию можно
# подделать и пароль не понадобится вовсе. Если переменной нет, берём
# случайный — вход всё равно закрыт отсутствием пароля.
SECRET_KEY = os.environ.get("AURRUM_SECRET_KEY", "").strip() or secrets.token_hex(32)

MISSING_ENV = [name for name, value in
               (("AURRUM_PASSWORD", PASSWORD), ("AURRUM_SECRET_KEY",
                os.environ.get("AURRUM_SECRET_KEY", "").strip())) if not value]

# Защита от перебора: N неудачных попыток с одного адреса — пауза.
MAX_FAILS = 5
LOCK_SECONDS = 300
_fails: dict[str, list[float]] = defaultdict(list)

# Роуты, доступные без пароля
PUBLIC_ENDPOINTS = {"login", "static"}

# Карточек на странице каталога: сетка 4 в ряд, шесть рядов.
PER_PAGE = 24

app = Flask(__name__)
app.config.update(
    MAX_CONTENT_LENGTH=MAX_MB * 1024 * 1024,
    SECRET_KEY=SECRET_KEY,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("AURRUM_HTTPS")),
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)


# --- Доступ по паролю ---------------------------------------------------

def _client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "?"


def _locked(ip: str) -> bool:
    now = time.time()
    _fails[ip] = [t for t in _fails[ip] if now - t < LOCK_SECONDS]
    return len(_fails[ip]) >= MAX_FAILS


@app.before_request
def require_password():
    if request.endpoint in PUBLIC_ENDPOINTS or session.get("authorized"):
        return None
    # Запросу за данными отвечаем отказом, а не страницей входа: сессия
    # живёт 7 дней, а проект правят часами, и fetch молча шёл за редиректом,
    # получал HTML и выдавал «Unexpected token '<'» вместо «перезайдите».
    # Условие узкое, поэтому печать и загрузка книги по-прежнему уходят
    # на страницу входа, как и положено переходу по ссылке.
    if request.is_json:
        return {"error": "Сессия истекла — перезайдите в соседней вкладке "
                         "и повторите."}, 401
    return redirect(url_for("login", next=request.full_path.rstrip("?")))


@app.route("/login", methods=["GET", "POST"])
def login():
    if MISSING_ENV:
        return render_template("login.html", error=(
            "Приложение не настроено: нет " + ", ".join(MISSING_ENV)
            + ". Задайте переменные в Project Settings → Environment Variables "
              "(локально — в .env)."
        )), 503

    ip = _client_ip()
    if _locked(ip):
        return render_template(
            "login.html", error="Слишком много попыток. Повторите через 5 минут."
        ), 429

    if request.method == "POST":
        # Сравниваем байтами: со строками compare_digest требует чистого
        # ASCII и падает на любом другом символе. Достаточно набрать пароль
        # с русской раскладкой — и вместо «неверный пароль» приходит 500.
        # Поймано на проде: локально я вводил только латиницу.
        entered = request.form.get("password", "").encode("utf-8")
        if hmac.compare_digest(entered, PASSWORD.encode("utf-8")):
            session.clear()
            session.permanent = True
            session["authorized"] = True
            _fails.pop(ip, None)
            nxt = request.args.get("next", "")
            # только внутренние адреса: "//host" браузер считает внешним
            if nxt.startswith("/") and not nxt.startswith("//"):
                return redirect(nxt)
            return redirect(url_for("index"))
        _fails[ip].append(time.time())
        return render_template("login.html", error="Неверный пароль."), 401

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --- Фильтры шаблонов ---------------------------------------------------

@app.template_filter("money")
def money(v) -> str:
    """43080 -> '43 080,00'. Нечисловое отдаём как есть."""
    if v is None or v == "":
        return ""
    try:
        return f"{float(v):,.2f}".replace(",", " ").replace(".", ",")
    except (TypeError, ValueError):
        return str(v)


@app.template_filter("nl2br")
def nl2br(v) -> Markup:
    return Markup("<br>".join(escape(str(v)).split("\n")))


# --- Роуты -------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", error=request.args.get("error"))


@app.route("/lookup", methods=["GET", "POST"])
def lookup():
    """Карточка позиции по ссылке на сайт бренда."""
    url = (request.form.get("url") or "").strip()
    if request.method == "GET" or not url:
        return render_template(
            "lookup.html", error="Вставьте ссылку на товар." if request.method == "POST" else None
        )

    if not url.startswith(("http://", "https://")):
        return render_template("lookup.html", url=url, error="Ссылка должна начинаться с http:// или https://"), 400

    try:
        product = product_lookup.lookup(url)
    except Exception as exc:  # noqa: BLE001 — причину показываем пользователю
        return render_template("lookup.html", url=url, error=str(exc)), 502

    description = product_lookup.to_excel_description(product, with_finishes=False)
    # Разбор стоит запроса Firecrawl и работы Gemini — терять его,
    # если менеджер закрыл вкладку, незачем. Кладём в каталог сразу,
    # ещё до того, как он что-то нажмёт.
    saved = _remember(product, description)

    return render_template(
        "lookup.html",
        url=url,
        product=product,
        description=description,
        saved_id=saved,
        variants=product_lookup.variant_cards(product),
        card_base=_card_base(product, description),
        project_choices=_project_choices(),
        types=product_lookup.TYPES_RU,
    )


def _remember(product, description: str) -> str | None:
    """Разобранную карточку — в каталог. Тихо: сбой хранилища не повод
    отнимать у менеджера уже собранную карточку на экране.

    Существующую не затираем: в ней могли быть правки руками, а свежий
    разбор их не знает. Обновление — по кнопке в редакторе карточки.
    """
    try:
        item_id = library.slug(product.brand, product.model)
        if library.get(item_id):
            return item_id
        return library.save({
            "source_url": product.source_url,
            "brand": product.brand, "model": product.model,
            "type_ru": product.type_ru, "collection": product.collection,
            "dims_raw": product.dims_raw, "dims_confident": product.dims_confident,
            "width_cm": product.width_cm, "depth_cm": product.depth_cm,
            "height_cm": product.height_cm, "volume_m3": product.volume_m3,
            "volume_source": product.volume_source,
            "finishes": product.finishes, "note": product.tech_note,
            "summary_ru": product.summary_ru, "description": description,
            "photos": product.photo_urls, "doc_urls": product.doc_urls,
        })["id"]
    except Exception:            # noqa: BLE001 — карточка на экране важнее
        return None


@app.route("/parse-doc", methods=["POST"])
def parse_doc():
    """Разбор техлиста — по кнопке у документа.

    Отдаём список исполнений, а не одно значение: у изделия бывает
    несколько версий, и выбрать должен менеджер. У каждой — свой артикул
    и признак вроде размера матраса, по которым выбор и делается.

    Разбирает `extract`: первым Gemini, он читает страницу вместе
    с чертежами; если не ответил — LlamaExtract по текстовому слою.
    Каким путём собрано, видно в предупреждениях карточки.

    Если не сработало и это, откатываемся на регулярку по markdown
    от LlamaParse: она находит только числа без контекста, но это лучше,
    чем пустая карточка.
    """
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not url.startswith(("http://", "https://")):
        return {"error": "Нужна ссылка на документ."}, 400
    try:
        # Проверяем здесь, иначе на внутренний адрес мы зря пойдём
        # в запасной разбор и ответим 502 вместо честной ошибки запроса.
        safe_fetch.assert_public(url)
    except safe_fetch.UnsafeUrl as exc:
        return {"error": str(exc)}, 400

    try:
        data = extract_agent.extract_pdf(url)
    except Exception as exc:  # noqa: BLE001 — причину показываем пользователю
        return _parse_doc_fallback(url, str(exc))

    candidates, finishes, warnings = extract_agent.to_candidates(data)
    return {"candidates": candidates, "finishes": finishes,
            "warnings": warnings, "source": "extract"}


def _parse_doc_fallback(url: str, reason: str):
    """Запасной разбор: markdown от LlamaParse + регулярка по габаритам."""
    try:
        markdown = doc_parser.parse_pdf(url)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{reason} Запасной разбор тоже не удался: {exc}"}, 502

    candidates = []
    for cand in doc_parser.find_dim_candidates(markdown):
        m = product_lookup.measure(cand["value"])
        candidates.append({
            **cand, "sku": None,
            "width_cm": m.width_cm, "depth_cm": m.depth_cm,
            "height_cm": m.height_cm,
            "volume_m3": m.volume_m3, "volume_source": m.volume_source,
            "dims_confident": m.confident, "warnings": m.warnings,
        })

    return {"candidates": candidates, "finishes": [], "source": "fallback",
            "warnings": [f"Извлечение не сработало ({reason}) — показаны "
                         f"размеры, найденные по тексту, без артикулов."]}


@app.route("/project")
def project():
    """Набранные позиции и расчёт по ним.

    Здесь всегда новый проект или черновик из браузера; сохранённый
    открывается по своему адресу (`/project/open/<id>`). Черновик держит
    работу между нажатиями «Сохранить» и переживает перезагрузку.
    """
    return render_template("project.html", project=None)


@app.route("/library")
def library_page():
    """Собранные карточки: искать, править, отправлять в проект.

    Первая страница с общими данными: карточка стоит запроса Firecrawl
    и разбора Gemini, и второй раз платить за неё незачем — ни деньгами,
    ни временем менеджера на выверку отделок.
    """
    query = (request.args.get("q") or "").strip()
    brand = (request.args.get("brand") or "").strip()
    type_ru = (request.args.get("type") or "").strip()
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1

    blank = {"items": [], "query": query, "brand": brand, "type_ru": type_ru,
             "brands": [], "types": [], "total": 0, "found": 0,
             "page": 1, "pages": 1}
    try:
        items = library.all_items()
    except library.NotConfigured as exc:
        return render_template("library.html", error=str(exc), **blank)
    except Exception as exc:  # noqa: BLE001 — причину показываем
        return render_template("library.html",
                               error=f"Библиотека не открылась: {exc}", **blank)

    # Списки для отбора считаем по всей библиотеке, а не по отфильтрованному:
    # иначе выбранный бренд выкидывает из списка все остальные, и вернуться
    # к ним можно только сбросом.
    brands = sorted({str(it.get("brand") or "").strip()
                     for it in items if it.get("brand")})
    types = sorted({str(it.get("type_ru") or "").strip()
                    for it in items if it.get("type_ru")})

    found = library.search(items, query)
    if brand:
        found = [it for it in found if it.get("brand") == brand]
    if type_ru:
        found = [it for it in found if it.get("type_ru") == type_ru]

    pages = max(1, (len(found) + PER_PAGE - 1) // PER_PAGE)
    page = min(page, pages)
    start = (page - 1) * PER_PAGE

    return render_template(
        "library.html", items=found[start:start + PER_PAGE],
        query=query, brand=brand, type_ru=type_ru,
        brands=brands, types=types,
        total=len(items), found=len(found), page=page, pages=pages, error=None)


def _card_base(product, description: str, item: dict | None = None) -> dict:
    """Полное состояние карточки для экрана.

    Экран показывает не всё, что в карточке лежит: опознаватель,
    коллекция, ссылки на документы, источник объёма, артикулы отделок
    полей на нём не имеют. А сохранение пишет карточку ЦЕЛИКОМ, без
    слияния (иначе задержка хранилища съедала бы только что сделанную
    правку). Значит экран обязан принести всё — иначе непоказанное
    стирается молча: правка одного примечания уносила ссылку на техлист
    вместе с кнопкой «Распарсить».

    Второе: без `id` сохранение считает его как slug(бренд, модель) —
    и правка производителя или модели клала ДУБЛЬ под новым именем,
    оставляя исходную карточку со старыми данными.
    """
    base = dict(item or {})
    base.update({
        "source_url": product.source_url, "brand": product.brand,
        "model": product.model, "type_ru": product.type_ru,
        "collection": product.collection, "dims_raw": product.dims_raw,
        "dims_confident": product.dims_confident,
        "width_cm": product.width_cm, "depth_cm": product.depth_cm,
        "height_cm": product.height_cm, "volume_m3": product.volume_m3,
        "volume_source": product.volume_source, "finishes": product.finishes,
        "note": product.tech_note, "summary_ru": product.summary_ru,
        "description": description, "photos": product.photo_urls,
        "doc_urls": product.doc_urls, "dims_from_spec": product.dims_from_spec,
    })
    return base


def _volume_source_of(item: dict) -> str:
    """Откуда объём, если в карточке это не записано.

    Карточки, сохранённые до того, как экран стал приносить всё
    состояние целиком, лежат без volume_source — и подпись под
    заполненным полем говорила «Не определён». Восстановить можно
    точно, без догадок: если объём совпадает с расчётом по осям, он
    расчётный; если стоит и не совпадает — его поставили руками.
    """
    volume = item.get("volume_m3")
    if not isinstance(volume, (int, float)) or not volume:
        return ""
    calc = product_lookup.volume_m3(item.get("width_cm"), item.get("depth_cm"),
                                    item.get("height_cm"))
    if calc is not None and abs(calc - float(volume)) < 0.005:
        return "расчёт по габаритам"
    return "задан вручную"


def _as_product(item: dict):
    """Сохранённая карточка -> Product: редактор у каталога и разбора один.

    Правят они одно и то же, поэтому и экран должен быть один. Пока их
    было два, они разошлись до того, что одно поле называлось `f_type`
    на разборе и `f_type_ru` в каталоге — на этом уже ломалось
    сохранение. Каталожный редактор был к тому же урезан: ни отделок с
    галочками, ни отбора фотографий, ни производителя с моделью.

    Исполнений в каталоге не хранится — их и раньше там не было.
    """
    return product_lookup.Product(
        source_url=item.get("source_url") or "",
        brand=item.get("brand") or "",
        model=item.get("model") or "",
        collection=item.get("collection") or "",
        type_ru=item.get("type_ru") or "",
        dims_raw=item.get("dims_raw") or "",
        width_cm=item.get("width_cm"),
        depth_cm=item.get("depth_cm"),
        height_cm=item.get("height_cm"),
        dims_confident=bool(item.get("dims_confident", True)),
        dims_from_spec=bool(item.get("dims_from_spec")),
        volume_m3=item.get("volume_m3"),
        volume_source=(item.get("volume_source")
                       or _volume_source_of(item)),
        finishes=item.get("finishes") or [],
        tech_note=item.get("note") or "",
        summary_ru=item.get("summary_ru") or "",
        photo_urls=item.get("photos") or [],
        doc_urls=item.get("doc_urls") or [],
    )


@app.route("/source-text")
def source_text():
    """Текст страницы бренда дословно — для сверки карточки с источником.

    Карточка говорит, что нашла, но проверить это можно было только уйдя
    на сайт и вычитав страницу глазами. Здесь тот же текст, по которому
    работало извлечение, — и в нём подсвечивается то, что попало в поля.
    Значение, которого в тексте нет, видно сразу: именно так выглядит
    выдумка.

    Ссылку проверяет safe_fetch: публичный адрес и на каждом редиректе.
    """
    url = (request.args.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return {"error": "Нужна ссылка на страницу товара."}, 400
    try:
        text, _links, _html = product_lookup._scrape(url)
    except product_lookup.NoDelivery as exc:
        return {"error": str(exc)}, 502
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Страница не открылась: {exc}"}, 502
    # Потолок на ответ: страницы брендов заметно короче, а огромный текст
    # только повесит вкладку.
    return {"text": text[:200_000], "chars": len(text)}


@app.route("/library/item/<item_id>")
def library_item(item_id: str):
    """Полная карточка: индекс её не хранит, лежит она в своём файле."""
    if not re.fullmatch(r"[a-z0-9-]{1,120}", item_id):
        return render_template("library.html", error="Неверный адрес карточки.",
                               items=[], query="", brand="", type_ru="",
                               brands=[], types=[], total=0, found=0,
                               page=1, pages=1), 400
    try:
        item = library.get(item_id)
    except Exception as exc:  # noqa: BLE001
        return render_template("library.html", error=f"Карточка не открылась: {exc}",
                               items=[], query="", brand="", type_ru="",
                               brands=[], types=[], total=0, found=0,
                               page=1, pages=1), 502
    if not item:
        return redirect(url_for("library_page"))
    # Описание берём сохранённое, а не собранное заново: в нём могли
    # быть правки руками, и пересборка их бы стёрла.
    product = _as_product(item)
    return render_template(
        "lookup.html", product=product, url=product.source_url,
        description=item.get("description") or "",
        variants=product_lookup.variant_cards(product),
        card_base=_card_base(product, item.get("description") or "", item),
        project_choices=_project_choices(),
        types=product_lookup.TYPES_RU, from_library=item_id)


@app.route("/library/pick.json")
def library_pick():
    """Каталог в JSON — для выбора позиции на странице проекта.

    Порядок работы такой: сперва собирается библиотека, потом из неё
    формируется проект. Ссылку на сайт бренда разбирают один раз, и
    карточка живёт в каталоге; в проект она попадает уже оттуда.
    """
    query = (request.args.get("q") or "").strip()
    try:
        rows = library.all_items()
    except library.NotConfigured as exc:
        return {"error": str(exc), "items": []}, 200
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Каталог не открылся: {exc}", "items": []}, 200
    if query:
        rows = library.search(rows, query)
    return {"items": rows[:200], "total": len(rows)}


@app.route("/library/card/<item_id>")
def library_card(item_id: str):
    """Полная карточка в JSON — для кнопки «В проект» из каталога.

    В индексе её нет: он держит выжимку, чтобы каталог открывался одним
    запросом. За описанием, всеми фотографиями и отделками идём в файл.
    """
    if not re.fullmatch(r"[a-z0-9-]{1,120}", item_id):
        return {"error": "Неверный адрес карточки."}, 400
    try:
        item = library.get(item_id)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Карточка не открылась: {exc}"}, 502
    return item or ({"error": "Карточки нет в библиотеке."}, 404)


# Поля, которые редактор карточки показывает и правит. Всё остальное
# в карточке (фотографии, документы, источник объёма) он передаёт как
# есть — и поэтому сервер ничего не перечитывает перед записью.
EDITABLE = ("type_ru", "dims_raw", "width_cm", "depth_cm", "height_cm",
            "volume_m3", "note", "description")

# Выводы разбора, а не правки руками. Их нельзя ни беречь как чужой
# труд, ни показывать как «предложение»: понижение доверия — это и есть
# тот сигнал, ради которого пересборку затевают. Карточка, сохранённая
# до правки единицы измерения, лежит с dims_confident=True и объёмом
# 7586 м³; пересборка возвращала False, но флаг не был ни в EDITABLE,
# ни среди пустых полей — и «!» не появлялся никогда.
#
# Только вниз: разбор вправе усомниться в сохранённом, но не вправе
# снять сомнение, которое там уже стоит. Числа менеджер мог поправить
# руками, и повышать к ним доверие за него мы не будем.
DOWNGRADE_ONLY = ("dims_confident",)


def _incoming(data: dict) -> tuple[dict | None, str | None]:
    """Карточка из запроса редактора: проверенная и с прежним именем.

    Читать сохранённую и сливать с ней нельзя: хранилище доходит с
    задержкой, чтение сразу после записи возвращает предыдущее — на
    этом правка «не терять» была молча съедена пересборкой. Поэтому
    целое состояние карточки приносит тот, у кого оно на экране.
    """
    item = data.get("item")
    if not isinstance(item, dict):
        return None, "Неверный запрос."
    item_id = str(item.get("id") or "")
    if not re.fullmatch(r"[a-z0-9-]{1,120}", item_id):
        return None, "Неверный адрес карточки."
    if not (str(item.get("brand") or "").strip() and str(item.get("model") or "").strip()):
        return None, "У карточки должны быть производитель и модель."
    return item, None


@app.route("/library/update", methods=["POST"])
def library_update():
    """Правки из редактора — записью целиком, без чтения и слияния."""
    item, error = _incoming(request.get_json(silent=True) or {})
    if error:
        return {"error": error}, 400
    try:
        library.save(item)
    except library.NotConfigured as exc:
        return {"error": str(exc)}, 400
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Не удалось сохранить: {exc}"}, 502
    return {"saved": item["id"]}


@app.route("/library/refresh", methods=["POST"])
def library_refresh():
    """Разобрать страницу бренда заново — не трогая заполненного.

    Свежий разбор ложится ПОД карточку с экрана: пустое заполняет,
    заполненное оставляет. Что разошлось — возвращаем отдельно, чтобы
    менеджер сам решил, а не узнавал об этом по изменившимся числам.
    """
    item, error = _incoming(request.get_json(silent=True) or {})
    if error:
        return {"error": error}, 400

    url = str(item.get("source_url") or "")
    if not url.startswith(("http://", "https://")):
        return {"error": "У карточки нет ссылки на источник — пересобрать не с чего."}, 400

    try:
        product = product_lookup.lookup(url)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Не удалось разобрать страницу: {exc}"}, 502

    fresh = {
        "type_ru": product.type_ru, "collection": product.collection,
        "dims_raw": product.dims_raw, "dims_confident": product.dims_confident,
        "width_cm": product.width_cm, "depth_cm": product.depth_cm,
        "height_cm": product.height_cm, "volume_m3": product.volume_m3,
        "volume_source": product.volume_source, "finishes": product.finishes,
        "note": product.tech_note, "summary_ru": product.summary_ru,
        # Без отделок: их отмечает менеджер, см. to_excel_description.
        "description": product_lookup.to_excel_description(product, with_finishes=False),
        "photos": product.photo_urls, "doc_urls": product.doc_urls,
    }
    # False — законное значение флага, а не пустота: фильтр сравнивает
    # по равенству, и без явной оговорки dims_confident=False уцелел бы
    # случайно, а не по правилу.
    fresh = {k: v for k, v in fresh.items()
             if k in DOWNGRADE_ONLY or v not in (None, "", [], {})}

    filled, differs = [], {}
    updated = dict(item)
    for key, value in fresh.items():
        if key in DOWNGRADE_ONLY:
            updated[key] = bool(item.get(key, True)) and bool(value)
            if updated[key] != item.get(key):
                filled.append(key)
        elif item.get(key) in (None, "", [], {}):
            updated[key] = value
            filled.append(key)
        elif key in EDITABLE and item.get(key) != value:
            differs[key] = value

    try:
        library.save(updated)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Не удалось сохранить: {exc}"}, 502
    return {"item": updated, "filled": filled, "differs": differs,
            "warnings": product.warnings}


@app.route("/library/save-many", methods=["POST"])
def library_save_many():
    """Все позиции проекта -> в каталог, одним движением.

    Индекс переписывается один раз в конце: иначе на каждую позицию
    пришлась бы полная перезапись индекса.
    """
    items = (request.get_json(silent=True) or {}).get("items")
    if not isinstance(items, list) or not items:
        return {"error": "Нечего сохранять."}, 400
    if len(items) > 200:
        return {"error": "Слишком много позиций за раз."}, 400

    saved, skipped = [], 0
    try:
        for item in items:
            if not isinstance(item, dict):
                skipped += 1
                continue
            try:
                saved.append(library.save(item, reindex=False)["id"])
            except ValueError:      # без бренда и модели карточки нет
                skipped += 1
        library.rebuild_index()
    except library.NotConfigured as exc:
        return {"error": str(exc)}, 400
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Не удалось сохранить: {exc}"}, 502

    return {"saved": len(saved), "skipped": skipped, "ids": saved}


@app.route("/library/save", methods=["POST"])
def library_save():
    """Положить карточку в библиотеку (или обновить существующую)."""
    item = request.get_json(silent=True) or {}
    try:
        saved = library.save(item)
    except (library.NotConfigured, ValueError) as exc:
        return {"error": str(exc)}, 400
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Не удалось сохранить: {exc}"}, 502
    return {"id": saved["id"], "saved_at": saved["saved_at"]}


@app.route("/library/delete", methods=["POST"])
def library_delete():
    item_id = (request.get_json(silent=True) or {}).get("id", "")
    if not re.fullmatch(r"[a-z0-9-]{1,120}", str(item_id)):
        return {"error": "Неверный опознаватель карточки."}, 400
    try:
        return {"deleted": library.delete(item_id)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Не удалось удалить: {exc}"}, 502


def _rooms(data: dict) -> list[str]:
    """Порядок комнат из запроса. Пустое — проект без комнат, это норма."""
    rooms = data.get("rooms")
    if rooms is None:
        return []
    if not isinstance(rooms, list) or len(rooms) > 100:
        raise ValueError("Комнаты должны быть списком не длиннее 100.")
    out = []
    for name in rooms:
        if not isinstance(name, str):
            raise ValueError("Название комнаты должно быть строкой.")
        name = name.strip()[:120]
        if name and name not in out:
            out.append(name)
    return out


def _incoming_project(data: dict) -> tuple[dict | None, str | None]:
    """Проект из запроса. Строже, чем у карточки: сервер ничего не
    перечитывает, поэтому кривой ответ лёг бы поверх целой работы."""
    project = data.get("project")
    if not isinstance(project, dict):
        return None, "Неверный запрос."
    project_id = str(project.get("id") or "")
    if not re.fullmatch(r"[a-z0-9-]{1,120}", project_id):
        return None, "Неверный опознаватель проекта."
    try:
        # В localStorage всё строки, а `5 > '3'` в Python падает — было бы
        # 500 на ровном месте вместо честного отказа.
        rev = int(project.get("rev") or 0)
    except (TypeError, ValueError):
        return None, "Неверный номер правки."
    positions = project.get("positions")
    if not isinstance(positions, list) or len(positions) > 200:
        return None, "Позиции должны быть списком не длиннее 200."
    if any(not isinstance(p, dict) for p in positions):
        return None, "Позиция должна быть записью."
    try:
        project["rooms"] = _rooms(project)
    except ValueError as exc:
        return None, str(exc)
    for key in ("header", "final", "rates"):
        if project.get(key) is not None and not isinstance(project.get(key), dict):
            return None, f"Поле «{key}» должно быть записью."
    return {**project, "id": project_id, "rev": rev, "positions": positions}, None


@app.route("/project/list")
def project_list():
    """Перечень проектов: открыть, найти, удалить."""
    query = (request.args.get("q") or "").strip()
    try:
        rows = projects.read_index()
    except projects.NotConfigured as exc:
        return render_template("projects.html", rows=[], query=query,
                               total=0, error=str(exc))
    except Exception as exc:  # noqa: BLE001
        return render_template("projects.html", rows=[], query=query, total=0,
                               error=f"Список не открылся: {exc}")
    return render_template("projects.html", rows=projects.search(rows, query),
                           query=query, total=len(rows), error=None)


def _project_choices() -> list[dict]:
    """Список проектов для выбора «в какой положить позицию».

    Сбой хранилища не повод отнимать кнопку: без списка остаётся текущий
    черновик и новый проект, и это рабочий путь.
    """
    try:
        # В строке индекса имя лежит в «title» — «name» там нет вовсе,
        # и окно выбора показывало опознаватели вместо названий.
        return [{"id": r.get("id"), "name": r.get("title") or r.get("id")}
                for r in projects.read_index() if r.get("id")]
    except Exception:            # noqa: BLE001 — карточка важнее списка
        return []


@app.route("/project/open/<project_id>")
def project_open(project_id: str):
    """Открыть сохранённый проект.

    Страницу без записи не рисуем: чтение глушит любую ошибку и отдаёт
    None, а страница с чужим черновиком под этим адресом записала бы его
    поверх целого проекта следующим же «Сохранить».
    """
    if not re.fullmatch(r"[a-z0-9-]{1,120}", project_id):
        return redirect(url_for("project_list"))
    try:
        project = projects.get(project_id)
    except Exception as exc:  # noqa: BLE001
        return render_template("projects.html", rows=[], query="", total=0,
                               error=f"Проект не открылся: {exc}"), 502
    if not project:
        # Два разных None: строка в списке есть — запись ещё не дошла;
        # строки нет — проекта нет вовсе.
        try:
            known = any(r.get("id") == project_id for r in projects.read_index())
        except Exception:        # noqa: BLE001
            known = False
        return render_template(
            "projects.html", rows=[], query="", total=0,
            error=("Запись ещё не дошла до хранилища — обновите страницу "
                   "через несколько секунд." if known else
                   "Такого проекта нет.")), 404 if not known else 503
    return render_template("project.html", project=project)


@app.route("/project/save", methods=["POST"])
def project_save():
    """Сохранить проект. Отказ по устаревшей правке — 409, не молчание."""
    project, error = _incoming_project(request.get_json(silent=True) or {})
    if error:
        return {"error": error}, 400
    try:
        saved = projects.save(project)
    except projects.Conflict as exc:
        return {"error": str(exc), "current": exc.current}, 409
    except projects.NotConfigured as exc:
        return {"error": str(exc)}, 400
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Не удалось сохранить: {exc}"}, 502
    return {"id": saved["id"], "rev": saved["rev"], "saved_at": saved["saved_at"]}


@app.route("/project/delete", methods=["POST"])
def project_delete():
    project_id = str((request.get_json(silent=True) or {}).get("id") or "")
    if not re.fullmatch(r"[a-z0-9-]{1,120}", project_id):
        return {"error": "Неверный опознаватель проекта."}, 400
    try:
        projects.delete(project_id)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Не удалось удалить: {exc}"}, 502
    return {"deleted": project_id}


@app.route("/project/reindex", methods=["POST"])
def project_reindex():
    """Пересобрать список из файлов — вернуть строку, потерянную гонкой."""
    try:
        return {"projects": projects.rebuild_index()}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Не удалось пересобрать: {exc}"}, 502


@app.route("/settings")
def settings():
    """Величины расчёта — одни на все проекты."""
    return render_template("settings.html",
                           defaults=pricing.DEFAULT_RATES,
                           position_defaults=pricing.DEFAULT_POSITION)


@app.route("/calc", methods=["POST"])
def calc():
    """Позиции -> расчёт по цепочке рабочей книги.

    Считает сервер, а не браузер: формула должна быть одна на приложение,
    иначе появятся две правды и разойдутся.
    """
    data = request.get_json(silent=True) or {}
    positions = data.get("positions")
    if not isinstance(positions, list):
        return {"error": "Нужен список позиций."}, 400
    if len(positions) > 500:
        return {"error": "Слишком много позиций за раз."}, 400

    return pricing.project(positions, rates=data.get("rates"),
                           final=data.get("final"))


@app.route("/project/export", methods=["POST"])
def project_export():
    """Проект -> файл Excel в разметке рабочей формы, с фотографиями."""
    data = request.get_json(silent=True) or {}
    positions = data.get("positions")
    if not isinstance(positions, list) or not positions:
        return {"error": "В проекте нет позиций."}, 400
    if len(positions) > 200:
        return {"error": "Слишком много позиций за раз."}, 400

    try:
        rooms = _rooms(data)
    except ValueError as exc:
        return {"error": str(exc)}, 400

    try:
        content = book_export.build(positions, rates=data.get("rates"),
                                    header=data.get("header"),
                                    final=data.get("final"), rooms=rooms)
    except Exception as exc:  # noqa: BLE001 — причину показываем пользователю
        return {"error": f"Не удалось собрать файл: {exc}"}, 500

    # В filename= только латиница. Заголовки HTTP кодируются latin-1, и
    # кириллица в нём роняла ответ на проде (UnicodeEncodeError) уже после
    # того, как файл был собран: браузер получал обрыв и говорил «не удалось
    # собрать файл». Настоящее имя приходит вторым параметром, filename*,
    # он для того и придуман (RFC 6266).
    name = "AURRUM-проект.xlsx"
    return Response(content, headers={
        "Content-Type": "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet",
        "Content-Disposition": 'attachment; filename="AURRUM-project.xlsx"; '
                               f"filename*=UTF-8''{quote(name)}",
    })


@app.route("/book-row", methods=["POST"])
def book_row_route():
    """Карточка -> строка для вставки в рабочую книгу.

    Собираем на сервере, а не в браузере: формулы привязаны к номеру
    строки, и ошибка здесь тихо уводит ссылки на соседние позиции.
    """
    data = request.get_json(silent=True) or {}
    try:
        row = int(data.get("row") or 0)
    except (TypeError, ValueError):
        row = 0
    if not 2 <= row <= 10000:
        return {"error": "Укажите номер строки в книге — от 2 до 10000."}, 400

    return {
        "row": row,
        "tsv": book_row.build(data, row, with_pricing=bool(data.get("with_pricing"))),
    }


@app.route("/photo")
def photo():
    """Отдаёт картинку с сайта бренда как файл.

    Через прокси, а не напрямую: атрибут download браузер игнорирует на
    чужом домене — ссылка просто открывается в соседней вкладке, и фото
    приходится сохранять руками по одному.
    """
    url = (request.args.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return "Нужна ссылка на изображение.", 400

    try:
        # Без User-Agent часть CDN отдаёт 403 — притворяемся браузером.
        got = safe_fetch.get(url, timeout=30, headers={
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/131.0.0.0 Safari/537.36"),
            "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
        })
    except safe_fetch.UnsafeUrl as exc:
        return str(exc), 400
    except Exception as exc:  # noqa: BLE001 — причину показываем пользователю
        return f"Не удалось скачать: {exc}", 502

    mime = got.headers.get("content-type", "").split(";")[0].strip()
    if not mime.startswith("image/"):
        return "По ссылке не изображение.", 400

    # ASCII: \w в юникоде пропускает кириллицу, а она в заголовке не живёт.
    name = re.sub(r"[^\w.\-]", "_", url.split("?")[0].rsplit("/", 1)[-1],
                  flags=re.ASCII) or "photo.jpg"
    return Response(got.content, mimetype=mime, headers={
        "Content-Disposition": f'attachment; filename="{name}"',
    })


@app.route("/project/print", methods=["POST"])
def project_print():
    """Проект -> компред в фирменном дизайне, минуя файл.

    Путь нарочно тот же, что у загруженного Excel: проект складывается
    в книгу в памяти (`book_export`), и её разбирает та же печать
    (`spec_parser` -> spec.html). Одна правда: что ушло бы в файл,
    то и на печати — вплоть до формул итога и фотографий.
    """
    try:
        data = json.loads(request.form.get("payload") or "{}")
    except ValueError:
        return render_template("index.html", error="Не разобран запрос печати."), 400
    positions = data.get("positions")
    if not isinstance(positions, list) or not positions:
        return render_template("index.html", error="В проекте нет позиций."), 400
    if len(positions) > 200:
        return render_template("index.html", error="Слишком много позиций за раз."), 400

    try:
        rooms = _rooms(data)
    except ValueError as exc:
        return render_template("index.html", error=str(exc)), 400

    try:
        content = book_export.build(positions, rates=data.get("rates"),
                                    header=data.get("header"),
                                    final=data.get("final"), values=True,
                                    rooms=rooms)
        spec = spec_parser.parse(content)
    except Exception as exc:  # noqa: BLE001 — причину показываем пользователю
        return render_template(
            "index.html", error=f"Не удалось собрать компред: {exc}"
        ), 400

    return render_template("spec.html", spec=spec)


@app.route("/convert", methods=["POST"])
def convert():
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return render_template("index.html", error="Файл не выбран."), 400

    ext = os.path.splitext(upload.filename)[1].lower()
    if ext not in ALLOWED:
        return render_template(
            "index.html",
            error=f"Нужен файл Excel ({', '.join(sorted(ALLOWED))}), а пришёл «{ext or '?'}».",
        ), 400

    # Книга живёт только в памяти запроса: на диск ничего не кладём,
    # фото уезжают в документ как data-URI.
    try:
        spec = spec_parser.parse(upload.read())
    except Exception as exc:  # noqa: BLE001 — показываем причину пользователю
        return render_template(
            "index.html", error=f"Не удалось разобрать файл: {exc}"
        ), 400

    return render_template("spec.html", spec=spec)


@app.errorhandler(413)
def too_large(_):
    return render_template(
        "index.html", error=f"Файл больше {MAX_MB} МБ."
    ), 413


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=True)
