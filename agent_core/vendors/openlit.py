"""Рендер нейтральной модели в OTEL GenAI (`gen_ai.*`) — приём OpenLIT.

**Об имени файла.** `vendors/openlit.py` — про ВЕНДОРА, а не про библиотеку: это рендер для приёмника
OpenLIT, и пакет `openlit` здесь не импортируется (его в зависимостях `core` нет вовсе, и `import
openlit` не встречается в нашем коде ни разу). Предлагалось имя `otel_gen_ai.py`;
НЕ принято: имя `vendors/openlit.py` выбрано владельцем, а затенения нет — импорты
абсолютные.

**Имена атрибутов взяты из СПЕКИ, а не выдуманы** (сверено по реестру
семантические конвенции GenAI): `gen_ai.operation.name`
(значения `invoke_workflow`/`invoke_agent`), `gen_ai.usage.input_tokens`/`gen_ai.usage.output_tokens`,
`gen_ai.agent.id`, `gen_ai.conversation.id`, ошибка — `error.type`.

❗ **Вся секция `gen_ai.*` сегодня в статусе Development** ⇒ имена могут измениться. Поэтому они
собраны в ОДНОМ месте (здесь), а не рассыпаны: смена спеки — правка одного файла.

❗ **Чего у `gen_ai` НЕТ и что мы делаем вместо.** Понятий «воркфлоу», «ревизия роли» и «ссылка на
объект платформы» стандарт не знает. Поэтому наши ссылки уходят под СВОИМ префиксом
(`agenta.references.*`), а не выбрасываются: молча потерять привязку к воркфлоу и ревизии — значит
получить «три безымянных агента» в новом приёмнике. Решение владельца: «`gen_ai.*` + свои ключи».

**Границы, названные честно:** (1) живой приём OpenLIT НЕ проверялся — стек поднимает владелец вручную,
форма доказана ТЕСТОМ, живая проба — отдельный шаг; (2) `mark_error` ставит ТОЛЬКО статус спана, хотя
стандарт советует ещё и `error.type` — не делаем здесь (названо, а не забыто).
"""
from __future__ import annotations

from agent_core.vendors.neutral import AGENT, NODE, WORKFLOW, SpanShape, Usage
from agent_core.vendors.port import Receiver

#: Наш префикс для того, чего в стандарте нет. Один на адаптер, чтобы «свои ключи» были узнаваемы.
PREFIX = "agenta"

#: Приём OTLP/HTTP OpenLIT по умолчанию. Владелец поднимает стек ВРУЧНУЮ, поэтому адрес можно задать
#: настройкой, а это — рабочий адрес из спецификации OpenLIT.
DEFAULT_ENDPOINT = "http://127.0.0.1:4318/v1/traces"

#: Нейтральный тип → имя операции в стандарте. У узла пайплайна (`prepare`/`gate`/`assemble`) операции
#: НЕТ: это не GenAI-действие, и выдумывать ему `invoke_*` значило бы соврать в семантике. Имя узла
#: видно по имени спана и по `agenta.step`.
OPERATION = {WORKFLOW: "invoke_workflow", AGENT: "invoke_agent", NODE: None}


def attributes(shape: SpanShape) -> dict:
    """Атрибуты спана в форме OpenTelemetry GenAI + наши ссылки под своим префиксом."""
    if shape.kind is not None and shape.kind not in OPERATION:
        raise ValueError(
            f"неизвестный тип спана {shape.kind!r}: известны {', '.join(sorted(OPERATION))} "
            f"и «тип не объявлен» (None)."
        )

    attributes: dict[str, object] = {}
    operation = OPERATION.get(shape.kind) if shape.kind is not None else None
    if operation:
        attributes["gen_ai.operation.name"] = operation
    if shape.kind == AGENT and (shape.agent_id or shape.revision_id):
        # Стандарт различает агента по id; у нас «агент» = ревизия инструкции роли, поэтому берём её.
        attributes["gen_ai.agent.id"] = shape.agent_id or shape.revision_id
    if shape.session_id:
        # СТАНДАРТНЫЙ ключ беседы: наш хеш сессии — ровно этот смысл (в трейс идёт хеш, не человек).
        attributes["gen_ai.conversation.id"] = shape.session_id
    if shape.step:
        attributes[f"{PREFIX}.step"] = shape.step

    # Ссылки, которых в стандарте нет: под своим префиксом, но НЕ потерянные.
    if shape.workflow_id:
        attributes[f"{PREFIX}.references.workflow_id"] = shape.workflow_id
    if shape.workflow_slug:
        attributes[f"{PREFIX}.references.workflow_slug"] = shape.workflow_slug
    if shape.revision_id:
        attributes[f"{PREFIX}.references.workflow_revision_id"] = shape.revision_id
    if shape.agent_id:
        attributes[f"{PREFIX}.references.agent_id"] = shape.agent_id
    return attributes


def set_usage(current, usage: Usage) -> None:
    """Расход в терминах стандарта: вход и выход раздельно.

    **Почему при отсутствии разбивки число всё равно не теряется.** Слота «только итог» стандарт не
    знает (`gen_ai.usage.*` — это вход и выход). Если провайдер разбивку не отдал, кладём итог под свой
    префикс: потерять известное число молча нельзя, а выдавать его за вход или за выход — соврать.

    Ноль не пишем вовсе: у стандарта ноль и «неизвестно» различаются, и пустое значение в приёмнике
    выглядело бы как осмысленный ноль (`Agenta` на этом теряет накопленную `cumulative` — урок тот же).

    `tool_calls` — исключение: он ставится ВСЕГДА (и при нуле). Слота для него в стандарте нет, ключ
    наш (`agenta.tool_calls`), и у Agenta он ровно такой же: одно нейтральное поле — один ключ у обоих
    вендоров. Ноль здесь осмыслен — «инструменты не звались».
    """
    if current is None:
        return
    if usage.has_breakdown:
        if usage.prompt_tokens:
            current.set_attribute("gen_ai.usage.input_tokens", usage.prompt_tokens)
        if usage.completion_tokens:
            current.set_attribute("gen_ai.usage.output_tokens", usage.completion_tokens)
    elif usage.total:
        current.set_attribute(f"{PREFIX}.usage.total_tokens", usage.total)
    # Ставится ВСЕГДА, как и в адаптере Agenta: у обоих вендоров нейтральное поле обязано давать ОДИН
    # и тот же ключ, иначе «одинаковый смысл» разъедется на первой же правке одного из них.
    current.set_attribute(f"{PREFIX}.tool_calls", usage.tool_calls)


def receiver(settings) -> Receiver:
    """Приём этого вендора: адрес (`TELEMETRY_OTLP_ENDPOINT`) и, если задан, свой ключ.

    ❗ Адрес НЕ берётся из `OTEL_ENDPOINT`: у вендоров разные приёмы, и общий ключ увёл бы спаны в
    Agenta, тогда как флаг обещает OpenLIT.
    """
    endpoint = ((getattr(settings, "telemetry_otlp_endpoint", "") or "").strip()
                or (getattr(settings, "openlit_otlp_endpoint", "") or "").strip() or DEFAULT_ENDPOINT)
    key = ((getattr(settings, "telemetry_otlp_api_key", "") or "").strip()
           or (getattr(settings, "openlit_otlp_api_key", "") or "").strip())
    return Receiver(endpoint, {"Authorization": f"Bearer {key}"} if key else {})


#: Маркер обнаружения OpenLIT: без него служба НЕ появляется в разделе «Agents».
#:
#: **Откуда требование.** Материализатор агентов вендора выбирает службы запросом
#: `lib/platform/agents/materialize.ts:200-232` и требует
#: `ResourceAttributes['telemetry.sdk.name'] = 'openlit'` (плюс исключения для CLI/eBPF-дистрибьюций).
#: У голого OTEL-SDK это значение `opentelemetry`, поэтому наши боты в «Agents → Services» не
#: показывались НИКОГДА: крон-материализатор каждую минуту писал `processed: 0`.
#:
#: **Почему маркер, а не их SDK.** SDK OpenLIT тянет ~50 пакетов авто-инструментаторов и начинает
#: писать СВОИ спаны поверх наших же (`langgraph`, `httpx`, LLM-клиенты) — то есть дубли и второй
#: источник правды о ходе. Данные в трейсах остаются нашими; здесь только ключ группировки приёмника.
#: Маркер обнаружения отличает второй приёмник от первого; форма спана от него не зависит.
DISCOVERY_SDK_NAME = "openlit"


def resource_attributes(settings, service_name: str) -> dict:
    """Ресурс спанов для OpenLIT: `service.name` (ключ группировки) + маркер обнаружения агента.

    `service.name` у этого приёмника — не украшение: по нему строится список служб и дерево агентов.
    Поэтому одна роль = одно имя (`agent-<роль>`), а не одно имя на процесс.
    """
    return {"service.name": service_name, "telemetry.sdk.name": DISCOVERY_SDK_NAME}
