"""Порт рантайма: контракт, делегирование и ось выбора — то, чем доказывается «рантайм не сломан».

Что проверяется:

1. настоящий `ChannelsClient` УДОВЛЕТВОРЯЕТ протоколу (структурная проверка) — иначе порт выдуман;
2. двойник без одной операции протоколу НЕ удовлетворяет — иначе проверка ничего не стоит;
3. делегат передаёт аргументы БЕЗ изменений (сравнение вызовов, а не «работает»);
4. ось `AGENT_RUNTIME`: по умолчанию `platform`, `auto` следует за вендором, неизвестное — отказ;
5. роль без поддержки своего рантайма получает платформу и НАЗВАННЫЙ откат.
"""

from __future__ import annotations

import pytest

from agent_core.config import CoreSettings
from agent_core.vendors.channels.agenta import AgentaChannels
from agent_core.vendors.runtime import (
    AgentRuntime,
    OwnRuntime,
    PlatformRuntime,
    RoleSpec,
    resolve,
    runtime_for,
)

DIALOG = RoleSpec(
    name="dialog",
    bot="bot-dialog",
    connection_slug="dialog-conn",
    connection_name="Dialog Bot",
    workflow_id="wf",
    revision_id="rev",
    prompt_path="/srv/agent/dialog.md",
    price_in=1.0,
    price_out=2.0,
    own_supported=True,
    history_limit=8,
)

DIGEST = RoleSpec(name="digest", bot="bot-digest", connection_slug="digest-conn", own_supported=False)


class _ClientDouble:
    """Двойник клиента: записывает вызовы, чтобы сверить их ПОАРТИЙНО."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def ensure_connection(self, *, slug: str, name: str = "") -> None:
        self.calls.append(("ensure_connection", {"slug": slug, "name": name}))

    async def ensure_agent(self, *, workflow_id: str, workflow_revision_id: str, slug: str) -> None:
        self.calls.append(("ensure_agent", {
            "workflow_id": workflow_id,
            "workflow_revision_id": workflow_revision_id,
            "slug": slug,
        }))

    async def ask(self, *, user: str, text: str, wait_s: float = 60.0, poll_s: float = 3.0,
                  strict_user: bool = False):
        self.calls.append(("ask", {
            "user": user, "text": text, "wait_s": wait_s, "poll_s": poll_s, "strict_user": strict_user,
        }))
        return "ОТВЕТ-КЛИЕНТА"

    async def aclose(self) -> None:
        self.calls.append(("aclose", {}))


class _IncompleteDouble:
    """Реализует только `ask`. Протоколу удовлетворять НЕ должен."""

    async def ask(self, **_kwargs):
        return None


def test_platform_runtime_refuses_vendor_without_channel_path():
    """У вендора без канального пути рантайм `platform` — отказ с его именем, а не чужой контракт."""
    with pytest.raises(ValueError, match="нет реализации"):
        runtime_for(CoreSettings(agent_runtime="platform", platform_vendor="openlit"), role=DIALOG)


def test_real_client_satisfies_the_port():
    """Настоящий клиент обязан структурно подходить порту — иначе шов выдуман."""
    client = AgentaChannels(base_url="http://127.0.0.1:1/api", api_key="k", project_id="p", bot="b")

    assert isinstance(client, AgentRuntime), "ChannelsClient не удовлетворяет порту — порт описан неверно"


def test_double_without_full_operations_is_rejected():
    """Проверка, которая не может упасть, — не проверка: двойник с одним `ask` портом быть не должен."""
    assert not isinstance(_IncompleteDouble(), AgentRuntime)


async def test_platform_runtime_delegates_without_changes():
    """Делегат передаёт РОВНО те аргументы: это и есть гарантия «поведение не изменилось»."""
    client = _ClientDouble()
    runtime = PlatformRuntime(client=client)
    runtime.connection_name = "Dialog Bot"  # type: ignore[attr-defined]

    await runtime.ensure_connection(slug="dialog-conn")
    await runtime.ensure_agent(workflow_id="wf", workflow_revision_id="rev", slug="bot-dialog")
    reply = await runtime.ask(user="tg-1", text="привет", wait_s=42.0, poll_s=1.5, strict_user=True)
    await runtime.aclose()

    assert reply == "ОТВЕТ-КЛИЕНТА"
    assert client.calls == [
        ("ensure_connection", {"slug": "dialog-conn", "name": "Dialog Bot"}),
        ("ensure_agent", {"workflow_id": "wf", "workflow_revision_id": "rev", "slug": "bot-dialog"}),
        ("ask", {"user": "tg-1", "text": "привет", "wait_s": 42.0, "poll_s": 1.5, "strict_user": True}),
        ("aclose", {}),
    ], "аргументы изменились — это регресс рантайма"


async def test_empty_slug_is_refused_not_passed_through():
    """Пустой slug — отказ: привязка канала без имени уехала бы в тело запроса и дала 422."""
    runtime = PlatformRuntime(client=_ClientDouble())

    with pytest.raises(ValueError, match="пустой slug"):
        await runtime.ensure_connection()


# --- ось `AGENT_RUNTIME` ---------------------------------------------------------------------------

def test_default_runtime_is_platform():
    """Пустая ось = значение по умолчанию. Включение механизма не меняет поведение ни у кого."""
    assert resolve(CoreSettings(agent_runtime="")) == "platform"
    assert resolve(CoreSettings(agent_runtime="platform")) == "platform"


def test_auto_follows_the_vendor_axis():
    assert resolve(CoreSettings(agent_runtime="auto", platform_vendor="agenta")) == "platform"
    assert resolve(CoreSettings(agent_runtime="auto", platform_vendor="openlit")) == "own"


def test_unknown_runtime_is_refused_loudly():
    """Опечатка в оси — отказ, а не молчаливый откат: иначе ход исполнит не тот рантайм."""
    with pytest.raises(ValueError, match="неизвестный рантайм"):
        resolve(CoreSettings(agent_runtime="agnta"))


def test_runtime_for_builds_platform_delegate_by_default():
    runtime = runtime_for(CoreSettings(agent_runtime="platform"), role=DIALOG)

    assert isinstance(runtime, PlatformRuntime)
    assert isinstance(runtime, AgentRuntime)
    assert runtime.connection_slug == "dialog-conn"  # type: ignore[attr-defined]


async def test_own_runtime_support_is_declared_by_the_role():
    """`own` строится для роли, которая его объявила, и отказывает остальным."""
    runtime = runtime_for(CoreSettings(agent_runtime="own"), role=DIALOG)

    assert isinstance(runtime, OwnRuntime)
    assert runtime.name == "own"
    assert not runtime.ready, "до ensure_connection рантайм не готов — /health обязан это показать"

    #: Роль без поддержки своего рантайма: отказ с названной причиной, а не «сделаю вид, что умею».
    with pytest.raises(NotImplementedError, match="не реализован"):
        OwnRuntime(role=DIGEST, settings=CoreSettings())

    #: А через фабрику та же роль получает ПЛАТФОРМУ и названный откат (иначе сервис не поднялся бы).
    assert isinstance(runtime_for(CoreSettings(agent_runtime="own"), role=DIGEST), PlatformRuntime)
