"""Маскировка приватного iCal-адреса в логах.

Адрес календаря содержит приватный токен (даёт чтение всего календаря), а `httpx` печатает URL
запроса в INFO. Фильтр вешается на обработчики root, поэтому ловит
и строку httpx, и наши сообщения с ошибкой (текст исключения httpx тоже содержит URL).
"""

from __future__ import annotations

import logging
import re

PRIVATE_ICAL_RE = re.compile(r"(/private-)[^/\s\"']+")
MARKER = "/private-"
MASK = r"\1***"


def _mask(item):
    """Маска в одном месте: и для кортежа аргументов, и для словаря (см. `filter`)."""
    if isinstance(item, str) and MARKER in item:
        return PRIVATE_ICAL_RE.sub(MASK, item)
    return item


class RedactPrivateUrls(logging.Filter):
    """`/private-<токен>` → `/private-***` в самом сообщении и в его аргументах."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and MARKER in record.msg:
            record.msg = PRIVATE_ICAL_RE.sub(MASK, record.msg)
        # ⚠️ `record.args` бывает СЛОВАРЁМ: logging разворачивает единственный mapping-аргумент
        # (`log.warning("…: %r", payload)` с dict ⇒ args = payload, а не кортеж). Перебор словаря
        # дал бы кортеж его КЛЮЧЕЙ, и форматирование падало бы с «not all arguments converted»:
        # запись пропадала бы из журнала, а на её месте появлялся бы стек ошибки логирования.
        # Поэтому форму аргумента сохраняем: кортеж — кортежем, словарь — словарём.
        if isinstance(record.args, tuple):
            record.args = tuple(_mask(item) for item in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: _mask(item) for key, item in record.args.items()}
        return True


def install() -> None:
    """Повесить фильтр на логгер `httpx` И на обработчики root.

    Одних обработчиков root мало: uvicorn после импорта приложения применяет свой `dictConfig`, и
    строка httpx проходит незамаскированной.
    Фильтр на самом логгере `httpx` ловит записи независимо от конфигурации обработчиков.
    """
    filter_instance = RedactPrivateUrls()
    httpx_logger = logging.getLogger("httpx")
    if not any(isinstance(item, RedactPrivateUrls) for item in httpx_logger.filters):
        httpx_logger.addFilter(filter_instance)
    for handler in logging.getLogger().handlers:
        if not any(isinstance(item, RedactPrivateUrls) for item in handler.filters):
            handler.addFilter(RedactPrivateUrls())
