"""`trace_id` в строках журнала: строка лога и трейс должны указывать друг на друга.

**Почему отдельный модуль, а не `logging_redact`.** У того модуля одна причина меняться — приватный
адрес календаря в логах. Здесь другая: связать журнал с трейсом. Смешав их, мы получили бы файл, за
который отвечают две разные задачи.

**Где это ломается и почему так устроено.** uvicorn после импорта приложения применяет свой
`dictConfig` и переписывает обработчики и форматы. В проекте уже есть этот случай
(см. `logging_redact.install`): фильтр «только на обработчиках» не спас. Поэтому
здесь (а) фильтр ставится и на свои логгеры, и на обработчики root, а (б) форматёры оборачиваются
**на старте lifespan**, то есть уже после uvicorn. Без поля в записи `trace_id` просто не печатается —
ошибки форматирования не возникает, и это осознанно: журнал не должен падать из-за наблюдения.
"""
from __future__ import annotations

import logging

from agent_core import telemetry

#: Логгеры, к которым фильтр вешается напрямую: их задаёт продукт при старте.
OWN_LOGGERS: tuple[str, ...] = ()


class TraceContext(logging.Filter):
    """Кладёт в запись `trace_id` текущего спана (пустая строка, если спана нет)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = telemetry.trace_id()
        return True


class TraceFormatter(logging.Formatter):
    """Дописать `trace_id` к строке журнала — и только когда он есть.

    Пустой хвост `trace_id=` в каждой строке был бы шумом: вне спанов (при выключенной трассировке)
    строки должны выглядеть как раньше.
    """

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        value = getattr(record, "trace_id", "")
        return f"{base} trace_id={value}" if value else base


def install(*owners: str) -> None:
    """Повесить фильтр на логгеры продукта и обернуть форматёры.

    `owners` — имена логгеров, которые продукт ведёт сам (их настройки никто не переписывает; uvicorn
    трогает root). Идемпотентно: повторный вызов ничего не дублирует.
    """
    for name in (*OWN_LOGGERS, *owners):
        logger = logging.getLogger(name)
        if not any(isinstance(item, TraceContext) for item in logger.filters):
            logger.addFilter(TraceContext())

    root = logging.getLogger()
    if not any(isinstance(item, TraceContext) for item in root.filters):
        root.addFilter(TraceContext())  # записи без своего логгера тоже получают поле
    for handler in root.handlers:
        if not any(isinstance(item, TraceContext) for item in handler.filters):
            handler.addFilter(TraceContext())
        if not isinstance(handler.formatter, TraceFormatter):
            handler.setFormatter(TraceFormatter(
                handler.formatter._fmt if handler.formatter else "%(asctime)s %(levelname)s %(name)s: %(message)s"))
