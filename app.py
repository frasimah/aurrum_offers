# -*- coding: utf-8 -*-
"""
AURRUM — конвертер спецификаций: Excel на входе, готовый документ для печати
(HTML + печать в PDF средствами браузера) на выходе.
"""

from __future__ import annotations

import hmac
import os
import time
from collections import defaultdict
from datetime import timedelta

from markupsafe import Markup, escape

from dotenv import load_dotenv
from flask import (Flask, redirect, render_template, request, session, url_for)

# Ключи и пароли живут в .env (в git не попадает; образец — .env.example).
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import product_lookup  # noqa: E402 — после load_dotenv, читает переменные окружения
import spec_parser  # noqa: E402

ALLOWED = {".xlsx", ".xlsm"}
MAX_MB = 25

# Общий пароль на вход.
#
# ВРЕМЕННЫЙ РЕЖИМ: дефолт «123», чтобы приложение работало на Vercel без
# настройки переменных. Пароль такого вида подбирается автоматическими
# сканерами за минуты, поэтому перед реальной работой задайте
# AURRUM_PASSWORD в Project Settings -> Environment Variables:
# переменная перекрывает дефолт, править код не нужно.
PASSWORD = os.environ.get("AURRUM_PASSWORD") or "123"

# Защита от перебора: N неудачных попыток с одного адреса — пауза.
MAX_FAILS = 5
LOCK_SECONDS = 300
_fails: dict[str, list[float]] = defaultdict(list)

# Роуты, доступные без пароля
PUBLIC_ENDPOINTS = {"login", "static"}

app = Flask(__name__)
app.config.update(
    MAX_CONTENT_LENGTH=MAX_MB * 1024 * 1024,
    # На нескольких инстансах Vercel случайный ключ на каждом процессе означал
    # бы постоянный разлогин, поэтому во временном режиме держим фиксированный
    # дефолт. AURRUM_SECRET_KEY его перекрывает — задайте на проде.
    SECRET_KEY=os.environ.get("AURRUM_SECRET_KEY") or "aurrum-temp-session-key",
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
    ip = _client_ip()
    if _locked(ip):
        return render_template(
            "login.html", error="Слишком много попыток. Повторите через 5 минут."
        ), 429

    if request.method == "POST":
        if hmac.compare_digest(request.form.get("password", ""), PASSWORD):
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
