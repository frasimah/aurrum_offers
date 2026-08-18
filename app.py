# -*- coding: utf-8 -*-
"""
AURRUM — конвертер спецификаций: Excel на входе, готовый документ для печати
(HTML + печать в PDF средствами браузера) на выходе.
"""

from __future__ import annotations

import hmac
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
import book_row  # noqa: E402
import doc_parser  # noqa: E402
import extract_agent  # noqa: E402
import pricing  # noqa: E402
import product_lookup  # noqa: E402 — после load_dotenv, читают переменные окружения
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

    return render_template(
        "lookup.html",
        url=url,
        product=product,
        description=product_lookup.to_excel_description(product),
        types=product_lookup.TYPES_RU,
    )


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
        w, d, h, sure = product_lookup.parse_dims(cand["value"])
        candidates.append({
            **cand, "sku": None,
            "width_cm": w, "depth_cm": d, "height_cm": h,
            "volume_m3": product_lookup.volume_m3(w, d, h),
            "volume_source": "формула", "dims_confident": sure,
        })

    return {"candidates": candidates, "finishes": [], "source": "fallback",
            "warnings": [f"Извлечение не сработало ({reason}) — показаны "
                         f"размеры, найденные по тексту, без артикулов."]}


@app.route("/project")
def project():
    """Набранные позиции и расчёт по ним.

    Сам список живёт в браузере: базы у приложения нет, а на serverless
    нет и диска. Зато перезагрузка страницы больше ничего не теряет.
    """
    return render_template("project.html")


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

    return pricing.project(positions, rates=data.get("rates"))


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
        content = book_export.build(positions, rates=data.get("rates"),
                                    header=data.get("header"))
    except Exception as exc:  # noqa: BLE001 — причину показываем пользователю
        return {"error": f"Не удалось собрать файл: {exc}"}, 500

    name = "AURRUM-проект.xlsx"
    return Response(content, headers={
        "Content-Type": "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet",
        "Content-Disposition": f'attachment; filename="{name}"; '
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

    name = re.sub(r"[^\w.\-]", "_", url.split("?")[0].rsplit("/", 1)[-1]) or "photo.jpg"
    return Response(got.content, mimetype=mime, headers={
        "Content-Disposition": f'attachment; filename="{name}"',
    })


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
