"""Спаны наших контуров: открытие, пометка ошибки и НЕЙТРАЛЬНЫЕ атрибуты.

**Что здесь, а что в `vendors/`.** Здесь — OTEL-API, общий ОБОИМ вендорам (открыть спан, пометить
ребро ошибкой) и хеш сессии. Самой ФОРМЫ спана здесь больше нет: она переехала в адаптеры
(`vendors/agenta.py`, `vendors/openlit.py`), потому что была записана литералами в четырёх местах двух
репозиториев и в двух диалектах одного вендора. Здесь остаётся СМЫСЛ: тип спана, имя узла, хеш чата.

**Зачем общий модуль.** Спаны нужны двум разным контурам: выпуску фоновых сводок и графу
напоминаний (`visit.tick`/`visit.flow` плюс десять узлов). Форма у них одна, и копий здесь нет: две
копии разошлись бы при первой же правке — ровно так, как разошлись бы две копии подписи кнопок.

**Приватность.** Корневой спан получает хеш сессии (`pii.chat_hash`), а не идентификатор человека: в
трейс ПДн не уходят.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from agent_core import vendors

log = logging.getLogger(__name__)


@contextmanager
def span(tracer, name: str, attributes: dict, *, record_exceptions: bool = True) -> Iterator[object]:
    """Открыть спан с атрибутами. Без трейсера — просто «ничего не делать».

    Падение внутри НЕ глотается: контекст обязан пропустить исключение наружу, иначе упавший шаг было
    бы не отличить от завершившегося. Когда трассировка выключена, `tracer` — пустышка (`ProxyTracer` из), поэтому ни одна точка вызова не проверяет «включено ли».

    `record_exceptions=False` — для мест, где статус решаем МЫ: у узлов графа напоминаний приостановка
    (`GraphInterrupt`, узел встал на прерывание) — это нормальный ход работы, а не сбой, и автоматическая
    пометка записала бы её ошибкой. Там же, где статус ставит `mark_error`, след исключения в спане
    становится дублем одной и той же беды.
    """
    if tracer is None:
        yield None
        return
    with tracer.start_as_current_span(
        name, record_exception=record_exceptions, set_status_on_exception=record_exceptions
    ) as current:
        for key, value in attributes.items():
            current.set_attribute(key, value)
        yield current


def mark_error(current, message: str, *, error_type: str = "") -> None:
    """Пометить СВОЁ ребро ошибкой: падение шага не должно выглядеть как падение всего контура.

    **Почему статус И `error.type`.** Стандарт OpenTelemetry («Recording Errors»)
    требует ДВУХ вещей: код статуса спана `Error` И атрибут `error.type` с низкокардинальным
    идентификатором ошибки (рекомендуется каноническое имя исключения). Мы ставили только статус —
    приёмник видел «упало», но не видел «чем», и различать отказы приходилось по свободному тексту
    `status_message`, который для группировки не годится.

    `error_type` передаётся ЯВНО, а не выводится из текста: вытаскивать тип из свободного сообщения
    значило бы гадать, а гадание на границе спана — это мусор в трейсе вместо признака. Где тип
    известен (а он известен везде, где у нас есть само исключение) — его и передают; где неизвестен,
    атрибут просто не ставится, и это честнее, чем подставить имя наугад.

    **Это общий слой, а не адаптер.** `error.type` — атрибут СТАНДАРТА OTEL, одинаковый для обоих
    вендоров (как сам статус и имя спана), поэтому он живёт рядом с `span()`, а не в `vendors/`:
    делить здесь нечего.
    """
    if current is None:
        return
    try:
        from opentelemetry.trace import Status, StatusCode

        current.set_status(Status(StatusCode.ERROR, message))
        if error_type:
            current.set_attribute("error.type", error_type)
    except Exception as error:  # noqa: BLE001 — ошибка разметки не роняет прогон
        log.warning("трейс: не удалось пометить спан ошибкой (%s: %s)", type(error).__name__, error)


def workflow_attributes(*, session_id: str = "") -> dict:
    """Атрибуты КОРНЕВОГО спана контура: тип `workflow` и (если известно) хеш сессии.

    Форму делает активный адаптер вендора (`vendors.active()`); здесь остаётся только СМЫСЛ — «это
    корень контура». Ссылки на объекты вендора (воркфлоу, ревизии ролей) сюда НЕ входят: у каждого
    контура они свои, и общий модуль не должен знать про `OTEL_*` конкретного контура.

    Пустая сессия не мешает: адаптер просто не поставит атрибут сессии, и мусорного пустого словаря в
    трейсе не появится.
    """
    return vendors.active().attributes(vendors.SpanShape(kind=vendors.WORKFLOW, session_id=session_id))


def session_id_for(chat_id: str) -> str:
    """Хеш чата вместо идентификатора человека.

    Одно место на оба контура: если бы хеш считался в каждом своём, однажды один из них отправил бы
    сам чат — и это заметили бы по чужому логу, а не по коду.
    """
    from agent_core.ids import chat_hash

    return chat_hash(str(chat_id)) if chat_id else ""


def node_attributes(*, name: str) -> dict:
    """Атрибуты узла пайплайна: тип `task` и имя шага.

    Тип здесь ОБЯЗАТЕЛЬНО объявляется, иначе «десять узлов графа» читались бы в трейсе как десять
    агентов: узел — не агент, и это различие делает адаптер.
    """
    return vendors.active().attributes(vendors.SpanShape(kind=vendors.NODE, step=name))
