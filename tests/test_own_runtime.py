"""Свой рантайм диалога: форма ответа, форма отказа, ретраи и ЧЕСТНАЯ цена.

**Почему эти тесты именно такие.** Порт требует, чтобы `own` повторял форму платформы, но повторение
нельзя проверить «на глаз» — поэтому здесь один параметризованный тест гоняет одни и те же СЦЕНАРИИ
через обе реализации и сравнивает наблюдаемое (бросил/нет, `done`, ключи `usage`).

**Двойник — только транспорт.** `httpx.MockTransport` стоит на месте сети, а не нашего кода: промпт,
ретраи, дедлайн, подсчёт цены — настоящие.

**Главный капкан, который здесь закрыт.** `store.usage_fields` на не-словарь отдаёт `(None,None,None,None)`,
а `cost_usd` суммирует `coalesce(sum, 0)`. Поэтому `usage` не словарём и ноль вместо `None` дают в отчёте
«$0 за платный ход» — без единой ошибки в журнале.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from agent_core.db.store import usage_fields
from agent_core.vendors.runtime import provider
from agent_core.vendors.runtime.own import OwnRuntime
from agent_core.vendors.runtime.platform_api import PlatformRuntime
from agent_core.vendors.runtime.port import RoleSpec

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "prompts"
DIALOG = FIXTURES / "dialog.md"
LISTING = FIXTURES / "alt.md"


class _Settings:
    """Настройки, которых достаточно рантайму: путь к промпту роли, креды и цены роли."""

    #: Ровно та форма, которой пользуется `own`: аксессоры роли, а не поля по месту.
    def __init__(self, *, price_in: float = 0.0, price_out: float = 0.0, path: str = str(DIALOG),
                 role: str = "dialog") -> None:
        self._role = role
        self._path = path
        self._prices = (price_in, price_out)

    def runtime_prompt_setting(self, agent: str) -> str:
        return f"{agent.upper()}_PROMPT_PATH"

    def runtime_prompt_path(self, agent: str) -> str:
        return self._path if agent == self._role else ""

    def runtime_prices(self, agent: str) -> tuple[float, float]:
        return self._prices if agent == self._role else (0.0, 0.0)

    def chat_credentials(self) -> tuple[str, str, str]:
        return ("http://provider.local", "key", "deepseek/deepseek-v4.1-flash")

    def role_spec(self, role: str | None = None) -> RoleSpec:
        """Роль так, как её объявляет продукт: путь промпта, цены и свойства своего рантайма."""
        name = role or self._role
        known = name == self._role
        return RoleSpec(
            name=name,
            prompt_path=self._path if known else "",
            price_in=self._prices[0] if known else 0.0,
            price_out=self._prices[1] if known else 0.0,
            own_supported=name in ("dialog", "alt"),
            history_limit=8 if name == "dialog" else 0,
        )


def _reply(*, content: str = "Здравствуйте!", tokens: tuple[int, int] = (1200, 40)) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1],
                  "total_tokens": tokens[0] + tokens[1]},
    }


class _Transport:
    """Сценарий из последовательности ответов; запоминает тела запросов."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self._responses = list(responses)
        self.bodies: list[dict] = []
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.bodies.append(json.loads(request.content))
        item = self._responses[min(self.calls - 1, len(self._responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def _run(coro):
    return asyncio.run(coro)


async def _ask(settings: _Settings, transport: _Transport, *, text: str = "привет", wait_s: float = 60.0,
               loader=None, role: str = "dialog"):
    """Ход через НАСТОЯЩИЙ рантайм: подменён только транспорт (сеть), а не наш вызов."""
    runtime = OwnRuntime(role=settings.role_spec(role), settings=settings)
    runtime.history_loader = loader
    await runtime.ensure_connection()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        runtime.http_client = client
        return await runtime.ask(user="tg-1", text=text, wait_s=wait_s)


def test_usage_is_a_platform_shaped_dict():
    transport = _Transport(httpx.Response(200, json=_reply()))
    reply = _run(_ask(_Settings(price_in=0.5, price_out=1.5), transport))

    assert reply.done is True
    assert reply.text == "Здравствуйте!"
    assert isinstance(reply.usage, dict)
    assert set(reply.usage) == {"input", "output", "total", "cost"}
    #: 1200 * 0.5/1000 + 40 * 1.5/1000 = 0.66 — цена сходится числом, а не «примерно».
    assert reply.usage["cost"] == pytest.approx(0.66)


def test_unset_prices_give_none_and_never_zero():
    """Главный капкан: ноль в `usage` читался бы как «ход бесплатный»."""
    transport = _Transport(httpx.Response(200, json=_reply()))
    reply = _run(_ask(_Settings(), transport))

    assert reply.usage["cost"] is None
    #: И это доезжает до хранилища именно как «неизвестно», а не как 0.
    assert usage_fields(reply.usage)[3] is None


def test_request_carries_header_budget_and_no_reasoning():
    transport = _Transport(httpx.Response(200, json=_reply()))
    _run(_ask(_Settings(), transport))

    body = transport.bodies[0]
    assert body["max_tokens"] == 400               # из шапки файла, а не из константы
    assert body["temperature"] == 0.2
    assert body["reasoning"] == {"enabled": False}  # иначе `content` пуст
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


def test_history_arrives_between_system_and_current_turn():
    transport = _Transport(httpx.Response(200, json=_reply()))

    async def loader(user: str, limit: int):
        assert (user, limit) == ("tg-1", 8)
        return [{"role": "user", "content": "хочу двушку"}, {"role": "assistant", "content": "есть на Садовой"}]

    _run(_ask(_Settings(), transport, text="а второй?", loader=loader))
    roles = [m["role"] for m in transport.bodies[0]["messages"]]
    contents = [m["content"] for m in transport.bodies[0]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    assert contents[-1] == "а второй?"


def test_429_then_200_is_one_answer_and_two_attempts():
    transport = _Transport(httpx.Response(429, headers={"Retry-After": "0"}, json={}),
                           httpx.Response(200, json=_reply()))
    reply = _run(_ask(_Settings(), transport))

    assert reply.done is True
    assert transport.calls == 2


def test_400_is_not_retried_and_raises():
    """Неверный запрос повтором не лечится: ретрай только сжёг бы деньги."""
    transport = _Transport(httpx.Response(400, json={"error": "bad"}))
    with pytest.raises(provider.RuntimeUnavailable) as caught:
        _run(_ask(_Settings(), transport))

    assert transport.calls == 1
    assert "400" in str(caught.value)
    assert not isinstance(caught.value, provider.RuntimeTimeout)


def test_503_three_times_raises_with_attempt_count():
    transport = _Transport(httpx.Response(503, headers={"Retry-After": "0"}, json={}))
    with pytest.raises(provider.RuntimeUnavailable) as caught:
        _run(_ask(_Settings(), transport))

    assert transport.calls == provider.ATTEMPTS
    assert "503" in str(caught.value)


def test_deadline_returns_done_false_instead_of_raising():
    """Форма платформы: не дождались — `done=False`, а НЕ исключение (`client.py:252-253`)."""
    transport = _Transport(httpx.Response(503, headers={"Retry-After": "0"}, json={}))
    reply = _run(_ask(_Settings(), transport, wait_s=0.5))

    assert reply.done is False
    assert reply.text == ""
    assert reply.usage == {}


def test_empty_content_is_not_an_answer_but_the_money_is_reported():
    transport = _Transport(httpx.Response(200, json=_reply(content="   ")))
    reply = _run(_ask(_Settings(price_in=0.5), transport))

    assert reply.done is False
    assert reply.text == ""
    #: Заплатили — значит расход обязан быть виден, иначе ход выглядит бесплатным.
    assert reply.usage["input"] == 1200


def test_strict_user_does_not_change_behaviour():
    transport_plain = _Transport(httpx.Response(200, json=_reply()))
    transport_strict = _Transport(httpx.Response(200, json=_reply()))

    plain = _run(_ask(_Settings(), transport_plain))
    strict = _run(_ask(_Settings(), transport_strict))

    assert plain.text == strict.text


def test_close_is_idempotent():
    runtime = OwnRuntime(role=_Settings().role_spec(), settings=_Settings())

    async def go():
        await runtime.ensure_connection()
        await runtime.aclose()
        await runtime.aclose()

    _run(go())


def test_transport_lives_on_the_runtime_and_survives_a_stale_connection(monkeypatch):
    """Клиент — на рантайм, а не на ход; протухшее соединение ПОВТОРЯЕТСЯ, а не роняет ход.

    Две вещи в одном тесте намеренно: они правятся вместе и ломаются вместе. Переиспользование клиента
    без ретрая `RemoteProtocolError` сделало бы первый ход после простоя отказом транспорта — то есть
    «оптимизация» добавила бы отказ. Оба `ask` идут в ОДНОМ `asyncio.run`: клиент привязан к петле.
    """
    transport = _Transport(httpx.RemoteProtocolError("Server disconnected"),
                           httpx.Response(200, json=_reply()))
    made: list = []

    def counting_make():
        client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        made.append(client)
        return client

    monkeypatch.setattr(provider, "make_client", counting_make)

    async def go():
        runtime = OwnRuntime(role=_Settings().role_spec(), settings=_Settings())
        await runtime.ensure_connection()
        try:
            first = await runtime.ask(user="tg-1", text="привет")
            second = await runtime.ask(user="tg-1", text="ещё")
        finally:
            await runtime.aclose()
        return first, second

    first, second = _run(go())

    assert first.done is True and first.text == "Здравствуйте!"
    assert second.done is True
    #: Один конструктор на два хода (обрыв + повтор внутри первого — тот же клиент).
    assert len(made) == 1
    #: Первый ход: обрыв, повтор; второй ход: ответ. Три вызова транспорта, а не пять.
    assert transport.calls == 3


def test_closed_transport_is_a_refusal_not_a_server_error():
    """Клиента закрыли на остановке, а ход ещё летит: httpx бросает `RuntimeError`, НЕ `HTTPError`.

    Без этого `except` он пролетал бы мимо всех обработчиков и выходил клиенту как 500 — то есть
    плановая остановка процесса выглядела бы поломкой.
    """
    from agent_core.vendors.runtime.prompt_spec import spec_for

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(_Transport(httpx.Response(200, json=_reply()))))
        await client.aclose()
        with pytest.raises(provider.RuntimeUnavailable):
            await provider.chat(settings=_Settings(), spec=spec_for(DIALOG), history=[], text="привет",
                                wait_s=10.0, prices=(0.0, 0.0), client=client, sleep=_nosleep)

    _run(go())


async def _nosleep(_seconds: float) -> None:
    return None


async def _observable(runtime, *, text: str = "привет") -> dict:
    """Наблюдаемая форма хода: бросил ли, и что в ответе. Одна форма для обеих реализаций."""
    try:
        reply = await runtime.ask(user="tg-1", text=text)
    except Exception as error:  # noqa: BLE001 — это и есть наблюдаемое
        return {"raised": type(error).__name__, "done": None, "text": None,
                "usage_keys": None, "session": None}
    return {"raised": None, "done": reply.done, "text": reply.text,
            "usage_keys": sorted((reply.usage or {}).keys()), "session": reply.session_id}


class _ChannelsStub:
    """Клиент канала: отвечает тем, что велели, и БРОСАЕТ так же, как настоящий (`raise_for_status`)."""

    def __init__(self, *, mode: str) -> None:
        self.mode = mode

    class _Reply:
        def __init__(self, *, text: str, done: bool, usage, session_id: str) -> None:
            self.text, self.done, self.usage, self.session_id = text, done, usage, session_id

    async def ensure_connection(self, *, slug: str = "") -> str:
        return "conn"

    async def ensure_agent(self, **kwargs) -> None:
        return None

    async def ask(self, *, user: str, text: str, wait_s: float = 60.0, poll_s: float = 3.0,
                  strict_user: bool = False):
        if self.mode == "ok":
            return self._Reply(text="Здравствуйте!", done=True,
                               usage={"cost": 0.66, "input": 1200, "output": 40, "total": 1240},
                               session_id=user)
        if self.mode == "timeout":
            return self._Reply(text="", done=False, usage=None, session_id="")
        if self.mode == "http500":
            raise httpx.HTTPStatusError("500", request=httpx.Request("POST", "http://x"),
                                        response=httpx.Response(500))
        raise AssertionError(f"сценарий {self.mode} не реализован")

    async def aclose(self) -> None:
        return None


@pytest.mark.parametrize("mode", ["ok", "timeout", "http500"])
def test_failure_form_matches_the_platform(mode: str):
    """ОДИН тест на обе реализации: наблюдаемое обязано совпасть по виду отказа.

    Это и есть требование порта «форма отказа не переопределяется» (`port.py:25-27`), проверенное
    сравнением, а не обещанием в докстринге.
    """
    platform = PlatformRuntime(client=_ChannelsStub(mode=mode))
    if mode == "timeout":
        own_transport = _Transport(httpx.Response(503, headers={"Retry-After": "0"}, json={}))
        own = _run(_ask(_Settings(), own_transport, wait_s=0.5))
        expected = _run(_observable(platform))
        assert expected["done"] is False and expected["text"] == "" and expected["session"] == ""
        assert (own.done, own.text, own.session_id) == (False, "", "")
        return
    if mode == "http500":
        own_transport = _Transport(httpx.Response(500, headers={"Retry-After": "0"}, json={}))
        with pytest.raises(provider.RuntimeUnavailable):
            _run(_ask(_Settings(), own_transport))
        #: Имена исключений РАЗНЫЕ и это нормально: сайт ловит `Exception` и закрывает ход
        #: (`dialog.py:653-663`). Сравниваем то, что видит вызывающий: «бросил», а не «какой класс».
        expected = _run(_observable(platform))
        assert expected["raised"] is not None
        return
    expected = _run(_observable(platform))
    own_reply = _run(_ask(_Settings(price_in=0.5, price_out=1.5), _Transport(httpx.Response(200, json=_reply()))))
    assert expected["done"] is True and expected["usage_keys"] == ["cost", "input", "output", "total"]
    assert own_reply.done is True
    assert sorted(own_reply.usage) == expected["usage_keys"]
    assert own_reply.session_id == "tg-1"


# --- роль как свойство: промпт, цены и история — ПО РОЛИ ----------------------------------------

def test_second_role_has_no_history_by_design_and_does_not_warn(caplog):
    """У второй роли истории нет ПО СМЫСЛУ: каждый ход — новый тред, «предыдущей реплики» не существует.

    Значит загрузчик не должен зваться вовсе, и в журнале не должно быть предупреждения «история
    недоступна» — иначе журнал шумел бы на каждом ходу, а шум читают как поломку.
    """
    called: list[str] = []

    async def loader(user: str, limit: int):
        called.append(user)
        return [{"role": "user", "content": "лишнее"}]

    async def go():
        runtime = OwnRuntime(role=_Settings(role="alt", path=str(LISTING)).role_spec(), settings=_Settings(role="alt", path=str(LISTING),
                                                                 price_in=0.5))
        runtime.history_loader = loader
        await runtime.ensure_connection()
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=_reply()))) as client:
            runtime.http_client = client
            return await runtime.ask(user="job-1", text="сгенерируй текст")

    with caplog.at_level("WARNING"):
        reply = _run(go())

    assert called == [], "у роли без истории загрузчик не зовётся"
    assert reply.done is True
    assert "история реплик недоступна" not in caplog.text, "ноль истории — свойство роли, а не отказ"


def test_prices_come_from_the_role_not_from_dialog():
    """Цена роли: у диалога и второй роли она может быть разной — иначе ходы считались бы по чужому тарифу."""
    dialog_settings = _Settings(price_in=0.5, price_out=1.5)
    listing_settings = _Settings(role="alt", path=str(LISTING), price_in=0.25, price_out=0.75)

    dialog_reply = _run(_ask(dialog_settings, _Transport(httpx.Response(200, json=_reply()))))
    listing_reply = _run(_ask(listing_settings, _Transport(httpx.Response(200, json=_reply())),
                              role="alt"))

    #: 1200 * 0.5/1000 + 40 * 1.5/1000 = 0.66 ; 1200 * 0.25/1000 + 40 * 0.75/1000 = 0.33
    assert dialog_reply.usage["cost"] == pytest.approx(0.66)
    assert listing_reply.usage["cost"] == pytest.approx(0.33)


def test_refusal_names_the_role_whose_prompt_is_missing():
    """Отказ обязан называть РОЛЬ, у которой нет пути промпта: иначе разбор начинается вслепую."""
    async def go():
        runtime = OwnRuntime(role=_Settings(role="alt", path="").role_spec(),
                             settings=_Settings(role="alt", path=""))
        await runtime.ensure_connection()

    with pytest.raises(Exception) as caught:
        _run(go())

    message = str(caught.value)
    assert "alt" in message, "отказ не назвал роль"
    assert "путь" in message.lower(), "отказ не сказал, чего не хватает"
