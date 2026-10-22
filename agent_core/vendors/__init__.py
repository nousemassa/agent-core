"""Фабрика формы спана: выбор адаптера по настройке `PLATFORM_VENDOR`.

**Единственная ручка.** Решение владельца: переключение — ОДНИМ ключом в корневом `.env`,
без матрицы переопределений по осям. `agenta` — значение по умолчанию ⇒ поведение не меняется.

**Где выбирается.** В `setup_telemetry` (`telemetry.py`) — он и есть «единственный вход к трассировке».
❗ Вызов `select` стоит там ПЕРВОЙ строкой, ДО кэш-выхода: иначе повторный вызов с тем же адресом
вернул бы кэшированный трейсер и вендора НЕ пересмотрел — смена `PLATFORM_VENDOR` не сработала бы
молча, а молчаливый отказ от переключения ловится тестом.
"""
from __future__ import annotations

from agent_core.vendors import agenta, openlit
from agent_core.vendors.neutral import AGENT, NODE, WORKFLOW, SpanShape, Usage
from agent_core.vendors.port import Receiver, TraceShape

#: Значения `PLATFORM_VENDOR`.
#: ❗ `own` здесь НЕТ и быть не должно: «свой харнесс» — это ДРУГАЯ ось (`AGENT_RUNTIME = agenta | own`,
#: кто исполняет ход). `PLATFORM_VENDOR` отвечает только за наблюдаемость, и у OpenLIT адаптера
#: рантайма нет вовсе — он агента не исполняет.
AGENTA = "agenta"
OPENLIT = "openlit"
VENDORS = (AGENTA, OPENLIT)

_ADAPTERS: dict[str, TraceShape] = {AGENTA: agenta, OPENLIT: openlit}
_ACTIVE: TraceShape | None = None


def select(vendor: str) -> None:
    """Выбрать адаптер по значению `PLATFORM_VENDOR`.

    **Что считается ошибкой, а что нет.** Пустое значение — это «не задано», то же самое, что значение
    по умолчанию `agenta` (пустой ключ в `.env` не должен ронять службу). Регистр не различаем:
    `OpenLIT` и `openlit` — одно значение, это формат, а не смысл. А вот НЕИЗВЕСТНОЕ значение —
    ГРОМКИЙ отказ, а не молчаливый откат к `agenta`: иначе владелец поставил бы
    `PLATFORM_VENDOR=openlit`, спаны продолжали бы уезжать в Agenta, и по журналу это было бы
    неотличимо от «всё хорошо».
    """
    global _ACTIVE
    name = (vendor or "").strip().lower() or AGENTA
    if name not in _ADAPTERS:
        raise ValueError(
            f"неизвестный вендор платформы {vendor!r}: известны {', '.join(VENDORS)}. "
            f"Молчаливый откат к {AGENTA} соврал бы: спаны пошли бы не туда, куда просили."
        )
    _ACTIVE = _ADAPTERS[name]


def active() -> TraceShape:
    """Активный адаптер. По умолчанию — `agenta`: переключения не делаем, поведение прежнее."""
    return _ACTIVE if _ACTIVE is not None else agenta


__all__ = [
    "AGENT",
    "AGENTA",
    "NODE",
    "OPENLIT",
    "VENDORS",
    "WORKFLOW",
    "Receiver",
    "SpanShape",
    "TraceShape",
    "Usage",
    "active",
    "select",
]
