"""Компонент наката: имя, DDL, ревизия и проверка — то, чем продукт описывает СВОЮ схему."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from agent_core.db import schema_core, schema_runtime


@dataclass(frozen=True)
class Component:
    """Один владелец схемы: свои таблицы, своя ревизия, своя проверка наката.

    `url_key` говорит, к какой базе компонент относится: `primary` — основная база продукта,
    `vectors` — отдельная векторная база. Адреса передаёт вызывающий, компонент их не знает.
    """

    name: str
    ddl: str
    revision: int
    check_sql: str
    expected: int
    settings: Mapping[str, Any] = field(default_factory=dict)
    hint: str = ""
    url_key: str = "primary"
    #: Прежние имена ЭТОГО компонента, которые ещё может писать старый образ продукта. Объявляет
    #: продукт: ядро не знает, какие имена были у чужих схем.
    legacy_names: tuple[str, ...] = ()


CORE_REVISION = 1

CORE_COMPONENT = Component(
    name="core",
    ddl=schema_core.DDL,
    revision=CORE_REVISION,
    check_sql=schema_core.SCHEMA_CHECK_SQL,
    expected=schema_core.SCHEMA_CHECK_EXPECTED,
    settings=schema_core.DEFAULT_SETTINGS,
    hint=schema_core.SCHEMA_CHECK_HINT,
)

CORE_COMPONENTS = (CORE_COMPONENT,)

RUNTIME_REVISION = 1

RUNTIME_COMPONENT = Component(
    name="runtime",
    ddl=schema_runtime.DDL,
    revision=RUNTIME_REVISION,
    check_sql=schema_runtime.SCHEMA_CHECK_SQL,
    expected=schema_runtime.SCHEMA_CHECK_EXPECTED,
    hint=schema_runtime.SCHEMA_CHECK_HINT,
)

#: Механизм бота: ходы, очередь, согласия, история реплик. Накатывает продукт, которому он нужен.
RUNTIME_COMPONENTS = (RUNTIME_COMPONENT,)
