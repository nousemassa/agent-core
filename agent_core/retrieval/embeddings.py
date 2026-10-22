"""Клиент сервиса эмбеддингов (OpenAI-совместимый эндпоинт).

Адрес, ключ, имя модели и таймаут приходят ПАРАМЕТРАМИ: ядро не знает, где стоит служба — это
свойство развёртывания агента, и в бою адреса могут быть разнесены по разным хостам. Клиент на один
вызов, `httpx` импортируется внутри функции (чтобы офлайн-тесты не тянули сеть), таймаут — секунды.

**Деградация обязательна.** Сервис живёт вне репозитория и может быть не поднят.
Недоступность эмбеддингов не должна ломать ход диалога: вызывающий код ловит
`EmbeddingUnavailable` и продолжает без базы знаний, как если бы корпус был пуст.
Молча возвращать пустые векторы нельзя — это выглядело бы как «нашлось ноль», то есть
как содержательный ответ вместо сбоя, и замер `recall` считался бы по испорченным данным.
"""
from __future__ import annotations

import math

from agent_core.db.schema import EMBEDDING_DIM


class EmbeddingUnavailable(RuntimeError):
    """Сервис эмбеддингов недоступен или ответил ошибкой. Это штатный сценарий деградации."""


class EmbeddingDimensionMismatch(RuntimeError):
    """Модель отдала другую размерность, чем объявлено в схеме базы.

    Такое бывает у `embeddinggemma-2`: сборки с усечением до 512/256/128. Сравнивать
    векторы разной длины нельзя, поэтому расхождение — ошибка, а не мелочь.
    """


def _unit_norm(vector: list[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


async def embed_texts(
    texts: list[str],
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout_s: float = 15.0,
    expected_dim: int | None = EMBEDDING_DIM,
) -> list[list[float]]:
    """Посчитать эмбеддинги для списка текстов.

    Бросает `EmbeddingUnavailable` при любой сетевой или серверной ошибке и
    `EmbeddingDimensionMismatch`, если размерность не совпала со схемой.
    `expected_dim=None` отключает сверку — это нужно только проверке размерности,
    где ожидаемое значение ещё неизвестно.
    """
    if not texts:
        return []

    import httpx

    payload = {"model": model, "input": texts}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.post(base_url.rstrip("/") + "/embeddings", json=payload, headers=headers)
            response.raise_for_status()
            body = response.json()
    except Exception as error:  # любой сбой сервиса = деградация, а не падение хода
        raise EmbeddingUnavailable(f"{type(error).__name__}: {error}") from error

    items = body.get("data") if isinstance(body, dict) else None
    if not isinstance(items, list) or len(items) != len(texts):
        raise EmbeddingUnavailable(f"неожиданный ответ сервиса эмбеддингов: {str(body)[:200]}")

    vectors: list[list[float]] = []
    for index, item in enumerate(items):
        vector = item.get("embedding") if isinstance(item, dict) else None
        if not isinstance(vector, list) or not vector:
            raise EmbeddingUnavailable(f"в ответе нет вектора для фрагмента #{index}")
        if expected_dim is not None and len(vector) != expected_dim:
            raise EmbeddingDimensionMismatch(
                f"модель {model} отдала размерность {len(vector)}, а схема объявляет {expected_dim}"
            )
        vectors.append([float(value) for value in vector])
    return vectors


async def probe_dimension(
    *, base_url: str, api_key: str, model: str, timeout_s: float = 15.0
) -> int:
    """Узнать фактическую размерность модели одним пробным вызовом.

    Нужна ингесту: перед наливкой корпуса он сверяет факт со схемой, а не верит константе.
    """
    vectors = await embed_texts(
        ["проверка размерности"],
        base_url=base_url, api_key=api_key, model=model, timeout_s=timeout_s, expected_dim=None,
    )
    return len(vectors[0])
