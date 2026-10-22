"""Порт поиска и адаптер на pgvector.

**Порт.** `Retriever` — протокол с одним методом `search`. Он нужен, чтобы вызывающий код
(ход диалога) не знал ни про pgvector, ни про HTTP эмбеддингов: в тестах вместо адаптера
подставляется двойник, и это проверяется отдельно.

**Гибрид.** Векторный поиск и полнотекстовый объединяются **в одной базе** — ради этого
и выбран pgvector (одна СУБД вместо отдельного векторного сервиса). Объединение — по
reciprocal rank fusion: складываются `1/(k + ранг)` из двух списков. Это устойчивее
простого суммирования нормированных оценок: оценки вектора (косинус) и полнотекста
(`ts_rank`) живут в разных шкалах, и складывать их напрямую некорректно.

**Деградация.** Если сервис эмбеддингов недоступен, векторная половина невозможна, но
полнотекстовая работает — возвращаем её. Ход диалога не ломается ни при недоступном
эмбеддере, ни при пустом корпусе: в обоих случаях приходит пустой список, и это
осознанный результат, а не исключение.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from agent_core.retrieval.embeddings import EmbeddingUnavailable, embed_texts

log = logging.getLogger(__name__)

# Константа сглаживания из RRF. 60 — общепринятое значение: оно не даёт одному списку
# задавить другой только потому, что там оценки «крупнее».
RRF_K = 60


@dataclass(frozen=True)
class Chunk:
    """Найденный фрагмент базы знаний."""

    source: str
    source_id: str
    title: str
    text: str
    score: float


class Retriever(Protocol):
    async def search(self, query: str, *, limit: int) -> list[Chunk]: ...


def _vector_literal(vector: list[float]) -> str:
    """Вектор как строка для `::vector`: отдельный пакет `pgvector` не нужен."""
    return "[" + ",".join(f"{value:.7f}" for value in vector) + "]"


def _hybrid_sql(table: str, language: str) -> str:
    """Гибридный запрос для конкретной таблицы и языка полнотекста.

    Таблица и язык приходят от продукта: общий слой не знает, как называется его корпус и на каком
    языке он написан. Имя таблицы подставляется как идентификатор (значение приходит из кода, не из
    пользовательского ввода).
    """
    return f"""
WITH vector_hits AS (
    SELECT id,
           row_number() OVER (ORDER BY embedding <=> $1::vector) AS rank
      FROM {table}
     WHERE embedding IS NOT NULL
     ORDER BY embedding <=> $1::vector
     LIMIT $2
),
text_hits AS (
    SELECT id,
           row_number() OVER (
               ORDER BY ts_rank(to_tsvector('{language}', text),
                                plainto_tsquery('{language}', $3)) DESC
           ) AS rank
      FROM {table}
     WHERE to_tsvector('{language}', text) @@ plainto_tsquery('{language}', $3)
     LIMIT $2
),
merged AS (
    SELECT id, sum(weight) AS score FROM (
        SELECT id, 1.0 / ($4 + rank) AS weight FROM vector_hits
        UNION ALL
        SELECT id, 1.0 / ($4 + rank) AS weight FROM text_hits
    ) parts
    GROUP BY id
)
SELECT c.source, c.source_id, coalesce(c.title, '') AS title, c.text AS text, m.score AS score
  FROM merged m JOIN {table} c ON c.id = m.id
 ORDER BY m.score DESC
 LIMIT $2
"""


def _text_only_sql(table: str, language: str) -> str:
    """Полнотекстовая половина гибрида — работает и без сервиса эмбеддингов."""
    return f"""
SELECT source, source_id, coalesce(title, '') AS title, text AS text,
       ts_rank(to_tsvector('{language}', text), plainto_tsquery('{language}', $1)) AS score
  FROM {table}
 WHERE to_tsvector('{language}', text) @@ plainto_tsquery('{language}', $1)
 ORDER BY score DESC
 LIMIT $2
"""


class PgVectorRetriever:
    """Поиск по корпусу: вектор + полнотекст в одной базе."""

    def __init__(
        self,
        pool,
        *,
        table: str,
        fts_language: str,
        embed_url: str,
        embed_api_key: str,
        embed_model: str,
        embed_timeout_s: float = 15.0,
    ) -> None:
        if not table or not fts_language:
            raise ValueError("retrieval: нужны имя таблицы корпуса и язык полнотекста")
        self._pool = pool
        self._table = table
        self._fts_language = fts_language
        self._hybrid_sql = _hybrid_sql(table, fts_language)
        self._text_only_sql = _text_only_sql(table, fts_language)
        self._embed_url = embed_url
        self._embed_api_key = embed_api_key
        self._embed_model = embed_model
        self._embed_timeout_s = embed_timeout_s

    async def _vector(self, query: str) -> str | None:
        """Вектор запроса, либо `None`, если эмбеддер недоступен (тогда работает полнотекст)."""
        try:
            vectors = await embed_texts(
                [query],
                base_url=self._embed_url,
                api_key=self._embed_api_key,
                model=self._embed_model,
                timeout_s=self._embed_timeout_s,
            )
        except EmbeddingUnavailable as error:
            log.warning("retrieval: эмбеддинги недоступны (%s) — поиск только по полнотексту", error)
            return None
        return _vector_literal(vectors[0])

    async def search(self, query: str, *, limit: int = 4) -> list[Chunk]:
        query = (query or "").strip()
        if not query:
            return []

        vector = await self._vector(query)
        if vector is None:
            return await self._search_text_only(query, limit=limit)

        rows = await self._pool.fetch(self._hybrid_sql, vector, limit, query, RRF_K)
        return [
            Chunk(
                source=row["source"],
                source_id=row["source_id"],
                title=row["title"],
                text=row["text"],
                score=float(row["score"]),
            )
            for row in rows
        ]

    async def _search_text_only(self, query: str, *, limit: int) -> list[Chunk]:
        """Полнотекстовая половина гибрида — работает и без сервиса эмбеддингов."""
        rows = await self._pool.fetch(self._text_only_sql, query, limit)
        return [
            Chunk(
                source=row["source"],
                source_id=row["source_id"],
                title=row["title"],
                text=row["text"],
                score=float(row["score"]),
            )
            for row in rows
        ]
