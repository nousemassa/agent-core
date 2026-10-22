"""Схема ОБЩЕГО СОСТОЯНИЯ продукта: настройки, метрики дня и журнал действий.

Эти таблицы нужны каждому продукту и не связаны с механизмом диалога: их накатывает любой продукт.
Механизм бота (ходы, очередь, согласия, история реплик) объявлен отдельным компонентом в
`schema_runtime.py`, а предметные таблицы — в репозиториях продуктов.
"""
from __future__ import annotations

SCHEMA_CHECK_SQL = """
select
    (select count(*) from information_schema.tables
       where table_schema='public' and table_name='audit_log')
  + (select count(*) from information_schema.tables
       where table_schema='public' and table_name='settings')
  + (select count(*) from information_schema.tables
       where table_schema='public' and table_name='meta_metrics')
"""

SCHEMA_CHECK_EXPECTED = 3

SCHEMA_CHECK_HINT = "audit_log, settings, meta_metrics"

DDL = """CREATE TABLE IF NOT EXISTS settings (
    key   text PRIMARY KEY,
    value jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS meta_metrics (
    day    date NOT NULL,
    metric text NOT NULL,
    value  numeric NOT NULL,
    PRIMARY KEY (day, metric)
);

-- Журнал действий: кто и что сделал. Нейтральная замена типизированным журналам продуктов.
CREATE TABLE IF NOT EXISTS audit_log (
    id         bigserial PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    actor      text NOT NULL DEFAULT '',
    action     text NOT NULL,
    result     text,
    meta       jsonb
);
CREATE INDEX IF NOT EXISTS ix_audit_action ON audit_log (action, created_at DESC);
"""

DEFAULT_SETTINGS = {
    "bot_mode": "live",                 # live|paused|off
    "budget_month_usd": 25.0,
}
