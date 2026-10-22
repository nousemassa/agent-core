"""Схема МЕХАНИЗМА БОТА: состояние ходов, очередь доставки, согласия, история реплик, трассы.

Эти таблицы не знают предметной области: их применит любой продукт, который ведёт диалог и доставляет
исходящие. Продукт без такого механизма (например, однопроходный конвейер) этот компонент не накатывает.

Инварианты:
  * dedupe входящих и идемпотентность исходящих durable в PostgreSQL;
  * `outbox` фиксирует «отправка могла начаться» ДО сетевого вызова;
  * `turn_traces.chat_hash` — хеш, а не идентификатор человека.
"""
from __future__ import annotations

SCHEMA_CHECK_SQL = """
select
    (select count(*) from information_schema.tables
       where table_schema='public' and table_name='turn_state')
  + (select count(*) from information_schema.tables
       where table_schema='public' and table_name='turn_traces')
  + (select count(*) from information_schema.columns
       where table_schema='public' and table_name='turn_traces' and column_name='trace_id')
  + (select count(*) from information_schema.tables
       where table_schema='public' and table_name='outbox')
  + (case when (select count(*) from information_schema.columns
                  where table_schema='public' and table_name='outbox'
                    and column_name in ('attempts','next_attempt_at','last_error','lease_until','lease_owner')) = 5
          then 1 else 0 end)
  + (select count(*) from pg_indexes
       where schemaname='public' and indexname = 'ix_outbox_todo')
  + (select count(*) from information_schema.tables
       where table_schema='public' and table_name='dialog_messages')
  + (select count(*) from pg_indexes
       where schemaname='public' and indexname = 'ix_dialog_messages_chat')
  + (select count(*) from information_schema.tables
       where table_schema='public' and table_name='consumed_updates')
  + (select count(*) from information_schema.tables
       where table_schema='public' and table_name='send_state')
"""

SCHEMA_CHECK_EXPECTED = 10

SCHEMA_CHECK_HINT = (
    "turn_state, turn_traces (trace_id), outbox (5 колонок доставки + индекс), dialog_messages "
    "(+ индекс), consumed_updates, send_state"
)

DDL = """-- durable dedupe входящих (Redis — только кэш)
CREATE TABLE IF NOT EXISTS consumed_updates (
    platform           text NOT NULL,
    chat_id            text NOT NULL,
    external_update_id text NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, chat_id, external_update_id)
);

-- идемпотентность исходящих SEND/steer
CREATE TABLE IF NOT EXISTS send_state (
    client_msg_id text PRIMARY KEY,
    platform      text NOT NULL,
    chat_id       text NOT NULL,
    session_id    text NOT NULL,
    turn_id       text,
    status        text NOT NULL DEFAULT 'new',    -- new|sent|done|failed
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS consents (
    platform   text NOT NULL,
    chat_id    text NOT NULL,
    version    text NOT NULL,
    allow_ai   boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, chat_id)
);

CREATE TABLE IF NOT EXISTS conversations (
    platform        text NOT NULL,
    chat_id         text NOT NULL,
    session_id      text NOT NULL,
    seq             int  NOT NULL DEFAULT 0,
    step            text NOT NULL DEFAULT 'new',
    continuum       jsonb,
    last_inbound_at timestamptz,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, chat_id)
);
CREATE INDEX IF NOT EXISTS ix_conv_updated ON conversations (updated_at);

-- Последовательность ДО таблицы: `seq` задаёт порядок доставки, и `DEFAULT 0` его не задавал
-- (порядок брать было не из чего).
CREATE SEQUENCE IF NOT EXISTS outbox_seq;

CREATE TABLE IF NOT EXISTS outbox (
    id         bigserial PRIMARY KEY,
    kind       text NOT NULL,
    payload    jsonb NOT NULL,
    status     text NOT NULL DEFAULT 'new',      -- new|in_flight|sent|failed
    seq        bigint NOT NULL DEFAULT nextval('outbox_seq'),
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at    timestamptz
);

-- Колонки доставки добавляются отдельными ALTER: `CREATE TABLE IF NOT EXISTS` новых колонок не приносит.
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS attempts int NOT NULL DEFAULT 0;
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS next_attempt_at timestamptz;
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS last_error text;
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS lease_until timestamptz;
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS lease_owner text;
-- **Дефолт существующей колонки `ADD COLUMN IF NOT EXISTS` НЕ меняет** — а на живой базе `outbox` уже
-- создана со старым `seq DEFAULT 0`, из-за чего порядок доставки был бы «как получится». Поэтому дефолт
-- ставим явно и добиваем уже записанные строки.
ALTER TABLE outbox ALTER COLUMN seq SET DEFAULT nextval('outbox_seq');
UPDATE outbox SET seq = nextval('outbox_seq') WHERE seq = 0;
-- Индекс под выборку работы: берём самые старые `new`, у которых настало время попытки.
CREATE INDEX IF NOT EXISTS ix_outbox_todo ON outbox (status, next_attempt_at, seq);

-- Состояние хода диалога: переживает ли ХОД падение процесса — видно по таблице.
--
-- Lease, а не просто статус: воркеров может быть больше одного, и один ход должен брать ровно один из
-- них. `lease_until` + `SKIP LOCKED` дают это без внешнего оркестратора. Просроченный lease значит
-- «процесс, взявший ход, умер» — такой ход подхватывает sweeper.
CREATE TABLE IF NOT EXISTS turn_state (
    id          bigserial PRIMARY KEY,
    platform    text NOT NULL,
    chat_id     text NOT NULL,
    turn_key    text NOT NULL,                   -- ключ хода: один ход — одна строка
    status      text NOT NULL DEFAULT 'created', -- created|sent|awaiting|answered|failed
    lease_until timestamptz,
    lease_owner text,
    attempts    int NOT NULL DEFAULT 0,
    reply_text  text,
    error       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (platform, chat_id, turn_key)
);
CREATE INDEX IF NOT EXISTS ix_turn_state_lease ON turn_state (status, lease_until);

-- РЕПЛИКИ ДИАЛОГА (`own`-рантайм): что участники сказали друг другу, а не состояние хода.
-- ❗ Текст пишется ТОЛЬКО МАСКИРОВАННЫМ: история — это вход следующего запроса, значит она и есть
-- место, где открытый телефон утёк бы в третий сервис.
CREATE TABLE IF NOT EXISTS dialog_messages (
    id         bigserial PRIMARY KEY,
    platform   text NOT NULL,
    chat_id    text NOT NULL,
    turn_key   text NOT NULL,
    role       text NOT NULL CHECK (role IN ('user', 'assistant')),
    text       text NOT NULL,
    step       text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);
-- Окно читается «последние N по клиенту», поэтому индекс по (клиент, порядок вставки).
CREATE INDEX IF NOT EXISTS ix_dialog_messages_chat ON dialog_messages (platform, chat_id, id DESC);

-- Трассы ходов: ОДНА строка на клиентский ход — токены, стоимость, латентность и исход разбора.
-- `agent_called=false` — ход закрыт локально: строку всё равно пишем, иначе «$/диалог» считался бы
-- только по ходам с облаком и завышал цену сопровождения клиента.
CREATE TABLE IF NOT EXISTS turn_traces (
    id             bigserial PRIMARY KEY,
    created_at     timestamptz NOT NULL DEFAULT now(),
    day            date NOT NULL,
    platform       text NOT NULL DEFAULT 'tg',
    chat_hash      text NOT NULL DEFAULT '',
    turn_id        bigint,
    session_id     text NOT NULL DEFAULT '',
    step           text NOT NULL DEFAULT '',
    prompt_hash    text NOT NULL DEFAULT '',
    context_items  integer NOT NULL DEFAULT 0,
    agent_called   boolean NOT NULL DEFAULT false,
    outcome        text NOT NULL DEFAULT '',
    parser_verdict text NOT NULL DEFAULT '',
    model          text,
    provider       text,
    tokens_in      integer,
    tokens_out     integer,
    tokens_total   integer,
    cost_usd       numeric(12, 6),
    latency_ms     integer,
    -- Идентификатор трейса платформы (hex 32 знака). Пусто, когда трассировка выключена.
    trace_id       text NOT NULL DEFAULT ''
);
-- ALTER, а не только CREATE: на ЖИВОЙ базе `CREATE TABLE IF NOT EXISTS` колонок не добавляет.
ALTER TABLE turn_traces ADD COLUMN IF NOT EXISTS trace_id text NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS ix_turn_traces_day ON turn_traces (day);
CREATE INDEX IF NOT EXISTS ix_turn_traces_turn ON turn_traces (turn_id);
"""
