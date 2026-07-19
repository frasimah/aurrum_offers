# -*- coding: utf-8 -*-
"""
AURRUM — конвертер спецификаций: Excel на входе, готовый документ для печати
(HTML + печать в PDF средствами браузера) на выходе.
"""

from __future__ import annotations

import hmac
import os
import secrets
import shutil
import time
import uuid
from collections import defaultdict
from datetime import timedelta

from markupsafe import Markup, escape

from flask import (Flask, abort, redirect, render_template, request, session,
                   send_from_directory, url_for)
from werkzeug.utils import secure_filename

import spec_parser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(BASE_DIR, "jobs")
ALLOWED = {".xlsx", ".xlsm"}
MAX_MB = 25
JOB_TTL = 6 * 3600  # разобранные файлы живут 6 часов

# Общий пароль на вход. Задаётся переменной окружения — в репозитории его нет.
PASSWORD = os.environ.get("AURRUM_PASSWORD", "")

# Защита от перебора: N неудачных попыток с одного адреса — пауза.
MAX_FAILS = 5
LOCK_SECONDS = 300
_fails: dict[str, list[float]] = defaultdict(list)

# Роуты, доступные без пароля
PUBLIC_ENDPOINTS = {"login", "static"}

app = Flask(__name__)
app.config.update(
    MAX_CONTENT_LENGTH=MAX_MB * 1024 * 1024,
    # Без AURRUM_SECRET_KEY сессии переживут только текущий процесс —
    # для локального запуска нормально, на сервере ключ стоит задать.
    SECRET_KEY=os.environ.get("AURRUM_SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("AURRUM_HTTPS")),
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)

os.makedirs(JOBS_DIR, exist_ok=True)


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
    if not PASSWORD:
        return render_template(
            "login.html",
            error="На сервере не задан пароль: переменная окружения AURRUM_PASSWORD.",
        ), 503

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


# --- Уборка ------------------------------------------------------------

def cleanup_jobs() -> None:
    """Удаляем старые задания: на диске лежат клиентские данные."""
    now = time.time()
    try:
        for job in os.listdir(JOBS_DIR):
            path = os.path.join(JOBS_DIR, job)
            if os.path.isdir(path) and now - os.path.getmtime(path) > JOB_TTL:
                shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


# --- Роуты -------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", error=request.args.get("error"))


@app.route("/convert", methods=["POST"])
def convert():
    cleanup_jobs()

    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return render_template("index.html", error="Файл не выбран."), 400

    ext = os.path.splitext(upload.filename)[1].lower()
    if ext not in ALLOWED:
        return render_template(
            "index.html",
            error=f"Нужен файл Excel ({', '.join(sorted(ALLOWED))}), а пришёл «{ext or '?'}».",
        ), 400

    job = uuid.uuid4().hex
    job_dir = os.path.join(JOBS_DIR, job)
    os.makedirs(job_dir, exist_ok=True)

    src = os.path.join(job_dir, secure_filename(upload.filename) or "spec.xlsx")
    upload.save(src)

    try:
        spec = spec_parser.parse(src, job_dir)
    except Exception as exc:  # noqa: BLE001 — показываем причину пользователю
        shutil.rmtree(job_dir, ignore_errors=True)
        return render_template(
            "index.html", error=f"Не удалось разобрать файл: {exc}"
        ), 400
    finally:
        # Исходник с внутренними колонками на диске не держим
        if os.path.exists(src):
            os.remove(src)

    return render_template("spec.html", spec=spec, job=job)


@app.route("/photo/<job>/<name>")
def photo(job: str, name: str):
    if not job.isalnum():
        abort(404)
    job_dir = os.path.join(JOBS_DIR, job)
    if not os.path.isdir(job_dir):
        abort(404)
    return send_from_directory(job_dir, secure_filename(name))


@app.errorhandler(413)
def too_large(_):
    return render_template(
        "index.html", error=f"Файл больше {MAX_MB} МБ."
    ), 413


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=True)
