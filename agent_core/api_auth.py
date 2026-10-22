"""Проверка ключа `X-API-Key` для HTTP-ручек.

**Почему это в ОБЩЕМ слое.** Ключ проверяют несколько процессов, и проверка должна быть одна: копия в
каждом продукте разошлась бы в поведении. Механика не про предметную область, а про доступ к ручкам.

**Что здесь важно:** сравнение constant-time (`hmac.compare_digest`) и
отказ при коротком ожидаемом ключе. Ключ короче 32 байт считается негодным НЕ по длине присланного, а по
длине ожидаемого — иначе короткий «правильный» ключ из конфигурации проходил бы проверку.
"""
from __future__ import annotations

import hmac


def check_api_key(provided: str | None, expected: str) -> bool:
    """X-API-Key ≥32 байт, сравнение constant-time."""
    if not expected or len(expected) < 32 or not provided:
        return False
    return hmac.compare_digest(provided, expected)
