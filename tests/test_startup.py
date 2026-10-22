"""Ожидание готовности зависимостей на старте: повторы, бюджет, проброс последней ошибки."""
from __future__ import annotations

import pytest

from agent_core import startup

FAST = {"budget_s": 0.5, "first_delay_s": 0.001, "max_delay_s": 0.002}


async def test_ready_on_first_attempt_does_not_retry():
    calls = 0

    async def probe() -> None:
        nonlocal calls
        calls += 1

    await startup.wait_ready(probe, what="тест", **FAST)
    assert calls == 1


async def test_cold_start_gonka_waited_out():
    """Гонка перезагрузки хоста: зависимость отвечает не с первой попытки."""
    calls = 0

    async def probe() -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionRefusedError(111, "Connection refused")

    await startup.wait_ready(probe, what="тест", **FAST)
    assert calls == 3


async def test_http_502_is_also_not_ready():
    """Канал Agenta на холодном старте отдаёт 502 — это тоже «не готов», ждём."""
    calls = 0

    async def probe() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("502 Bad Gateway")

    await startup.wait_ready(probe, what="тест", **FAST)
    assert calls == 2


async def test_dead_dependency_raises_last_error_after_budget():
    """Мёртвая зависимость обязана довести старт до падения: лимитер юнита — настоящий сторож."""
    calls = 0

    async def probe() -> None:
        nonlocal calls
        calls += 1
        raise ConnectionRefusedError(111, "Connection refused")

    with pytest.raises(ConnectionRefusedError):
        await startup.wait_ready(probe, what="тест", **FAST)
    assert calls >= 2
