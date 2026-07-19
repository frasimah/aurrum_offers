# -*- coding: utf-8 -*-
"""Точка входа для Vercel: их Python-рантайм ищет WSGI-объект `app`.

Файл лежит в api/, а само приложение — в корне, поэтому корень нужно
добавить в путь импорта.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402,F401  — Vercel забирает этот объект
