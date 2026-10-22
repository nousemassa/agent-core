"""Векторная база общего слоя: DDL, проверка наката и нормализация адреса.

ОТДЕЛЬНАЯ база в отдельном контейнере (`pgvector/pgvector:pg17`): в платформенном `postgres:17`
расширения `vector` нет, а менять образ платформы нельзя. Поэтому DDL этой базы — своя константа.

Схема общего компонента живёт в `schema_core.py`; схемы продуктов — в их репозиториях.

Размерность колонки обязана совпадать с тем, что отдаёт модель эмбеддингов: её сверяет наливка корпуса
и падает, если модель отдаёт другую размерность (у `embeddinggemma-2` бывают усечённые сборки).
"""
from __future__ import annotations

EMBEDDING_DIM = 768

VECTOR_DDL = """
CREATE EXTENSION IF NOT EXISTS vector;

-- Чанки корпуса: `source` + `source_id` + `chunk_no` дают идемпотентную перезаливку.
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id          bigserial PRIMARY KEY,
    source      text NOT NULL,
    source_id   text NOT NULL,
    chunk_no    int  NOT NULL DEFAULT 0,
    title       text,
    text        text NOT NULL,
    embedding   vector(768),
    meta        jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, source_id, chunk_no)
);

-- Индекс HNSW с косинусной метрикой: поиск `ORDER BY embedding <=> $1 LIMIT k`.
CREATE INDEX IF NOT EXISTS ix_chunks_embedding_hnsw
    ON knowledge_chunks USING hnsw (embedding vector_cosine_ops);

-- Полнотекстовая половина гибрида: `ts_rank` в той же базе, без второго поисковика.
CREATE INDEX IF NOT EXISTS ix_chunks_text_fts
    ON knowledge_chunks USING gin (to_tsvector('russian', text));

"""

# Проверка векторной схемы — тем же приёмом, что и основной гейт: один запрос, одно число.
#   1) расширение `vector` установлено;
#   2) таблица чанков существует;
#   3) размерность колонки `embedding` равна EMBEDDING_DIM;
#   4) индекс HNSW с косинусной метрикой существует.
VECTOR_SCHEMA_CHECK_SQL = f"""
select
    (select count(*) from pg_extension where extname = 'vector')
  + (select count(*) from information_schema.tables
       where table_schema='public' and table_name='knowledge_chunks')
  + (select count(*) from pg_attribute
       where attrelid = 'public.knowledge_chunks'::regclass
         and attname = 'embedding'
         and format_type(atttypid, atttypmod) = 'vector({EMBEDDING_DIM})')
  + (select count(*) from pg_indexes
       where schemaname='public' and indexname = 'ix_chunks_embedding_hnsw')
"""

VECTOR_SCHEMA_CHECK_EXPECTED = 4

VECTOR_SCHEMA_CHECK_HINT = (
    "расширение vector, таблица knowledge_chunks, колонка vector(768), индекс HNSW"
)


def normalize_dsn(url: str) -> str:
    """DSN для `asyncpg`: он НЕ понимает префикс драйвера `+asyncpg` (это форма SQLAlchemy).

    ❗ `CoreSettings.db_url` приходит как `postgresql+asyncpg://…`, и `asyncpg.connect` падает
    `ClientConfigurationError: invalid DSN`. Пул `asyncpg.create_pool` получает значение уже приведённым
    ниже по стеку; накат подключается напрямую, поэтому приведение нужно ему самому.
    """
    return url.replace("+asyncpg", "").replace("postgresql+psycopg", "postgresql")
