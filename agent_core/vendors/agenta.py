"""Рендер нейтральной модели в ИЗМЕРЕННУЮ форму вендора Agenta.

**Форма не выдумана — она снята пробой** (проба наблюдаемости по живым трассам) и закреплена
golden-тестами. Четыре факта, из которых состоит этот файл, и каждый — замер, а не решение:

* **Объявлен тип ⇒ вложенно.** Спан с типом носит `ag.type.span`/`ag.type.node` ВЛОЖЕННЫМ словарём:
  платформа читает тип ТОЛЬКО оттуда (плоский ключ остаётся `TASK` — измерено пробой).
* **Тип не объявлен ⇒ плоско.** Спан хода объявляет ссылки плоскими ключами и типа не несёт вовсе
  (форма). Так он уходил и раньше, так уходит и теперь: форму НЕ меняет.
* **Ссылки — ВСЕГДА плоские** (`ag.references.*`): и у корня выпуска, и у роли-агента, и у спана хода.
  Это не непоследовательность, а измеренная форма, и она сохранена.
* **`ag.agent` — всегда вложенный** (внутри `ag`).

**Что здесь ИСПРАВЛЕНО осознанно.** Только `set_usage`: раньше расход писался скаляром и в
витрину метрик приёмника не попадал вообще — см. докстроку метода.
"""
from __future__ import annotations

from agent_core.vendors.neutral import AGENT, NODE, WORKFLOW, SpanShape, Usage
from agent_core.vendors.port import Receiver

#: Как нейтральный тип называется в форме вендора. `node` → `task` — НЕ опечатка: узлы пайплайна
#: платформа различает как `task`, иначе «три роли» читались бы как шесть агентов (`NON_ROLE_NODES`).
SPAN_NAME = {WORKFLOW: "workflow", NODE: "task", AGENT: "agent"}


def attributes(shape: SpanShape) -> dict:
    """Атрибуты спана в форме Agenta — ОДИН вход на все сегодняшние сайты.

    До эту форму собирали четыре места в двух репозиториях и в двух диалектах. Здесь она одна:
    правила выше (вложенность по объявленному типу, плоские ссылки) воспроизводят все прежние выходы
    БЕЗ отличий — это и держат golden-тесты.
    """
    if shape.kind is not None and shape.kind not in SPAN_NAME:
        raise ValueError(
            f"неизвестный тип спана {shape.kind!r}: известны {', '.join(sorted(SPAN_NAME))} "
            f"и «тип не объявлен» (None). Молчаливая подстановка типа соврала бы в трейсе."
        )

    attributes: dict[str, object] = {}
    if shape.kind is None:
        # Форма: тип НЕ объявлен, и приёмник оставляет свой (`TASK`). Поля идут плоскими ключами.
        if shape.session_id:
            attributes["ag.session.id"] = shape.session_id
        if shape.step:
            attributes["ag.data.step"] = shape.step
    else:
        name = SPAN_NAME[shape.kind]
        ag: dict[str, object] = {"type": {"span": name, "node": name}}
        if shape.session_id:
            ag["session"] = {"id": shape.session_id}
        if shape.agent_id:
            ag["agent"] = {"id": shape.agent_id}
        if shape.step:
            ag["data"] = {"step": shape.step}
        attributes["ag"] = ag

    # Ссылки — плоскими ключами и ВНЕ ветки типа: так у КОРНЯ выпуска (вложенный тип + плоские ссылки)
    # и так же у роли (вложенный тип + плоская ревизия). Один и тот же факт в обоих диалектах.
    if shape.workflow_id:
        attributes["ag.references.workflow.id"] = shape.workflow_id
    if shape.workflow_slug:
        attributes["ag.references.workflow.slug"] = shape.workflow_slug
    if shape.revision_id:
        attributes["ag.references.workflow_revision.id"] = shape.revision_id
    return attributes


def set_usage(current, usage: Usage) -> None:
    """Записать расход в той форме, которую приём Agenta ДЕЙСТВИТЕЛЬНО читает.

    **Дефект, найденный замером, и что здесь изменилось.** Расход писался СКАЛЯРОМ
    (`ag.metrics.tokens = 11`), а приём ждёт СЛОВАРЬ-разбивку: `AgVectorMetricEntryAttributes` —
    это `{cumulative, incremental}`. Нормализатор приёма скаляр уносил в
    `ag.unsupported.metrics.tokens`, слот оставался пустым `{}`, а свёртка (`cumulate_tokens`) читает
    именно `ag.metrics.tokens.incremental` ⇒ **числа ролей не попадали в витрину метрик никогда**
    (замер: у ролей `{}`, а `tool_calls` — в `unsupported` у 47 спанов).

    Теперь разбивка идёт в `incremental`, а `cumulative` считает САМ приём — включая пересчёт по всему
    трейсу после каждой партии (`worker.py`), поэтому корень выпуска получает сумму ролей.

    **Нулевую разбивку НЕ ставим.** Пустое значение приём трактует как «производитель сообщает ноль» и
    СБРАСЫВАЕТ уже накопленную `cumulative` (`_write_cumulative`).

    **`tool_calls` идёт в НАШ префикс и ставится ВСЕГДА — и при нуле тоже.** Слота метрики для него у
    приёмника нет вовсе (`AgMetricsAttributes` знает только `duration|errors|tokens|costs`), и раньше
    он уезжал в `ag.unsupported.metrics.tool_calls` — то есть в ВЕДРО того, чего приёмник не понял.
    Опираться на ведро нельзя: это боковой канал, который исчезнет, когда приёмник починят. Замер
    Замер: ключ ВНЕ `ag` приём ХРАНИТ как обычный атрибут — проба с `f1probe.marker` пережила
    приём и лежит в `attributes.f1probe.marker`. Поэтому наш ключ — `agenta.tool_calls`: он остаётся
    ровно тем, чем является, и СОВПАДАЕТ с тем, что пишет адаптер OpenLIT, — у обоих вендоров одно имя,
    а не два разных. Ноль здесь осмыслен: «инструменты не звались», а не отсутствие данных.
    """
    if current is None:
        return
    if usage.has_breakdown:
        current.set_attribute("ag.metrics.tokens", {"incremental": {
            "prompt": usage.prompt_tokens,
            "completion": usage.completion_tokens,
            "total": usage.total or (usage.prompt_tokens + usage.completion_tokens),
        }})
    elif usage.total:
        current.set_attribute("ag.metrics.tokens", {"incremental": {"total": usage.total}})
    current.set_attribute("agenta.tool_calls", usage.tool_calls)


def receiver(settings) -> Receiver:
    """Приём Agenta: адрес из `OTEL_ENDPOINT`, ключ — `OTEL_API_KEY` с фолбэком на `PLATFORM_API_KEY`.

    Пустой адрес — ШТАТНОЕ состояние («трассировки нет»), а не ошибка настройки: так было и раньше, и
    именно поэтому здесь нет предупреждения — журнал ругался бы на каждой установке по умолчанию.
    """
    endpoint = (getattr(settings, "otel_endpoint", "") or "").strip()
    if not endpoint:
        return Receiver()
    key = (getattr(settings, "otel_api_key", "") or "").strip() or (
        getattr(settings, "platform_api_key", "") or ""
    )
    return Receiver(endpoint, {"Authorization": f"ApiKey {key}"})


def resource_attributes(settings, service_name: str) -> dict:
    """Ресурс спанов для Agenta: ТОЛЬКО `service.name` — и это осознанно.

    Платформа атрибуты ресурса ИГНОРИРУЕТ: привязка к нашему агенту идёт атрибутами
    спана (`ag.references.*`). Поэтому здесь нет ни маркеров, ни окружения: положить их «на всякий
    случай» значило бы соврать в трейсе ради симметрии с другим вендором.
    """
    return {"service.name": service_name}
