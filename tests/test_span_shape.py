"""Форма спана у двух вендоров: «золотые» образцы, снятые ЗАМЕРОМ, а не выдуманные.

**Зачем этот файл.** Форма спана вынесена из четырёх мест в адаптеры вендора. Перевод, который
нечем проверить, может изменить форму МОЛЧА — а форма трейса читается ПРИЁМНИКОМ, и «почти такая же»
там означает «сломано». Поэтому образцы сняты ВЫПОЛНЕНИЕМ прежних функций ДО рефакторинга: любой
сдвиг формы — падение теста, а не сюрприз в живом трейсе.

**Что именно закреплено.** Два диалекта ОДНОГО вендора, которые раньше жили в разных файлах:
объявленный тип спана ⇒ поля ВНУТРИ `ag`; тип не объявлен ⇒ плоские ключи; ссылки — всегда плоские;
`ag.agent` — всегда вложенный. Плюс рендер второго вендора (`gen_ai.*` + наши ключи) — он пока доказан
ТОЛЬКО здесь: живой приём OpenLIT поднимает владелец вручную, а это отдельный шаг.

**Почему тесты не ходят в сеть.** Ни настроек, ни трейсера, ни экспортёра: `attributes()` — чистая
функция от нейтрального описания, и проверять в ней больше нечего.
"""
from __future__ import annotations

import pytest

from agent_core import telemetry, vendors
from agent_core.vendors import agenta, neutral, openlit

telemetry.register_roles("dialog", "builder", "reminder", "digest")


@pytest.fixture(autouse=True)
def _vendor_back_to_default():
    """После каждого теста — вендор по умолчанию.

    `select()` меняет состояние МОДУЛЯ, а не аргумент: без сброса один тест красил бы следующий, и
    падение «только вместе» не указывает на виновника. Тот же приём, каким соседние деревья изолируют
    подмену настроек.
    """
    yield
    vendors.select(vendors.AGENTA)


# --- адаптер Agenta: восемь образцов, снятых замером -------------------------------------------

def test_agenta_root_without_references_is_nested_only():
    """Корень контура: тип объявлен, поэтому поля идут ВЛОЖЕННО внутри `ag`, ссылок нет."""
    assert agenta.attributes(neutral.SpanShape(kind=neutral.WORKFLOW, session_id="SID")) == {
        "ag": {"session": {"id": "SID"}, "type": {"node": "workflow", "span": "workflow"}},
    }


def test_agenta_root_without_session_has_no_empty_session():
    """Пустая сессия не оставляет мусорного пустого словаря: приём складывает такое в `unsupported`."""
    assert agenta.attributes(neutral.SpanShape(kind=neutral.WORKFLOW, session_id="")) == {
        "ag": {"type": {"node": "workflow", "span": "workflow"}},
    }


def test_agenta_node_is_a_task_not_an_agent():
    """Узел пайплайна — `task`: иначе «десять узлов» читались бы как десять агентов."""
    assert agenta.attributes(neutral.SpanShape(kind=neutral.NODE, step="prepare")) == {
        "ag": {"data": {"step": "prepare"}, "type": {"node": "task", "span": "task"}},
    }


def test_agenta_references_only_dialect_is_flat():
    """Форма спана хода: тип НЕ объявляется, поэтому ссылки, сессия и шаг идут ПЛОСКИМИ ключами.

    Это не «недоделка», а измеренная форма: так спан хода уходил с самого начала, и рефакторинг её
    сохранил (смена типа — смена формы трейса, это отдельное решение).
    """
    assert agenta.attributes(neutral.SpanShape(
        session_id="HASH", workflow_id="wf-1", workflow_slug="agent-dialog", step="builder",
    )) == {
        "ag.data.step": "builder",
        "ag.references.workflow.id": "wf-1",
        "ag.references.workflow.slug": "agent-dialog",
        "ag.session.id": "HASH",
    }


def test_agenta_release_root_mixes_nested_type_with_flat_references():
    """Корень выпуска: вложенный тип + сессия + агент И плоские ссылки в ОДНОМ словаре.

    Именно здесь живёт источник путаницы: «ссылки плоские, а тип вложенный» — не непоследовательность,
    а измеренная форма, и она сохранена байт-в-байт.
    """
    assert agenta.attributes(neutral.SpanShape(
        kind=neutral.WORKFLOW, session_id="HASH", agent_id="agent-1",
        workflow_id="wf-1", workflow_slug="agent-digest",
    )) == {
        "ag": {"agent": {"id": "agent-1"}, "session": {"id": "HASH"},
               "type": {"node": "workflow", "span": "workflow"}},
        "ag.references.workflow.id": "wf-1",
        "ag.references.workflow.slug": "agent-digest",
    }


def test_agenta_role_carries_the_revision_as_a_flat_reference():
    """Роль несёт ссылку на РЕВИЗИЮ инструкции: именно она делает «своя ревизия у каждой роли» фактом."""
    assert agenta.attributes(neutral.SpanShape(kind=neutral.AGENT, revision_id="rev-a")) == {
        "ag": {"type": {"node": "agent", "span": "agent"}},
        "ag.references.workflow_revision.id": "rev-a",
    }
    assert agenta.attributes(neutral.SpanShape(kind=neutral.AGENT, revision_id="")) == {
        "ag": {"type": {"node": "agent", "span": "agent"}},
    }


def test_unknown_kind_is_refused_loudly():
    """Неизвестный тип — ОТКАЗ, а не молчаливая подстановка: подставленный тип соврал бы в трейсе."""
    with pytest.raises(ValueError, match="неизвестный тип спана"):
        agenta.attributes(neutral.SpanShape(kind="agent-of-chaos"))


# --- адаптер Agenta: расход --------------------------------------------------------------------

class _Span:
    """Двойник спана: запоминает, что на него поставили."""

    def __init__(self) -> None:
        self.attributes: dict = {}

    def set_attribute(self, key, value) -> None:
        self.attributes[key] = value


def test_usage_split_goes_to_the_breakdown_the_receiver_reads():
    """Разбивка идёт в `incremental`: раньше расход писался СКАЛЯРОМ и в витрину метрик не попадал."""
    span = _Span()
    agenta.set_usage(span, neutral.Usage(total=15, prompt_tokens=10, completion_tokens=5))

    assert span.attributes["ag.metrics.tokens"] == {
        "incremental": {"prompt": 10, "completion": 5, "total": 15},
    }
    assert span.attributes["agenta.tool_calls"] == 0, "ноль вызовов — факт, а не отсутствие данных"


def test_usage_without_split_still_reports_the_total():
    """Знает только итог — итог и уходит: потерять известное число хуже, чем не иметь разбивки."""
    span = _Span()
    agenta.set_usage(span, neutral.Usage(total=11))

    assert span.attributes["ag.metrics.tokens"] == {"incremental": {"total": 11}}


def test_zero_usage_sets_no_token_attribute_but_sets_tool_calls():
    """Нулевую разбивку НЕ ставим: приём трактует пустое значение как ноль и СБРАСЫВАЕТ `cumulative`.

    А `tool_calls` ставится и при нуле — слот метрики для него у приёмника отсутствует, ключ наш.
    """
    span = _Span()
    agenta.set_usage(span, neutral.Usage(total=0, tool_calls=0))

    assert "ag.metrics.tokens" not in span.attributes, "ноль стёр бы уже накопленную `cumulative`"
    assert span.attributes["agenta.tool_calls"] == 0


def test_usage_on_a_missing_span_does_nothing():
    """Трейсера нет — спан `None`: вызывающий об этом не думает, предохранитель живёт в адаптере."""
    agenta.set_usage(None, neutral.Usage(total=5))     # не должно бросить


# --- адаптер OpenLIT: имена из СПЕКИ, а не выдуманные -------------------------------------------

def test_openlit_uses_standard_operation_names():
    """`gen_ai.operation.name`: `invoke_workflow` у корня, `invoke_agent` у роли — значения из спеки."""
    assert openlit.attributes(neutral.SpanShape(
        kind=neutral.WORKFLOW, session_id="HASH", workflow_id="wf-1", workflow_slug="agent-digest",
    )) == {
        "agenta.references.workflow_id": "wf-1",
        "agenta.references.workflow_slug": "agent-digest",
        "gen_ai.conversation.id": "HASH",
        "gen_ai.operation.name": "invoke_workflow",
    }
    assert openlit.attributes(neutral.SpanShape(kind=neutral.AGENT, revision_id="rev-a")) == {
        "agenta.references.workflow_revision_id": "rev-a",
        "gen_ai.agent.id": "rev-a",
        "gen_ai.operation.name": "invoke_agent",
    }


def test_openlit_gives_a_node_no_invented_operation():
    """У узла пайплайна операции в стандарте НЕТ: выдумать `invoke_*` значило бы соврать в семантике."""
    assert openlit.attributes(neutral.SpanShape(kind=neutral.NODE, step="prepare")) == {
        "agenta.step": "prepare",
    }


def test_openlit_keeps_what_the_standard_does_not_have():
    """Чего у `gen_ai` нет (воркфлоу, ревизия) — уходит под НАШИМ префиксом, а не выбрасывается.

    Иначе «три агента» в новом приёмнике стали бы безымянными: потерять привязку молча нельзя.
    """
    rendered = openlit.attributes(neutral.SpanShape(
        kind=neutral.AGENT, agent_id="ag-1", revision_id="rev-a", workflow_id="wf-1", step="author",
    ))

    assert rendered["agenta.references.agent_id"] == "ag-1"
    assert rendered["agenta.references.workflow_revision_id"] == "rev-a"
    assert rendered["agenta.references.workflow_id"] == "wf-1"
    assert rendered["agenta.step"] == "author"


def test_openlit_usage_uses_standard_token_keys_and_our_total_fallback():
    """Вход/выход — стандартные ключи; итог без разбивки — наш ключ (в стандарте его нет)."""
    span = _Span()
    openlit.set_usage(span, neutral.Usage(total=15, prompt_tokens=10, completion_tokens=5))
    assert span.attributes == {
        "gen_ai.usage.input_tokens": 10,
        "gen_ai.usage.output_tokens": 5,
        "agenta.tool_calls": 0,
    }

    only_total = _Span()
    openlit.set_usage(only_total, neutral.Usage(total=11))
    assert only_total.attributes["agenta.usage.total_tokens"] == 11
    assert "gen_ai.usage.input_tokens" not in only_total.attributes, \
        "выдать итог за вход или за выход — соврать"


# --- выбор вендора: одна ручка, отказ вместо молчаливого отката -----------------------------------

def test_vendor_defaults_to_agenta_and_active_follows_select():
    """По умолчанию поведение прежнее; переключение — по значению `PLATFORM_VENDOR`."""
    assert vendors.active() is agenta, "по умолчанию вендор — agenta, переключения нет"

    vendors.select("openlit")
    assert vendors.active() is openlit

    vendors.select("  OpenLIT  ")
    assert vendors.active() is openlit, "регистр и пробелы — формат, а не смысл"


def test_empty_vendor_value_means_the_default_not_a_crash():
    """Пустое значение в `.env` — это «не задано»: служба не должна падать от пустого ключа."""
    vendors.select("")

    assert vendors.active() is agenta


def test_unknown_vendor_is_refused_loudly():
    """Опечатка в вендоре — ОТКАЗ: молчаливый откат к `agenta` сделал бы спаны «не там» неотличимыми.

    `own` отвергается осознанно: это ДРУГАЯ ось (`AGENT_RUNTIME`), и адаптера рантайма у OpenLIT нет.
    """
    for wrong in ("open-lit", "own", "nonsense"):
        with pytest.raises(ValueError, match="неизвестный вендор платформы"):
            vendors.select(wrong)


# --- ось НАБЛЮДАЕМОСТИ (`TELEMETRY_VENDOR`): отдельно от рантайма -----------------------------
#
# **Зачем ось.** Пока диалог исполняет Agenta, спаны при этом должны уметь идти в OpenLIT: иначе второй
# вендор остаётся без данных, а «посмотреть его UI на своих трейсах» невозможно. Ось читает ОДИН вход —
# `setup_telemetry`, поэтому проверяем именно его, а не `vendors.select` напрямую.

def test_telemetry_vendor_is_independent_of_platform_vendor():
    """Рантайм Agenta + спаны в OpenLIT — разрешённая комбинация, и она действительно переключает приём."""
    from agent_core import telemetry
    from agent_core.config import CoreSettings


    settings = CoreSettings(platform_vendor="agenta", telemetry_vendor="openlit",
                        telemetry_otlp_endpoint="http://127.0.0.1:4318/v1/traces")
    telemetry.setup_telemetry(settings, agent="dialog")

    assert vendors.active() is openlit, "форма спана — по оси наблюдаемости"
    assert vendors.active().receiver(settings).endpoint.endswith(":4318/v1/traces"), \
        "приём обязан быть OpenLIT, иначе ось — украшение"


def test_empty_telemetry_vendor_keeps_previous_behaviour():
    """Пустая ось = «не задано» ⇒ ровно как раньше: вендор берётся из `PLATFORM_VENDOR`.

    Это регресс-защита: включение оси не должно менять поведение ни у кого, кто её не задавал.
    """
    from agent_core import telemetry
    from agent_core.config import CoreSettings

    telemetry.setup_telemetry(CoreSettings(platform_vendor="agenta", telemetry_vendor=""), agent="dialog")
    assert vendors.active() is agenta

    telemetry.setup_telemetry(CoreSettings(platform_vendor="openlit", telemetry_vendor="",
                                       telemetry_otlp_endpoint="http://127.0.0.1:4318/v1/traces"),
                          agent="dialog")
    assert vendors.active() is openlit


# --- РОЛИ АГЕНТОВ: одна роль = одна служба в приёмнике -----------------------------------------
#
# **Зачем.** Владелец хотел видеть в UI не «одну службу сервиса», а отдельных агентов по ролям.
# `service.name` — свойство ПРОВАЙДЕРА, поэтому «роль» в коде обязана доехать до
# ресурса провайдера. Проверяем ровно стык «роль → атрибуты ресурса»: если он сломается, в UI снова
# будет одна безымянная служба, и это опять заметит владелец, а не тест.

def test_service_name_is_built_from_role_and_refuses_unknown():
    """Имя службы — по ФУНКЦИИ роли; опечатка в роли — отказ, а не пятый «агент» в приёмнике."""
    from agent_core import telemetry


    assert telemetry.service_name("dialog") == "agent-dialog"
    assert telemetry.service_name("builder") == "agent-builder"
    assert telemetry.service_name("reminder") == "agent-reminder"
    assert telemetry.service_name("digest") == "agent-digest"

    with pytest.raises(ValueError, match="неизвестная роль агента"):
        telemetry.service_name("lisitng")


def test_each_role_gets_its_own_resource_at_openlit():
    """OpenLIT: у каждой роли СВОЁ `service.name` и маркер обнаружения в разделе Agents."""
    from agent_core import telemetry
    from agent_core.config import CoreSettings

    settings = CoreSettings(platform_vendor="agenta", telemetry_vendor="openlit",
                        telemetry_otlp_endpoint="http://127.0.0.1:4318/v1/traces")
    vendors.select(vendors.OPENLIT)

    dialog = telemetry.tracer_resource(settings, "dialog")
    digest = telemetry.tracer_resource(settings, "digest")

    assert dialog["service.name"] == "agent-dialog"
    assert digest["service.name"] == "agent-digest"
    assert dialog["service.name"] != digest["service.name"], "одна служба на все роли — это то, что чинили"
    assert dialog["telemetry.sdk.name"] == openlit.DISCOVERY_SDK_NAME, \
        "без маркера служба не появится в Agents → Services (materialize.ts требует именно его)"


def test_at_agenta_resource_carries_no_discovery_marker():
    """У Agenta ресурс игнорируется: маркера нет, и придумывать его «для симметрии» нельзя."""
    from agent_core import telemetry
    from agent_core.config import CoreSettings

    settings = CoreSettings(platform_vendor="agenta", telemetry_vendor="")
    vendors.select(vendors.AGENTA)

    resource = telemetry.tracer_resource(settings, "dialog")

    assert resource == {"service.name": "agent-dialog"}


def test_two_roles_in_one_process_give_two_tracers():
    """Две роли в ОДНОМ процессе — два провайдера, а не один на всех.

    Это и есть та правка, без которой вторая роль уезжала бы под именем первой: `trace.get_tracer()`
    вернул бы трейсер ГЛОБАЛЬНОГО провайдера, а ролей у нас две.
    """
    from agent_core import telemetry
    from agent_core.config import CoreSettings

    settings = CoreSettings(platform_vendor="agenta", telemetry_vendor="openlit",
                        telemetry_otlp_endpoint="http://127.0.0.1:4318/v1/traces")

    dialog = telemetry.setup_telemetry(settings, agent="dialog")
    listing = telemetry.setup_telemetry(settings, agent="builder")

    assert dialog is not listing, "роли разделяют один трейсер — в приёмнике они сольются"
    assert telemetry.setup_telemetry(settings, agent="builder") is listing, \
        "повторный вызов обязан вернуть ТОТ ЖЕ трейсер (иначе осиротеет досылка)"


def test_fastapi_telemetry_skips_liveness_paths_and_keeps_business_ones():
    """Проверки живости не трассируются, бизнес-ручки — трассируются, лишние спаны выключены.

    Замер: `GET /health` + три служебных спана FastAPI на КАЖДЫЙ опрос давали 2 259 спанов из 2 269,
    и настоящие трейсы в списке было не найти. Краснеет этот тест, если фильтр перестанет
    отсекать `/health` (шум вернётся) или начнёт отсекать `/api/*` (пропадёт видимость работы).
    """
    from agent_core import telemetry


    config = telemetry.fastapi_telemetry()
    skip = config["exclude"]

    assert skip({"path": "/health"}) is True
    assert skip({"path": "/mcp/health"}) is True
    assert skip({"path": "/metrics"}) is True
    assert skip({"path": "/api/run"}) is False
    assert skip({"path": "/api/jobs/generate"}) is False
    assert config["operation_spans"] is False, "четыре спана на запрос вместо одного — это и был шум"


# --- ОДИН ВХОД К СПАНУ: два контура не должны расходиться статусом ошибки --------------------------
#
# **Зачем эти тесты.** В ядре жили ДВЕ реализации «открыть спан»: `spans.span` (умеет не помечать
# исключение ошибкой — нужно графу, где приостановка не сбой) и `telemetry.span` (не умел). Вторую звал
# один контур, первую — другой, поэтому одно и то же событие давало в трейсах РАЗНЫЙ статус. Проверка
# держит единство: реализация одна, а старый вход — делегат с тем же поведением.

class _RecordingSpan:
    """Спан-двойник: запоминает атрибуты и то, как его открыли."""

    def __init__(self, record: dict, name: str, attributes: dict, kwargs: dict) -> None:
        self._record = record
        record["spans"].append({"name": name, "attributes": dict(attributes or {}), "kwargs": dict(kwargs), "set": {}})

    def set_attribute(self, key: str, value) -> None:
        self._record["spans"][-1]["set"][key] = value

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


class _RecordingTracer:
    def __init__(self) -> None:
        self.record: dict = {"spans": []}

    def start_as_current_span(self, name: str, attributes: dict | None = None, **kwargs):
        return _RecordingSpan(self.record, name, attributes, kwargs)


def test_both_entries_open_a_span_with_the_same_error_policy():
    """`telemetry.span` и `spans.span` — один контракт: одинаковые аргументы и одинаковый статус ошибки.

    Краснеет, если кто-то снова заведёт вторую реализацию: у делегата kwargs обязаны совпасть с
    `spans.span` (`record_exception`/`set_status_on_exception`), а не потеряться.
    """
    from agent_core import spans, telemetry

    one, two = _RecordingTracer(), _RecordingTracer()
    with telemetry.span(one, "turn", {"a": 1}) as current:
        current.set_attribute("k", "v")
    with spans.span(two, "turn", {"a": 1}) as current:
        current.set_attribute("k", "v")

    assert one.record["spans"] == two.record["spans"], \
        "два входа к спану расходятся — статус ошибки в трейсах снова будет разным"


def test_record_exceptions_switch_works_through_the_old_entry():
    """Графу нужен `record_exceptions=False` (приостановка — не сбой); через старый вход он тоже доступен."""
    from agent_core import telemetry


    tracer = _RecordingTracer()
    with telemetry.span(tracer, "visit.tick", record_exceptions=False):
        pass

    kwargs = tracer.record["spans"][0]["kwargs"]
    assert kwargs["record_exception"] is False and kwargs["set_status_on_exception"] is False


def test_span_without_tracer_is_a_no_op_through_both_entries():
    """Без трейсера оба входа дают пустой контекст и не падают — регресс на «а если трассировки нет»."""
    from agent_core import spans, telemetry

    for entry in (telemetry.span, spans.span):
        with entry(None, "turn", {}) as current:
            assert current is None
