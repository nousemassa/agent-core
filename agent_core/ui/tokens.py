"""Подпись и разбор данных кнопок.

Требования (план v2 /, v3.1 ):
  * подпись покрывает чат, вид действия, аргумент и токен хода — иначе валидная кнопка
    одного чата принимается в другом;
  * данные не доверяем: `arg` — только индекс в серверном списке предложенного (см. dialog);
  * секрет кнопок (UI_SECRET) отдельный от `bridge_api_key` и не имеет пустого дефолта;
  * `callback_data` Telegram ≤ 64 байт — держим формат `c:<kind>:<arg>:<mac8>`.
"""
from __future__ import annotations

import hashlib
import hmac

MAX_CALLBACK_BYTES = 64
PREFIX = "c"
# 16 hex = 64 бита: 8 hex (32 бита) перебираемы, а лимита на callback у нас нет.
TAG_HEX = 16


def mac(secret: str, *, chat_id: str, kind: str, arg: str, token: str) -> str:
    message = f"{chat_id}|{kind}|{arg}|{token}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()[:TAG_HEX]


def sign(secret: str, *, chat_id: str, kind: str, arg: str = "", token: str = "") -> str:
    data = f"{PREFIX}:{kind}:{arg}:{mac(secret, chat_id=chat_id, kind=kind, arg=arg, token=token)}"
    if len(data.encode()) > MAX_CALLBACK_BYTES:
        raise ValueError(f"callback_data длиннее {MAX_CALLBACK_BYTES} байт: {data!r}")
    return data


def verify(secret: str, data: str, *, chat_id: str, token: str) -> tuple[str, str] | None:
    """(kind, arg) или None: битая подпись, чужой чат, чужой токен."""
    if not secret:
        return None
    parts = (data or "").split(":")
    if len(parts) != 4 or parts[0] != PREFIX:
        return None
    _, kind, arg, digest = parts
    expected = mac(secret, chat_id=chat_id, kind=kind, arg=arg, token=token)
    return (kind, arg) if hmac.compare_digest(expected, digest) else None
