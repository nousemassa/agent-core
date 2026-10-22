"""`Db.connect()`: пул, созданный до проверки схемы, не остаётся без владельца.

**Зачем отдельный файл.** `connect()` создаёт пул и только ПОТОМ проверяет схему. Если проверка падает,
`wait_ready` ретраит её (до 11 попыток в бюджете 45 с) — и каждая попытка оставляла свой пул: у
`asyncpg.Pool` нет `__del__`, самоочистки не будет. Здесь это проверяется счётчиком `close()`, а не
«на глаз»: двойник живёт на месте `create_pool`, наш код — настоящий.

**Политику ретраев не трогаем.** Ошибка схемы остаётся ВРЕМЕННОЙ намеренно: после ребута `migrate`
может ещё не доехать, и мгновенный выход первой попыткой — это регресс холодного старта,
который ловится `tests/test_startup.py`.
"""

from __future__ import annotations

import pytest

from agent_core.db import store


class _Conn:
    """Соединение двойника: `fetchval` отвечает «сколько таблиц нашлось»."""

    def __init__(self, found: int) -> None:
        self._found = found
        self.statements: list[str] = []

    async def fetchval(self, sql: str, *args):
        self.statements.append(sql)
        return self._found

    async def execute(self, sql: str, *args):
        self.statements.append(sql)


class _Acquire:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc_info) -> bool:
        return False


class _Pool:
    def __init__(self, found: int) -> None:
        self.conn = _Conn(found)
        self.closed = False

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def pools(monkeypatch):
    """Подменяем ТОЛЬКО создание пула: наш `connect`, проверка схемы и `close` — настоящие."""
    made: list[_Pool] = []

    def fake_create_pool(url: str, **kwargs):
        pool = _Pool(found=0)  # ноль таблиц: схема «не применена»
        made.append(pool)
        return pool

    async def fake_create_pool_async(*args, **kwargs):
        return fake_create_pool(*args, **kwargs)

    monkeypatch.setattr(store.asyncpg, "create_pool", fake_create_pool_async)
    return made


def test_pool_is_closed_when_schema_is_not_applied(pools):
    """Ошибка схемы: пул закрыт, ссылка снята — повторная попытка начнёт с чистого листа."""
    db = store.Db("postgresql://x/y", apply_ddl=False, expected_tables=("some_table", "settings"))

    import asyncio
    with pytest.raises(RuntimeError, match="схема не применена"):
        asyncio.run(db.connect())

    assert len(pools) == 1
    assert pools[0].closed is True
    assert db._pool is None


def test_no_pool_is_created_for_a_database_without_expectations(pools):
    """У векторной базы таблиц не называют: проверки нет, значит и закрывать нечего."""
    db = store.Db("postgresql://x/y", apply_ddl=False, expected_tables=None)

    import asyncio
    asyncio.run(db.connect())

    assert len(pools) == 1
    assert pools[0].closed is False
    assert db._pool is pools[0]
