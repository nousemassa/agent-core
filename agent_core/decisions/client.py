"""Клиент резидентного решателя `solver`.

Сервис отдаёт **калиброванную вероятность по вариантам**: `POST /v1/systemone` принимает `state`,
`question`, `options` и отвечает `answer`, `probabilities`, `top_probability`. Адрес, ключ и таймаут
приходят ПАРАМЕТРАМИ: ядро не знает, где стоит служба — это свойство развёртывания агента, а в бою
адреса могут быть разнесены по разным хостам.

**Почему отдельный модуль, а не вызов рядом с местом применения.** Про сеть и протокол должно быть
известно в одном месте: у вызывающих (лестница и инструмент проверяющего) остаются только решения.
Образец — `retrieval/embeddings.py`: клиент на один вызов, `httpx` импортируется внутри функции
(офлайн-тесты не тянут сеть), своя ошибка недоступности.

**Деградация обязательна и является штатным сценарием.** Сервис может быть не поднят или упасть во
время прогона; это не должно ломать ни ход диалога, ни работу команды — вызывающий ловит
`SolverUnavailable` и уходит на уровень 3 (облако). Молча вернуть «нет» вместо «не знаю» нельзя: это
подменило бы отказ решением и, например, скрыло бы новость от владельца.

**Ключ** приходит из настроек агента. Он не логируется и в отчёты не попадает.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


class SolverUnavailable(RuntimeError):
    """Решатель недоступен или ответил ошибкой. Штатный сценарий деградации на уровень 3."""


class SolverBadAnswer(RuntimeError):
    """Решатель ответил, но ответ не разбирается: нет `answer` или вероятностей.

    Отдельно от недоступности, потому что это разные случаи для вызывающего: недоступность —
    «решателя нет, работаем как раньше», битый ответ — «решатель есть, но доверять нечему».
    В обоих случаях лестница уходит на уровень 3, но причина в отчёте разная.
    """


@dataclass(frozen=True)
class Decision:
    """Ответ решателя: выбранный вариант и уверенность в нём."""

    answer: str
    top_probability: float
    probabilities: dict[str, float] = field(default_factory=dict)

    @property
    def confident(self) -> float:
        """Уверенность как число в [0..1] — то, что сравнивается с порогом."""
        return float(self.top_probability or 0.0)


async def decide(
    *,
    state: str,
    question: str,
    options: dict[str, str],
    base_url: str,
    api_key: str = "",
    timeout_s: float = 60.0,
    temperature: float | None = None,
) -> Decision:
    """Спросить решателя и вернуть его решение.

    `state` — то, что решатель оценивает (например, текст новости), `question` — вопрос по нему,
    `options` — словарь «вариант: пояснение»; пояснения важны, ими задаётся смысл варианта.

    Бросает `SolverUnavailable` при любой сетевой или серверной ошибке и `SolverBadAnswer`, если
    ответ не разбирается. Ничего не глотает: молчание вместо решения здесь хуже отказа.
    """
    if not state.strip() or not question.strip():
        raise SolverBadAnswer("нужны непустые state и question")
    if not options:
        raise SolverBadAnswer("нужен хотя бы один вариант ответа")

    import httpx

    payload: dict = {"state": state, "question": question, "options": options}
    if temperature is not None:
        payload["temperature"] = temperature
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.post(
                base_url.rstrip("/") + "/v1/systemone", json=payload, headers=headers
            )
            response.raise_for_status()
            body = response.json()
    except Exception as error:  # любой сбой сервиса = деградация, а не падение прогона
        raise SolverUnavailable(f"{type(error).__name__}: {error}") from error

    return parse_response(body)


def parse_response(body) -> Decision:
    """Разобрать ответ решателя. Отдельно от сети, чтобы проверялось без сервера."""
    if not isinstance(body, dict):
        raise SolverBadAnswer(f"ответ не объект: {type(body).__name__}")
    answer = str(body.get("answer") or "").strip()
    if not answer:
        raise SolverBadAnswer("в ответе нет поля answer")
    raw = body.get("probabilities")
    probabilities = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            try:
                probabilities[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
    top = body.get("top_probability")
    if top is None:
        # Сервис может не прислать top_probability — тогда берём уверенность из вероятностей,
        # а не подставляем ноль: ноль выглядел бы как «совсем не уверен» и всегда уходил бы в облако.
        top = probabilities.get(answer, 0.0)
    try:
        top_probability = float(top)
    except (TypeError, ValueError) as error:
        raise SolverBadAnswer(f"top_probability не число: {top!r}") from error
    return Decision(answer=answer, top_probability=top_probability, probabilities=probabilities)


def make_ask(base_url: str, *, api_key: str = "", timeout_s: float = 60.0):
    """Собрать функцию «спросить решателя» из явных значений.

    Одно место, где адрес, ключ и таймаут превращаются в вызов: этим пользуются и инструмент команды,
    и прогон, и гейт. Значения приходят из настроек агента — ядро адресов не знает.

    Пустой адрес — отказ при сборке: «удобное умолчание» здесь означало бы, что в бою запрос уйдёт
    не туда, а причина будет невидима.
    """
    if not base_url.strip():
        raise ValueError("решатель: адрес не задан — его объявляет агент в своих настройках")

    async def ask(state: str, question: str, options: dict) -> Decision:
        return await decide(
            state=state, question=question, options=options,
            base_url=base_url,
            api_key=(api_key or "").strip(),
            timeout_s=float(timeout_s),
        )

    return ask
