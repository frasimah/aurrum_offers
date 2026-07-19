# -*- coding: utf-8 -*-
"""
AURRUM — конвертер спецификаций: Excel на входе, готовый документ для печати
(HTML + печать в PDF средствами браузера) на выходе.
"""

from __future__ import annotations

import os
import shutil
import time
import uuid
from markupsafe import Markup, escape

from flask import (Flask, abort, render_template, request,
                   send_from_directory, url_for)
from werkzeug.utils import secure_filename

import spec_parser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(BASE_DIR, "jobs")
ALLOWED = {".xlsx", ".xlsm"}
MAX_MB = 25
JOB_TTL = 6 * 3600  # разобранные файлы живут 6 часов

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024

os.makedirs(JOBS_DIR, exist_ok=True)


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
