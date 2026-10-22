"""Нейтральные двойники общего слоя: каналы, Telegram-клиент, база в памяти.

Доменных двойников здесь нет: продукт добавляет свои в своём дереве тестов.
"""
from __future__ import annotations

import pathlib
import tempfile
from types import SimpleNamespace

import pytest

from agent_core.config import CoreSettings

SECRET = "s" * 40

OWNER = "111"

class FakeMsg:
    """Сообщение от клиента/овнера: фиксирует отправленные ответы.

    `sender` — профиль отправителя для самозаявки: по умолчанию живой человек с ником.
    """

    def __init__(self, text: str, chat_id: str = "777", private: bool = True, *,
                 sender: object | None = "default", sender_raises: BaseException | None = None) -> None:
        self.raw_text = text
        self.chat_id = int(chat_id)
        self.is_private = private
        self.sender = (SimpleNamespace(id=int(chat_id), username="applicant", bot=False)
                       if sender == "default" else sender)
        self.sender_raises = sender_raises
        self.sent: list[tuple[str, object]] = []

    async def get_sender(self):
        if self.sender_raises:
            raise self.sender_raises
        return self.sender

    async def respond(self, text=None, buttons=None, **kwargs) -> None:
        self.sent.append((text, buttons))

class FakeSender:
    """Профиль отправителя: `bot`/`broadcast`/`megagroup` покрывают отказ «это бот или канал»."""

    def __init__(self, *, user_id: int = 777, username: str | None = "applicant",
                 bot: bool = False, broadcast: bool | None = None, megagroup: bool | None = None) -> None:
        self.id = user_id
        self.username = username
        self.bot = bot
        self.broadcast = broadcast
        self.megagroup = megagroup

    @property
    def texts(self) -> list[str]:
        return [item[0] for item in self.sent]

class FakeCallback:
    """Callback-нажатие: порядок вызовов важен (тост до снятия кнопок)."""

    def __init__(self, data: str = "", chat_id: str = OWNER, private: bool = True, *,
                 sender: object | None = "default") -> None:
        self.data = (data or "").encode()
        self.chat_id = int(chat_id)
        self.is_private = private
        self.sender = (SimpleNamespace(id=int(chat_id), username="applicant", bot=False)
                       if sender == "default" else sender)
        self.log: list[str] = []
        self.answers: list[str | None] = []
        self.sent: list[tuple] = []

    async def get_sender(self):
        return self.sender

    async def answer(self, text=None, **kwargs) -> None:
        self.log.append("answer")
        self.answers.append(text)

    async def edit(self, **kwargs) -> None:
        self.log.append("edit")

    async def respond(self, text=None, buttons=None, **kwargs) -> None:
        self.log.append("respond")
        self.sent.append((text, buttons))

def _trace_cost(row: dict) -> float:
    """Стоимость строки трассы — из записи `usage`, как в настоящем `Db`."""
    usage = row.get("usage")
    if not isinstance(usage, dict) or usage.get("cost") is None:
        return 0.0
    try:
        return float(usage["cost"])
    except (TypeError, ValueError):
        return 0.0

class FakeDb:
    """Минимум сервиса: диалог, очередь исходящих, согласия, настройки, метрики."""

    # --- очередь исходящих: в тестах базы нет, поэтому строки копим в памяти ---
    async def enqueue_outbox(self, *, kind: str, payload: dict) -> int:
        rows = getattr(self, "outbox_rows", None)
        if rows is None:
            rows = self.outbox_rows = []
        rows.append({"kind": kind, "payload": payload})
        return len(rows)

    async def outbox_lag_s(self) -> float:
        return 0.0

    async def set_max_metric(self, *, metric: str, value: float, day=None) -> None:
        peaks = getattr(self, "max_metrics", None)
        if peaks is None:
            peaks = self.max_metrics = {}
        peaks[metric] = max(float(value), float(peaks.get(metric, 0.0)))

    # --- трассы ходов: в тестах базы нет, поэтому строки копим в памяти ---
    async def save_turn_trace(self, **fields) -> None:
        rows = getattr(self, "turn_traces_rows", None)
        if rows is None:
            rows = self.turn_traces_rows = []
        rows.append(fields)

    # --- история реплик (`own`-рантайм): в памяти, как и всё остальное здесь ---
    async def append_dialog_message(self, *, platform: str, chat_id: str, turn_key: str, role: str,
                                    text: str, step: str = "") -> None:
        rows = getattr(self, "dialog_rows", None)
        if rows is None:
            rows = self.dialog_rows = []
        rows.append({"platform": platform, "chat_id": chat_id, "turn_key": turn_key,
                     "role": role, "text": text, "step": step})

    async def load_dialog_history(self, *, platform: str, chat_id: str, limit: int = 8) -> list[dict]:
        rows = [row for row in getattr(self, "dialog_rows", [])
                if row["platform"] == platform and row["chat_id"] == chat_id]
        return [{"role": row["role"], "content": row["text"]} for row in rows[-limit:]]

    async def cost_usd(self, day) -> float:
        return sum(_trace_cost(row) for row in getattr(self, "turn_traces_rows", []))

    async def cost_usd_month(self, day) -> float:
        return await self.cost_usd(day)


    async def turn_traces(self, *, days: int = 7, limit: int = 200) -> list[dict]:
        return list(getattr(self, "turn_traces_rows", []))[:limit]

    def __init__(self, *, consent: bool = True) -> None:
        self.consent = consent
        # «Стенные часы» фейка: от них считается TTL овнер-флоу (в проде — time.time).
        self.now = 1000.0
        self.metrics: dict[str, float] = {}
        self.actions: list[tuple[str, str, str]] = []
        self.consent_saves = 0
        self.ai = True          # /noai: False ⇒ хода к агенту нет
        self.state = None
        self.conversations: dict[str, dict] | None = {}
        self.settings_writes: list[tuple[str, object]] = []
        self.settings_values: dict[str, object] = {}

    # --- диалог ---
    async def has_consent(self, **kwargs) -> bool:
        return self.consent

    async def save_consent(self, **kwargs) -> bool:
        self.consent_saves += 1
        self.consent = True
        return True

    async def revoke_consent(self, **kwargs) -> None:
        self.consent = False


    async def set_setting(self, key: str, value) -> None:
        self.settings_writes.append((key, value))
        self.settings_values[key] = value
        if key == "bot_mode":
            self.bot_mode = value


    async def day_metrics(self, day, names) -> dict:
        return {name: self.metrics.get(name, 0) for name in names}

    async def setting(self, key: str, default=None):
        if key in self.settings_values:
            return self.settings_values[key]
        if key == "bot_mode":
            return self.bot_mode
        if key == "schedule":
            return default
        return default






    async def load_conversation(self, *, platform: str = "tg", chat_id: str = "", **kwargs):
        """Как pg.py: строка на (platform, chat_id) с полем continuum."""
        if self.conversations is not None:
            return self.conversations.get(str(chat_id))
        return self.state

    async def save_conversation(self, *, platform: str = "tg", chat_id: str = "", step=None,
                                continuum=None, **kwargs) -> None:
        if self.conversations is None:
            return
        self.conversations[str(chat_id)] = {"step": step, "continuum": continuum or {}}


    # --- заявки и роли ---





    async def ai_allowed(self, *, platform: str = "tg", chat_id: str) -> bool:
        return self.ai




    # --- фоновые сводки ---


























    async def log_audit(self, *, actor: str, action: str, result: str | None = None,
                        meta: dict | None = None) -> None:
        self.audit_entries = getattr(self, "audit_entries", [])
        self.audit_entries.append({"actor": actor, "action": action, "result": result, "meta": meta})

    async def bump_metric(self, *, metric: str, delta: float = 1, day: str | None = None) -> None:
        self.metrics[metric] = self.metrics.get(metric, 0) + delta

    # --- owner flow ---


class StrictChannels:
    """Доказывает «LLM не вызван»: любой `ask` — счётчик + падение."""

    def __init__(self) -> None:
        self.calls = 0

    async def ask(self, **kwargs):
        self.calls += 1
        raise AssertionError("агент не должен вызываться на этом пути")

class ScriptedChannels:
    """Канальный стаб с ЗАДАННЫМ ответом агента: нужен там, где `StrictChannels` бросает
    исключение до проверяемой ветки (текстовый ход на промежуточном шаге)."""

    def __init__(self, text: str = "Ответ агента.", *, done: bool = True) -> None:
        self.text = text
        self.done = done
        self.calls = 0
        self.prompts: list[str] = []
        self.strict_users: list[bool] = []

    async def ask(self, *, user: str, text: str, wait_s=None, strict_user: bool = False, **kwargs):
        self.calls += 1
        self.prompts.append(text)
        self.strict_users.append(strict_user)
        return SimpleNamespace(text=self.text, error=None, cost_usd=None, done=self.done, usage=None,
                               session_id="s1", records=1)

TEST_SESSIONS_DIR = pathlib.Path(tempfile.mkdtemp(prefix="agenta-test-sessions-"))

def make_settings(**overrides) -> CoreSettings:
    base = {
        "sessions_dir": str(TEST_SESSIONS_DIR),
        "ui_secret": SECRET,
        "bridge_api_key": "k" * 32,
        "telegram_owner_chat_id": OWNER,
    }
    base.update(overrides)
    return CoreSettings(_env_file=None, **base)


def fake_client(*, user=None, raises=None):
    """Фейковый Telegram-клиент: резолв ника и отправка сообщений без сети."""

    class FakeClient:
        def __init__(self) -> None:
            self.user = user
            self.raises = raises
            self.sent: list[tuple[int, str, object]] = []
            self.resolved: list[str] = []

        def is_connected(self) -> bool:
            return True

        async def get_entity(self, target):
            self.resolved.append(target)
            if self.raises:
                raise self.raises
            return self.user or SimpleNamespace(id=999, username="nick", bot=False)

        async def send_message(self, chat_id, text, buttons=None, **kwargs):
            self.sent.append((chat_id, text, buttons))
            if self.send_raises and (self.send_raises_for is None or int(chat_id) == self.send_raises_for):
                raise self.send_raises
            self._seq += 1
            return SimpleNamespace(id=self._seq, chat_id=chat_id, text=text)

        async def edit_message(self, chat_id, message_id, text=None, buttons=None, **kwargs):
            self.edited.append((chat_id, message_id, buttons))
            if self.edit_raises:
                raise self.edit_raises
            return SimpleNamespace(id=message_id)

    fake = FakeClient()
    fake.edited = []
    fake.send_raises = None
    fake.send_raises_for = None      # точечный сбой: только для указанного чата (например, канала)
    fake.edit_raises = None
    fake._seq = 500          # id сообщений начинаем с 501, чтобы отличать карточки в тестах
    return fake


@pytest.fixture
def db() -> FakeDb:
    return FakeDb()


class FakeTelethonClient:
    """Двойник Telethon-клиента для МЕХАНИЗМА подключения (`agent_core/tg/connection.py`).

    Ручной, а не `Mock`: «подключено» должно быть СОСТОЯНИЕМ — именно его проверяет сторож.
    `handlers` — часть контракта клиента: транспорт ставит обработчики на подключении, и двойник
    обязан это отражать (иначе проверка «бот не глухой» смотрела бы в пустоту).
    """

    def __init__(self, proxy, *, fail: bool = False) -> None:
        self.proxy = proxy
        self.fail = fail
        self.connected = False
        self.authorized = True
        self.disconnects = 0
        self.handlers = 0

    async def connect(self):
        if self.fail:
            raise OSError("Network is unreachable (тест)")
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def start(self, **_kwargs):
        await self.connect()

    async def get_me(self):
        if self.fail:
            raise ConnectionError("нет связи (тест)")
        return type("Me", (), {"username": "test_bot", "id": 1})()

    def add_event_handler(self, *_args, **_kwargs) -> None:
        self.handlers += 1

    async def __call__(self, *_args, **_kwargs):
        return None

    async def disconnect(self):
        self.connected = False
        self.disconnects += 1


@pytest.fixture
def built_client(monkeypatch):
    """Подменяет сборку клиента в механизме подключения и запоминает, с каким прокси её звали.

    Общий для обоих потребителей: механизм один, и проверяют его и продукт, и новости — своей копии
    обвязки ни у кого нет.
    """
    calls: list[tuple[str, int, str] | None] = []
    failures: set[int] = set()
    state = {"fail_all": False}

    def fake_build_client(_settings, *, session_path=None, proxy=None):
        calls.append(proxy)
        return FakeTelethonClient(proxy, fail=state["fail_all"] or (len(calls) - 1) in failures)

    from agent_core.tg import connection as connection_module

    monkeypatch.setattr(connection_module, "build_client", fake_build_client)
    built = type("Built", (), {"calls": calls, "failures": failures, "state": state})()
    built.fail_all = lambda value: state.__setitem__("fail_all", value)
    return built
