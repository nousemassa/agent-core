"""Накат схемы: ОДНА функция на компонент, реестр приходит от продукта.

Движок не знает, какие бывают продукты: он получает `Component` (DDL, ревизию, проверку) и адрес базы.
Продукт собирает свой реестр и вызывает `apply_all`.

**Механизмы наката:**

1. **Замок.** `pg_advisory_lock` на время наката: два композа запускают свои `migrate` независимо, а
   `CREATE … IF NOT EXISTS` при одновременном выполнении даёт `duplicate key … pg_type_typname_nsp_index`
   и `tuple concurrently updated`. Идемпотентность не равна безопасности параллелизма.
2. **Таймаут блокировки.** `SET lock_timeout` на время наката: у `ALTER` берётся исключительная блокировка,
   а в базе `lock_timeout=0`, `statement_timeout=0` ⇒ зависший накат ждал бы вечно.
3. **Версия схемы с запретом понижения.** Таблица `schema_revision(component, revision, applied_at)`
   пишется через `ON CONFLICT` и **не понижается**: старый образ, поднятый позже нового, не должен
   перезаписать ревизию вниз.

**Чего ревизия НЕ гарантирует:** DDL остаётся СХОДЯЩИМСЯ (`IF NOT EXISTS`), ревизия растёт рукой ⇒ если в
код добавили `ALTER` и забыли поднять константу, ревизия покажет старое число, а колонки в базе не будет.
Проверка «все нужные таблицы есть» — отдельная (`Component.check_sql` и `expected`).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass

import asyncpg

from agent_core import startup
from agent_core.db.registry import CORE_COMPONENTS, RUNTIME_COMPONENTS, Component
from agent_core.db.schema import normalize_dsn

log = logging.getLogger(__name__)

#: Ключ advisory-замка: произвольное, но постоянное число. Все накаты берут ОДИН замок, поэтому два
#: композа не выполняют DDL одновременно.
LOCK_KEY = 0x4147454E  # "AGEN"

#: Сколько ждать освобождения блокировок таблиц. Меньше — чаще ложные отказы на живой базе, больше —
#: дольше простой служб, которые ждут накат.
LOCK_TIMEOUT = "15s"

#: Куда пишется версия. Таблица создаётся самим накатом (её нет в DDL намеренно: это метаданные наката,
#: а не предметная схема).
REVISION_DDL = """
CREATE TABLE IF NOT EXISTS schema_revision (
    component   text PRIMARY KEY,
    revision    integer NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
"""

#: Запись версии БЕЗ понижения: `WHERE excluded.revision > schema_revision.revision` пропускает только рост.
REVISION_UPSERT = """
INSERT INTO schema_revision(component, revision) VALUES($1, $2)
ON CONFLICT (component) DO UPDATE SET revision = EXCLUDED.revision, applied_at = now()
WHERE EXCLUDED.revision > schema_revision.revision
"""

#: Перенос ревизии с прежнего имени: значение берётся как максимум, чтобы не понизить уже записанное.
REVISION_RENAME = """
INSERT INTO schema_revision(component, revision, applied_at)
SELECT $2, revision, applied_at FROM schema_revision WHERE component = $1
ON CONFLICT (component) DO UPDATE
   SET revision = GREATEST(schema_revision.revision, EXCLUDED.revision),
       applied_at = GREATEST(schema_revision.applied_at, EXCLUDED.applied_at)
"""


@dataclass(frozen=True)
class Outcome:
    """Что произошло с компонентом: применён или пропущен, и почему."""

    component: str
    applied: bool
    note: str = ""


async def _rename_legacy(conn: asyncpg.Connection, component: Component) -> None:
    """Перенести ревизию с прежнего имени компонента и оставить зеркало для старого образа.

    Старый образ продолжает писать прежнее имя при каждом старте, поэтому перенос повторяется на каждом
    накате, а не «один раз». Зеркало позволяет старому образу увидеть ревизию и не откатить её вниз.
    """
    for legacy in component.legacy_names:
        await conn.execute(REVISION_RENAME, legacy, component.name)
        await conn.execute(REVISION_UPSERT, legacy, component.revision)


async def apply_on_connection(conn: asyncpg.Connection, component: Component) -> None:
    """Накат одного компонента на уже открытом соединении (замок берёт вызывающий)."""
    await conn.execute(REVISION_DDL)
    await _rename_legacy(conn, component)
    await conn.execute(component.ddl)
    for key, value in component.settings.items():
        await conn.execute(
            "INSERT INTO settings(key, value) VALUES($1,$2::jsonb) ON CONFLICT (key) DO NOTHING",
            key,
            json.dumps(value),
        )
    await conn.execute(REVISION_UPSERT, component.name, component.revision)


def _url_for(component: Component, *, db_url: str, vector_url: str) -> str:
    if component.url_key == "vectors":
        if not vector_url:
            raise ValueError(f"компоненту {component.name!r} нужен адрес векторной базы, а он не задан")
        return vector_url
    if not db_url:
        raise ValueError(f"компоненту {component.name!r} нужен адрес общей базы, а он не задан")
    return db_url


async def apply_component(component: Component, url: str) -> Outcome:
    """Накатить ОДИН компонент: соединение, таймаут блокировок, замок, накат."""
    conn = await asyncpg.connect(normalize_dsn(url))
    try:
        await conn.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
        await conn.execute("SELECT pg_advisory_lock($1)", LOCK_KEY)
        try:
            await apply_on_connection(conn, component)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_KEY)
    finally:
        await conn.close()
    return Outcome(component=component.name, applied=True)


async def apply_all(components: tuple[Component, ...], *, db_url: str = "",
                    vector_url: str = "") -> list[Outcome]:
    """Накатить компоненты по порядку. Порядок задаёт продукт: общий слой идёт первым."""
    outcomes: list[Outcome] = []
    for component in components:
        url = _url_for(component, db_url=db_url, vector_url=vector_url)
        outcomes.append(await apply_component(component, url))
    return outcomes


async def check_component(component: Component, url: str) -> tuple[int, int]:
    """Накатить на ЧИСТУЮ базу и вернуть (сделано проверок, ожидалось)."""
    await apply_component(component, url)
    conn = await asyncpg.connect(normalize_dsn(url))
    try:
        got = int(await conn.fetchval(component.check_sql) or 0)
    finally:
        await conn.close()
    return got, component.expected


async def _main(components: tuple[Component, ...], argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Накат схемы")
    parser.add_argument("--components", default=",".join(c.name for c in components),
                        help="через запятую; по умолчанию все компоненты реестра")
    parser.add_argument("--db-url", default="", help="DSN основной базы (иначе DB_URL, затем BRIDGE_DB_URL)")
    parser.add_argument("--vector-url", default="", help="DSN векторной базы (иначе VECTOR_DB_URL)")
    parser.add_argument("--wait", type=float, default=0.0, help="ждать готовности базы, секунд")
    args = parser.parse_args(argv)

    import os
    db_url = args.db_url or os.environ.get("DB_URL") or os.environ.get("BRIDGE_DB_URL", "")
    vector_url = args.vector_url or os.environ.get("VECTOR_DB_URL", "")
    wanted = [name.strip() for name in args.components.split(",") if name.strip()]
    selected = tuple(c for c in components if c.name in wanted)
    unknown = set(wanted) - {c.name for c in selected}
    if unknown:
        print(f"[migrate] неизвестные компоненты: {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2

    if args.wait:
        for component in selected:
            url = _url_for(component, db_url=db_url, vector_url=vector_url)
            await startup.wait_ready(
                lambda u=url: _probe(u), what=f"БД ({component.name})", budget_s=args.wait)

    for component in selected:
        url = _url_for(component, db_url=db_url, vector_url=vector_url)
        outcome = await apply_component(component, url)
        print(f"[migrate] {outcome.component}: накатано (ревизия {component.revision}){outcome.note}")
    return 0


async def _probe(url: str) -> None:
    conn = await asyncpg.connect(normalize_dsn(url))
    await conn.close()


def cli(components: tuple[Component, ...], argv: list[str] | None = None) -> int:
    """CLI продукта: печатает и накатывает переданный реестр."""
    return asyncio.run(_main(components, argv))


def main(argv: list[str] | None = None) -> int:
    """CLI общего слоя: его собственные компоненты — `core` и `runtime`."""
    return cli(CORE_COMPONENTS + RUNTIME_COMPONENTS, argv)


if __name__ == "__main__":
    raise SystemExit(main())
