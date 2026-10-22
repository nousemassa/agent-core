"""Тесты чтения ответа агента из журнала сессии (без сети: фейковый HTTP-клиент).

Главное, что проверяем: сессия канала переиспользуется на весь тред, поэтому «done» от прошлого
хода не должен приниматься за ответ текущего (иначе ответы отстают ровно на шаг).
"""
from __future__ import annotations

from agent_core.vendors.channels.agenta import AgentaChannels
from agent_core.vendors.channels.port import Tail


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class FakeHttp:
    """Отдаёт заранее заданные страницы журнала и помнит тела запросов."""

    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages
        self.calls: list[dict] = []

    async def post(self, path: str, json: dict | None = None, **kwargs) -> FakeResponse:
        self.calls.append(json or {})
        if path.endswith("/records/query"):
            index = min(len(self.calls) - 1, len(self.pages) - 1)
            return FakeResponse(self.pages[index])
        return FakeResponse({})


def _record(sequence: int, record_type: str, text: str = "") -> dict:
    return {"sequence": sequence, "record_type": record_type, "attributes": {"text": text}}


def _client(pages: list[dict]) -> AgentaChannels:
    client = AgentaChannels(base_url="http://x", api_key="k", project_id="p", bot="test-bot")
    client._client = FakeHttp(pages)  # type: ignore[assignment]
    return client


async def test_read_tail_takes_last_reply_and_done_sequence():
    client = _client([{"count": 5, "records": [
        _record(1, "message", "From user tg-1 (agenta id 1):привет"),
        _record(2, "thought", "думаю"),
        _record(3, "message", "Здравствуйте! Вот объекты…"),
        _record(4, "usage"),
        _record(5, "done"),
    ]}])
    tail = await client.read_tail(session_id="s1")
    assert tail.text == "Здравствуйте! Вот объекты…", "эхо входящего сообщения не ответ"
    assert tail.done_seq == 5
    assert tail.records == 5


async def test_read_tail_pages_to_the_end_of_long_session():
    first_page = {"count": 300, "records": [_record(i, "message", f"строка {i}") for i in range(1, 201)]}
    tail_page = {"count": 300, "records": [_record(299, "message", "свежий ответ"), _record(300, "done")]}
    client = _client([first_page, tail_page])

    tail = await client.read_tail(session_id="s1")

    assert tail.text == "свежий ответ", "журнал длиннее окна — нужен хвост, а не первые записи"
    assert tail.done_seq == 300
    assert client._client.calls[1]["windowing"]["offset"] == 100  # type: ignore[attr-defined]


async def test_ask_waits_for_a_new_turn_and_ignores_previous_done():
    client = _client([])
    client.revision_id = "rev-1"

    script = [
        Tail(text="прошлый ответ", done_seq=5, records=5),   # baseline (до отправки)
        Tail(text="прошлый ответ", done_seq=5, records=7),   # ход идёт: записи выросли, done старый
        Tail(text="новый ответ", done_seq=10, records=10),   # ход завершён
    ]
    calls = {"n": 0, "posted": []}

    async def session_ids(*, revision_id=None, user=None, limit=20):
        return ["s1"]

    async def read_tail(*, session_id):
        current = script[min(calls["n"], len(script) - 1)]
        calls["n"] += 1
        return current

    async def post_message(*, user, text):
        calls["posted"].append(text)

    client.session_ids = session_ids  # type: ignore[assignment]
    client.read_tail = read_tail  # type: ignore[assignment]
    client.post_message = post_message  # type: ignore[assignment]

    reply = await client.ask(user="u", text="новый вопрос", wait_s=2.0, poll_s=0.01)

    assert calls["posted"] == ["новый вопрос"]
    assert reply.text == "новый ответ", "старый done не считается ответом"
    assert reply.done and reply.session_id == "s1"


# ---------- сессия ищется ПО ПОЛЬЗОВАТЕЛЮ, а не «последние 3» ----------

class FakeStreamsHttp(FakeHttp):
    """Отдаёт список потоков канала: свежие — чужие, сессия нашего пользователя — 4-я по давности."""

    def __init__(self) -> None:
        super().__init__([])
        self.streams = [
            {"session_id": f"other-{i}", "created_at": f"2026-10-05T1{i}:00:00Z",
             "name": f"From user tg-tg:700000000{i} (agenta id …):…", "references": [{"id": "rev-9"}]}
            for i in range(3)
        ] + [{"session_id": "ours-session", "created_at": "2026-10-04T05:35:00Z",
              "name": "From user tg-tg:100000002 (agenta id tg-tg:100000002):…",
              "references": [{"id": "rev-9"}]}]

    async def post(self, path, json=None, **kwargs):
        self.calls.append(json or {})
        if path.endswith("/streams/query"):
            return FakeResponse({"streams": self.streams})
        return FakeResponse({"count": 0, "records": []})


async def test_session_lookup_prefers_current_user():
    """4-й по давности пользователь раньше не находился (опрашивались «последние 3»)."""
    client = AgentaChannels(base_url="http://x", api_key="k", project_id="p", bot="test-bot")
    client._client = FakeStreamsHttp()  # type: ignore[assignment]
    client.revision_id = "rev-9"

    ours = await client.session_ids(revision_id="rev-9", user="tg-tg:100000002")
    assert ours == ["ours-session"], "сессия этого пользователя должна находиться независимо от давности"

    fresh = await client.session_ids(revision_id="rev-9")
    assert "ours-session" in fresh, "предел опроса шире трёх сессий"
    assert len(fresh) == 4



class FakeRevisionHttp(FakeHttp):
    """Потоки: **своя** сессия — под ПРЕЖНЕЙ ревизией, чужие — под текущей.

    Форма отказа: свою сессию не найти (фильтр по закреплённой ревизии), и фолбэк подставляет «свежие
    сессии любых пользователей». Владелец получает запасной текст, хотя ответ лежит в его же сессии.
    """

    def __init__(self) -> None:
        super().__init__([])
        # Форма ссылок повторяет ФАКТИЧЕСКИЙ ответ платформы: три ссылки — workflow,
        # workflow_variant и workflow_revision с `key`, `id` и `version`. Прежняя версия
        # фейка содержала только `id`, и это было непохоже на реальность: фильтр «без
        # ревизии» ищет ссылку именно по `key == "workflow_revision"`.
        ours = {"id": "own-rev-old", "key": "workflow_revision", "version": "4", "slug": "agent-dialog"}
        theirs = {"id": "rev-9", "key": "workflow_revision", "version": "9", "slug": "agent-dialog"}
        self.streams = [
            {"session_id": "foreign-1", "created_at": "2026-10-11T14:26:00Z",
             "name": "From user tg-tg:1309999684 (agenta id tg-tg:1309999684):…",
             "references": [{"id": "wf", "key": "workflow"},
                            {"id": "wv", "key": "workflow_variant"}, dict(theirs)]},
            {"session_id": "own-under-old-revision", "created_at": "2026-09-30T12:47:27Z",
             "name": "From user tg-tg:489313430 (agenta id tg-tg:489313430):…",
             "references": [{"id": "wf", "key": "workflow"},
                            {"id": "wv", "key": "workflow_variant"}, dict(ours)]},
        ]

    async def post(self, path, json=None, **kwargs):
        self.calls.append(json or {})
        if path.endswith("/streams/query"):
            return FakeResponse({"streams": self.streams})
        return FakeResponse({"count": 0, "records": []})


def _spy_client(http) -> tuple[AgentaChannels, list[dict]]:
    """Клиент с записанными запросами выбора сессий: видно, кого именно спрашивали."""
    client = AgentaChannels(base_url="http://x", api_key="k", project_id="p", bot="test-bot")
    client._client = http  # type: ignore[assignment]
    client.revision_id = "rev-9"
    seen: list[dict] = []
    original = client.session_ids

    async def spy(*, revision_id=None, user=None, limit=20):
        seen.append({"revision_id": revision_id, "user": user})
        return await original(revision_id=revision_id, user=user, limit=limit)

    client.session_ids = spy  # type: ignore[assignment]
    return client, seen


async def test_poll_sessions_finds_own_session_under_an_older_revision():
    """Своя сессия под прежней ревизией обязана находиться — иначе ответ не доходит."""
    client, _ = _spy_client(FakeRevisionHttp())

    load = client._poll_sessions(user="tg-tg:489313430")
    sessions = await load()

    assert sessions == ["own-under-old-revision"], (
        "сессия своего чата должна находиться и под прежней ревизией: иначе потребитель ждёт ответ "
        "в чужих сессиях и отдаёт клиенту запасной текст")


async def test_poll_sessions_never_asks_for_foreign_sessions():
    """Чужие сессии не запрашиваются вовсе: иначе ответ другого клиента уйдёт этому.

    Проверяется по ЗАПРОСАМ, а не по результату: важно именно то, что поиск без имени
    пользователя не делается ни на одном шаге.
    """
    client, seen = _spy_client(FakeRevisionHttp())

    await client._poll_sessions(user="tg-tg:489313430")()

    assert all(query["user"] for query in seen), f"запрос без пользователя = чужие сессии: {seen}"
    assert "foreign-1" not in {query.get("user") for query in seen}


async def test_poll_sessions_stops_at_current_revision_when_found():
    """Если своя сессия есть под текущей ревизией — второй запрос не нужен."""
    client, seen = _spy_client(FakeStreamsHttp())

    sessions = await client._poll_sessions(user="tg-tg:100000002")()

    assert sessions == ["ours-session"]
    assert len(seen) == 1, "лишний запрос «на всякий случай» — это лишние обращения к платформе"
