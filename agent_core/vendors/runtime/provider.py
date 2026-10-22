"""Один вызов модели для СВОЕГО рантайма: тело запроса, ретраи, дедлайн и честная цена.

**Зачем свой клиент, а не клиент продукта.** Тот модуль — часть продукта: другой провайдер
по смыслу задачи, константа температуры, цикл инструментов и форма ответа команды. Диалогу нужен один
ход без инструментов, но с собственным бюджетом ответа и дедлайном, равным `wait_s` сайта. Копировать
его целиком значило бы второй источник правды; здесь взято ровно то, что нужно, и названо, чего нет.

**Что взято из проверенного опыта (`llm.py`):**
* `reasoning: {"enabled": false}` — РАССУЖДАЮЩАЯ модель уводит в `reasoning_tokens` весь бюджет, и
  `content` приходит пустым; настройкой это делать нельзя;
* тело собирается отдельной функцией, чтобы форму запроса можно было проверить тестом.

**Чего здесь НЕТ и почему:**
* `max_tokens` не дефолтится: он приходит из шапки файла агента (`prompt_spec`). Без него провайдер
  выбирает бюджет сам, и счёт за ход становится непознаваемым — а именно счёт мы и обязаны отдавать в
  `usage`.
* Ретраи не ретраят `400/401/403/404/422`: это не «не повезло», а неверный запрос или ключ, и повтор
  только сожжёт деньги.
* Дедлайн — ОДИН на весь ход (`wait_s` сайта минус запас): ретраи обязаны уложиться в него, иначе сайт
  уже вернул пустоту, а мы продолжаем платить за ответ, который никто не прочитает.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

#: Сколько попыток всего (первая + повторы) и как ждём между ними.
ATTEMPTS = 3
BACKOFF_S = (0.5, 1.5)
#: `Retry-After` уважаем, но не больше этого: иначе дедлайн хода съедает один ответ сервера.
RETRY_AFTER_CAP_S = 5.0
#: Коды, которые повторяем. Остальные 4xx — ошибка запроса, повтор её не исправит.
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
#: Запас на сеть и разбор: дедлайн хода = `wait_s` сайта минус это.
DEADLINE_RESERVE_S = 2.0
#: Сколько живёт простаивающее соединение в пуле клиента. Дольше — провайдер/прокси его всё равно
#: закроет, и мы получим «обрыв на чтении» уже после отправки запроса (то есть оплатив его).
KEEPALIVE_EXPIRY_S = 30.0
#: Что повторяем на уровне транспорта. ❗ `ConnectError` — не единственная «протухшая» ошибка:
#: переиспользованное соединение, закрытое другой стороной, даёт `RemoteProtocolError`/`ReadError`
#: (оба — `HTTPError`, но НЕ `ConnectError`). Без них первый же ход после простоя падал бы как отказ.
RETRY_TRANSPORT = (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError)


def make_client() -> httpx.AsyncClient:
    """Клиент провайдера. Один на рантайм, а не на ход: иначе каждый ход — новый TCP+TLS.

    `keepalive_expiry` — поле `httpx.Limits`, а НЕ аргумент `AsyncClient`: как аргумент он даёт `TypeError`.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=45.0, write=30.0, pool=10.0),
        limits=httpx.Limits(keepalive_expiry=KEEPALIVE_EXPIRY_S),
    )


class RuntimeUnavailable(RuntimeError):
    """Ход не исполнен: провайдер недоступен, ответил ошибкой или не уложился в дедлайн.

    ❗ Форма отказа повторяет платформенную: сетевые ошибки канального пути тоже БРОСАЮТ
    (`vendors/channels/agenta.py`), а таймаут — единственный случай, когда
    возвращается `done=False`. Вызывающий код уже написан под это, и менять форму
    «по-своему» значило бы чинить вызывающих под новую реализацию.
    """


class RuntimeTimeout(RuntimeUnavailable):
    """Не уложились в дедлайн хода. Отдельный класс, потому что ЗДЕСЬ форма другая.

    Платформа на таймауте НЕ бросает: она возвращает `done=False` (`vendors/channels/agenta.py`, ветка таймаута), и
    вызывающий подставляет честный текст «не ответил вовремя». Если бы own бросал на дедлайне, сайт
    ушёл бы в ветку ошибки вместо этой — то есть форма отказа разъехалась бы ровно там, где её
    сравнивают в замере паритета.
    """


@dataclass(frozen=True)
class ChatResult:
    """Ответ модели: текст и расход в платформенной форме."""

    text: str
    #: `{input, output, total, cost}` — ровно та форма, которую читает `store.usage_fields`.
    usage: dict[str, Any]
    attempts: int


def request_body(*, model: str, system: str, history: list[dict[str, str]], text: str,
                 temperature: float, max_tokens: int) -> dict[str, Any]:
    """Тело запроса. Отдельной функцией — чтобы форму можно было проверить тестом, не ходя в сеть."""
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    messages.extend(history)
    messages.append({"role": "user", "content": text})
    return {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        # См. докстринг модуля: рассуждения выключены жёстко, иначе `content` приходит пустым.
        "reasoning": {"enabled": False},
    }


def price_usd(*, usage: dict[str, Any], prices: tuple[float, float]) -> float | None:
    """Цена хода из токенов и НАШИХ цен РОЛИ. Незаданные цены ⇒ `None`, никогда не ноль.

    ❗ Цены приходят аргументом, а не читаются из настроек здесь: у ролей разные модели и тарифы, и
    модуль, знающий слово «dialog», молча считал бы ходы другой роли по тарифу диалога.

    Ноль был бы ложью: `store.cost_usd` суммирует `coalesce(sum, 0)`, поэтому ноль в `usage`
    неотличим от «ход ничего не стоил», и отчёт владельцу показал бы бесплатный ход.
    """
    price_in, price_out = float(prices[0] or 0.0), float(prices[1] or 0.0)
    if not price_in and not price_out:
        return None
    return (int(usage.get("input") or 0) * price_in + int(usage.get("output") or 0) * price_out) / 1000.0


def _usage(payload: dict[str, Any], prices: tuple[float, float]) -> dict[str, Any]:
    """`usage` провайдера → платформенная форма. Недостающее честно `None`, а не ноль."""
    raw = payload.get("usage") or {}
    tokens_in = raw.get("prompt_tokens")
    tokens_out = raw.get("completion_tokens")
    tokens_total = raw.get("total_tokens")
    usage: dict[str, Any] = {
        "input": int(tokens_in) if tokens_in is not None else None,
        "output": int(tokens_out) if tokens_out is not None else None,
        "total": int(tokens_total) if tokens_total is not None else None,
    }
    usage["cost"] = price_usd(usage=usage, prices=prices)
    return usage


async def chat(
    *,
    settings: Any,
    spec: Any,
    history: list[dict[str, str]],
    text: str,
    wait_s: float,
    prices: tuple[float, float],
    client: httpx.AsyncClient | None = None,
    sleep=asyncio.sleep,
) -> ChatResult:
    """Исполнить ход: один вызов модели с ретраями внутри дедлайна хода."""
    base_url, api_key, model = settings.chat_credentials()
    if not (base_url and api_key):
        raise RuntimeUnavailable(
            "провайдер не настроен: `chat_credentials()` пуст. Ход не исполнен и денег не потрачено."
        )
    body = request_body(
        model=model,
        system=spec.system_text,
        history=history,
        text=text,
        temperature=spec.temperature,
        max_tokens=spec.max_tokens,
    )
    own_client = client is None
    http = client or make_client()
    deadline = time.monotonic() + max(1.0, wait_s - DEADLINE_RESERVE_S)
    last_error = ""
    try:
        for attempt in range(1, ATTEMPTS + 1):
            left = deadline - time.monotonic()
            if left <= 0:
                raise RuntimeTimeout(
                    f"дедлайн хода истёк до ответа провайдера (попыток {attempt - 1}, последняя ошибка: "
                    f"{last_error or 'нет'}). Ответ не получен — но деньги за отправленные запросы потрачены."
                )
            try:
                response = await http.post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json=body,
                    timeout=httpx.Timeout(connect=10.0, read=min(45.0, left), write=30.0, pool=10.0),
                )
            except RETRY_TRANSPORT as error:
                last_error = f"{type(error).__name__}: {error}"
                if attempt < ATTEMPTS:
                    await sleep(min(BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)] + random.random() * 0.2,
                                     max(0.0, deadline - time.monotonic())))
                    continue
                #: Таймаут — это «не дождались», как у платформы (`done=False`); обрыв/отказ соединения —
                #: ошибка транспорта, и она бросается. Формы разные, и путать их нельзя.
                if isinstance(error, httpx.TimeoutException):
                    raise RuntimeTimeout(f"провайдер не ответил за дедлайн после {attempt} попыток: {last_error}") from error
                raise RuntimeUnavailable(f"провайдер недоступен после {attempt} попыток: {last_error}") from error
            except RuntimeError as error:
                # ❗ Клиент закрыли на остановке, а ход ещё летит: httpx бросает `RuntimeError`, который
                # НЕ `HTTPError` и мимо всех наших `except` уходил бы в 500 клиенту. Это отказ транспорта.
                if getattr(http, "is_closed", False):
                    raise RuntimeUnavailable(
                        "транспорт закрыт (остановка процесса) — ход не исполнен, деньги не потрачены"
                    ) from error
                raise
            except httpx.HTTPError as error:
                raise RuntimeUnavailable(f"транспорт провайдера отказал: {type(error).__name__}: {error}") from error

            if response.status_code != 200:
                last_error = f"HTTP {response.status_code}"
                if response.status_code in RETRY_STATUS and attempt < ATTEMPTS:
                    # `Retry-After` уважаем, но не больше капа: иначе один ответ сервера съест дедлайн.
                    retry_after = min(float(response.headers.get("Retry-After") or 0) or 0.0, RETRY_AFTER_CAP_S)
                    pause = retry_after or BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)]
                    log.warning("own: провайдер ответил %s, повтор %s/%s через %.1f с",
                                response.status_code, attempt, ATTEMPTS - 1, pause)
                    await sleep(min(pause + random.random() * 0.2, max(0.0, deadline - time.monotonic())))
                    continue
                # Не ретраится или попытки кончились: это отказ, а не пустой ответ. Пустой текст здесь
                # был бы неотличим от таймаута и спрятал бы квоту/ключ/битый запрос.
                raise RuntimeUnavailable(
                    f"провайдер ответил {response.status_code} (попыток {attempt}). "
                    f"Тело ответа не печатаем: в нём бывают ключи."
                )

            try:
                payload = response.json()
            except ValueError as error:
                raise RuntimeUnavailable(f"ответ провайдера не разобран как JSON: {error}") from error
            choices = payload.get("choices") or []
            message = (choices[0] or {}).get("message") or {} if choices else {}
            content = message.get("content") or ""
            if not content.strip():
                # Заплатили, но модели нечего сказать. Это НЕ отказ транспорта: возвращаем `done=False`
                # с заполненным `usage`, как платформа (там пустой ответ тоже не считается ответом).
                log.warning("own: пустой content от провайдера (usage=%s)", payload.get("usage"))
                return ChatResult(text="", usage=_usage(payload, prices), attempts=attempt)
            return ChatResult(text=content.strip(), usage=_usage(payload, prices), attempts=attempt)
    finally:
        if own_client:
            await http.aclose()
    raise RuntimeUnavailable("ход не исполнен: цикл попыток завершился без результата")
