"""Механизм подключения к Telegram (`agent_core/tg/connection.py`) — один на все процессы.

Одиночная попытка подключения падает после перезагрузки хоста, и бот стоит
выключенным четыре часа при активном юните и отвечающем `/health`. Проверяется то, чего не было:
перебор массива прокси по очереди, ожидание готовности и самостоятельное переподключение.

Проверки идут по МЕХАНИЗМУ, а не по каждому потребителю: он один и переиспользуется, поэтому и проверка у него одна. Кто именно вызывает механизм, стережёт
архитектурная проверка в CI.

Тесты офлайн: клиент Telethon подменяется двойником, сеть не нужна.
"""
from __future__ import annotations

import asyncio
import pathlib
import tempfile

import pytest

from agent_core.config import CoreSettings
from agent_core.tg.connection import TelegramConnection

SECRET = "ee" + "a1b2" * 8 + "c3d4e5f6a7b8"  # синтетический, НЕ боевой
SECRET2 = "dd" + "b1c2" * 8 + "c3d4e5f6a7b8"

#: Каталог сессий для проверок — НАСТОЯЩИЙ (создаётся один раз на модуль).
#: ❗ Путь задаётся явно: каталог сессий не выводится из расположения кода (в установленном слое он вёл
#: бы в `site-packages`, и бот не подключался бы молча), а тест, не использующий путь, этого не увидит.
TEST_SESSIONS_DIR = pathlib.Path(tempfile.mkdtemp(prefix="agenta-test-sessions-"))


def settings_with(proxies: str, *, legacy: bool = False, token: str = "123:abc") -> CoreSettings:
    base = {
        "_env_file": None,
        "sessions_dir": str(TEST_SESSIONS_DIR),
        "telegram_bot_token": token,
        "telegram_api_id": "2040",
        "telegram_api_hash": "b" * 32,
        "telegram_mt_proxies": proxies,
    }
    if legacy:
        base.update(telegram_mt_proxy_server="10.0.0.9", telegram_mt_proxy_port="443",
                    telegram_mt_proxy_secret=SECRET2)
    return CoreSettings(**base)


def connection(settings: CoreSettings, *, name: str = "тест") -> TelegramConnection:
    return TelegramConnection(settings=settings, name=name, session_name="test", token="123:abc")


@pytest.mark.asyncio
async def test_connects_through_the_second_proxy_when_the_first_fails(built_client):
    """Главное требование владельца: перебирать массив ПО ОЧЕРЕДИ, а не сдаваться на первом."""
    conn = connection(settings_with(f"10.0.0.1:443:{SECRET}, 10.0.0.2:443:{SECRET2}"))
    built_client.failures.add(0)

    await conn.connect()

    assert conn.running is True
    assert built_client.calls[0][0] == "10.0.0.1", "сначала первая запись"
    assert built_client.calls[1][0] == "10.0.0.2", "затем вторая"
    assert conn.proxy[0] == "10.0.0.2", "журнал должен называть фактический прокси"


@pytest.mark.asyncio
async def test_next_attempt_starts_from_the_working_proxy(built_client):
    """Запоминаем удачную запись: иначе каждый повтор ждал бы таймаут на мёртвой."""
    conn = connection(settings_with(f"10.0.0.1:443:{SECRET}, 10.0.0.2:443:{SECRET2}"))
    built_client.failures.add(0)

    await conn.connect()
    await conn.connect()

    assert built_client.calls[-1][0] == "10.0.0.2"


@pytest.mark.asyncio
async def test_all_proxies_failing_names_every_reason(built_client):
    """«Не подключилось» без перечня причин неотличимо от отказа сети — перечисляем по каждой записи."""
    conn = connection(settings_with(f"10.0.0.1:443:{SECRET}, 10.0.0.2:443:{SECRET2}"))
    built_client.failures.update({0, 1})

    with pytest.raises(ConnectionError) as error:
        await conn.connect()

    assert "10.0.0.1" in str(error.value) and "10.0.0.2" in str(error.value)
    assert conn.running is False


@pytest.mark.asyncio
async def test_failed_attempt_leaves_no_half_client(built_client):
    """Неудачная попытка не оставляет полуклиента: висящее соединение съедало бы сокеты."""
    conn = connection(settings_with(f"10.0.0.1:443:{SECRET}"))
    built_client.failures.add(0)

    with pytest.raises(ConnectionError):
        await conn.connect()

    assert conn.running is False


@pytest.mark.asyncio
async def test_single_legacy_proxy_still_works(built_client):
    """Прежняя форма настройки (одна тройка) остаётся рабочей: она стоит в живом `.env`."""
    conn = connection(settings_with("", legacy=True))

    await conn.connect()

    assert built_client.calls[0] == ("10.0.0.9", 443, SECRET2)


@pytest.mark.asyncio
async def test_start_waits_for_readiness_and_arms_the_watchdog(built_client, monkeypatch):
    """Старт не сдаётся после одной попытки: ждёт готовности и поднимает сторож."""
    monkeypatch.setattr("agent_core.tg.connection.WATCHDOG_TICK_S", 0.01)
    conn = connection(settings_with(f"10.0.0.1:443:{SECRET}"))
    seen: list[bool] = []

    async def on_connected(first: bool) -> None:
        seen.append(first)

    await conn.start(on_connected)
    assert seen == [True], "подключились с первой попытки — потребитель получил «это старт»"
    assert conn.running is True
    await conn.stop()


@pytest.mark.asyncio
async def test_start_gives_up_on_budget_but_keeps_trying(built_client, monkeypatch):
    """Исчерпание бюджета НЕ выключает бота: он продолжает попытки в фоне.

    Именно этого не хватало: единственная попытка упала — и функция оказалась
    выключенной навсегда, хотя маршрут поднялся через секунды.
    """
    monkeypatch.setattr("agent_core.tg.connection.WATCHDOG_TICK_S", 0.01)
    monkeypatch.setattr("agent_core.tg.connection.WATCHDOG_BUDGET_S", 0.5)
    conn = TelegramConnection(settings=settings_with(f"10.0.0.1:443:{SECRET}"), name="тест",
                              session_name="test", token="123:abc", start_budget_s=0.05)
    built_client.fail_all(True)  # маршрута ещё нет: не проходит ни одна попытка
    seen: list[bool] = []

    async def on_connected(first: bool) -> None:
        seen.append(first)

    await conn.start(on_connected)
    assert seen == [], "на старте подключиться не вышло — потребитель не получил события"

    # маршрут появился: сторож обязан добрать подключение сам
    built_client.fail_all(False)
    for _ in range(200):
        await asyncio.sleep(0.01)
        if seen:
            break
    await conn.stop()

    assert seen == [False], "восстановление сообщается потребителю как «не старт»"
    assert conn.running is False, "после stop соединения нет"


@pytest.mark.asyncio
async def test_watchdog_reconnects_after_the_connection_drops(built_client, monkeypatch):
    """После обрыва соединение возвращается само — ручной перезапуск не нужен."""
    monkeypatch.setattr("agent_core.tg.connection.WATCHDOG_TICK_S", 0.01)
    monkeypatch.setattr("agent_core.tg.connection.WATCHDOG_BUDGET_S", 1.0)
    conn = connection(settings_with(f"10.0.0.1:443:{SECRET}"))
    await conn.start(lambda first: asyncio.sleep(0))
    assert conn.running is True
    calls = len(built_client.calls)

    conn.client.connected = False  # обрыв со стороны сети
    for _ in range(200):
        await asyncio.sleep(0.01)
        if conn.running and len(built_client.calls) > calls:
            break
    await conn.stop()

    assert len(built_client.calls) > calls, "механизм обязан собрать клиент заново"
    assert conn.running is False, "но после stop — уже нет"


@pytest.mark.asyncio
async def test_watchdog_does_not_touch_a_live_connection(built_client, monkeypatch):
    """Живое соединение сторож не трогает: иначе переподключения каждые полминуты."""
    monkeypatch.setattr("agent_core.tg.connection.WATCHDOG_TICK_S", 0.01)
    conn = connection(settings_with(f"10.0.0.1:443:{SECRET}"))
    await conn.start(lambda first: asyncio.sleep(0))
    calls = len(built_client.calls)

    await asyncio.sleep(0.1)
    await conn.stop()

    assert len(built_client.calls) == calls
