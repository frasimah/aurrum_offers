# -*- coding: utf-8 -*-
"""
Скачивание по ссылке от пользователя — с проверкой адреса назначения.

Приложение ходит по ссылкам, которые вводит пользователь (фото, техлист).
Без проверки это открытый прокси во внутреннюю сеть: снаружи не видно тела
ответа, но по коду и времени ответа отличается «соединение отклонено» от
«таймаута», а этого хватает, чтобы прощупать, что крутится рядом.

Редиректы проверяем на каждом шаге: публичный адрес может увести на
127.0.0.1, и обычный follow_redirects такую подмену пропустит.

Чего эта проверка не даёт: адрес резолвится при проверке и ещё раз при
соединении, и между ними DNS может ответить иначе (DNS rebinding). Закрыть
это можно только соединением по уже проверенному IP — для внутреннего
инструмента избыточно.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx

MAX_REDIRECTS = 5


class UnsafeUrl(ValueError):
    """Ссылка ведёт не туда, куда мы готовы ходить."""


def assert_public(url: str) -> None:
    parts = urlparse(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeUrl("Нужна ссылка http:// или https://.")
    host = parts.hostname
    if not host:
        raise UnsafeUrl("В ссылке нет адреса.")

    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeUrl(f"Адрес «{host}» не разрешается.") from exc

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved \
                or ip.is_multicast or not ip.is_global:
            raise UnsafeUrl(
                f"Адрес «{host}» ведёт внутрь сети ({ip}) — такие ссылки не открываю."
            )


def get(url: str, *, timeout: float = 60, headers: dict | None = None) -> httpx.Response:
    """GET с проверкой адреса на каждом редиректе."""
    seen = url
    with httpx.Client(timeout=timeout, follow_redirects=False) as http:
        for _ in range(MAX_REDIRECTS + 1):
            assert_public(seen)
            got = http.get(seen, headers=headers or {})
            if got.is_redirect and got.headers.get("location"):
                seen = str(got.next_request.url) if got.next_request else got.headers["location"]
                continue
            got.raise_for_status()
            return got
    raise UnsafeUrl("Слишком много перенаправлений.")
