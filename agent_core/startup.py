"""Ожидание готовности внешних зависимостей на старте сервиса.

После перезагрузки хоста база и контейнер платформы поднимаются позже сервиса: `lifespan`
падал, uvicorn выходил кодом 3, и юнит перезапускался до четырёх раз подряд. Ждём с backoff в
пределах бюджета: этого хватает холодному стеку, а действительно мёртвая зависимость всё
равно доводит юнит до `failed` — лимитер юнита (`StartLimitBurst=5`/60 с) намеренно сохранён.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

WAIT_BUDGET_S = 45.0
FIRST_DELAY_S = 1.0
MAX_DELAY_S = 5.0


async def wait_ready(
    probe: Callable[[], Awaitable[object]],
    *,
    what: str,
    budget_s: float = WAIT_BUDGET_S,
    first_delay_s: float = FIRST_DELAY_S,
    max_delay_s: float = MAX_DELAY_S,
) -> None:
    """Звать `probe()` до первого успеха, но не дольше `budget_s`; иначе — последняя ошибка наружу."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_s
    delay = first_delay_s
    attempt = 0
    while True:
        attempt += 1
        try:
            await probe()
        except Exception as error:  # неготовность бывает любой: отказ соединения, HTTP 502, таймаут
            if loop.time() + delay > deadline:
                log.error("startup: %s не готов за %.0f с (%d попыток): %s", what, budget_s, attempt, error)
                raise
            log.warning("startup: %s не готов (попытка %d): %s", what, attempt, error)
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay_s)
            continue
        if attempt > 1:
            log.info("startup: %s готов (попытка %d)", what, attempt)
        return
