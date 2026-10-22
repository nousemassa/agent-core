#!/usr/bin/env python3
"""Проверка применимости схемы на ЧИСТОМ PostgreSQL — путь для CI.

Проверка идёт на **чистом** PostgreSQL, куда схема накатывается с нуля: иначе поломка в DDL видна
только в бою. Каждый компонент накатывается СВОИМ кодом (`apply_component`), а затем проверяется его
собственной проверкой (`Component.check_sql` и `expected`).

Использование:
    python3 scripts/ci-schema-check.py                       # компоненты общего слоя
    python3 scripts/ci-schema-check.py --components core
    python3 scripts/ci-schema-check.py --url postgresql://…

Коды возврата: 0 — схема применилась и совпала с ожиданием; 1 — нет.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

from agent_core.db.migrate import apply_component
from agent_core.db.registry import CORE_COMPONENTS, RUNTIME_COMPONENTS, Component
from agent_core.db.schema import normalize_dsn

#: Реестр этого гейта: общий слой и механизм бота. Продукты проверяют свои схемы своими реестрами.
COMPONENTS: tuple[Component, ...] = CORE_COMPONENTS + RUNTIME_COMPONENTS


async def main() -> int:
    parser = argparse.ArgumentParser(description="Проверить применимость схемы на чистой БД")
    parser.add_argument(
        "--url",
        default=os.environ.get("CI_DB_URL") or os.environ.get("DB_URL") or os.environ.get("BRIDGE_DB_URL"),
        help="DSN чистой базы (по умолчанию CI_DB_URL, затем DB_URL, затем BRIDGE_DB_URL)",
    )
    parser.add_argument("--components", default=",".join(c.name for c in COMPONENTS),
                        help="через запятую; по умолчанию все компоненты реестра")
    args = parser.parse_args()

    if not args.url:
        print("[ci-schema] FAIL: не задан DSN — нужен CI_DB_URL, DB_URL или --url", file=sys.stderr)
        return 1

    wanted = [name.strip() for name in args.components.split(",") if name.strip()]
    selected = tuple(c for c in COMPONENTS if c.name in wanted)
    unknown = set(wanted) - {c.name for c in selected}
    if unknown:
        print(f"[ci-schema] FAIL: неизвестные компоненты: {', '.join(sorted(unknown))}", file=sys.stderr)
        return 1

    #: База должна быть ПУСТОЙ: иначе проверка перестаёт быть проверкой «схема накатывается с нуля».
    #: Поэтому число таблиц до наката печатается — это и есть свидетельство чистоты для читающего лог.
    probe = await asyncpg.connect(normalize_dsn(args.url))
    try:
        before = await probe.fetchval(
            "select count(*) from information_schema.tables where table_schema='public'"
        )
        print(f"[ci-schema] таблиц в базе до наката: {before}")
    finally:
        await probe.close()

    for component in selected:
        try:
            await apply_component(component, args.url)
        except Exception as error:  # noqa: BLE001 — это и есть проверяемый исход
            print(
                f"[ci-schema] FAIL: компонент {component.name} не накатался на чистой базе — "
                f"{type(error).__name__}: {error}",
                file=sys.stderr,
            )
            return 1
        print(f"[ci-schema] {component.name}: накатан (ревизия {component.revision})")

    failed = False
    conn = await asyncpg.connect(normalize_dsn(args.url))
    try:
        total = await conn.fetchval(
            "select count(*) from information_schema.tables where table_schema='public'"
        )
        print(f"[ci-schema] таблиц после наката: {total}")
        for component in selected:
            got = int(await conn.fetchval(component.check_sql) or 0)
            if got != component.expected:
                failed = True
                print(
                    f"[ci-schema] FAIL: {component.name}: проверок {got}/{component.expected} — "
                    f"не хватает чего-то из: {component.hint}",
                    file=sys.stderr,
                )
            else:
                print(f"[ci-schema] {component.name}: проверок {got}/{component.expected}")
    finally:
        await conn.close()

    if failed:
        return 1
    print("[ci-schema] OK: схема применилась на чистой базе")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
