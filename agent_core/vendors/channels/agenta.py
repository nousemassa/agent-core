"""Канальный путь платформы: HTTP-клиент её контракта (адаптер порта `ChannelPath`).

**Что здесь — контракт именно этой платформы.** Пути `/channels/…`, имя канала в запросах и заголовок
`ApiKey` принадлежат ей; при смене платформы добавляется второй адаптер, а ядро и потребители не меняются.

**Подтверждено на живом инстансе:**
  * агента запускает ТОЛЬКО канал: connection → channel agent (+default) → events;
  * ответ агента лежит в records последней сессии (последний record type="message");
  * `windowing.through_sequence` в records/query обязателен.

Методы идемпотентны: существующие connection/agent переиспользуются.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from agent_core.vendors.channels.port import Reply, Tail

log = logging.getLogger("agent_core.vendors.channels")


RECORDS_LIMIT = 200          # максимум, что принимает records/query
THROUGH_SEQUENCE = 100_000   # «всё, что есть» (обязательное поле окна)


class AgentaChannels:
    def __init__(self, *, base_url: str, api_key: str, project_id: str, bot: str, timeout_s: float = 30.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"ApiKey {api_key}"},
            timeout=httpx.Timeout(connect=10.0, read=timeout_s, write=30.0, pool=10.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        self.project_id = project_id
        self.bot = bot
        self.connection_id: str | None = None
        self.agent_id: str | None = None
        self.revision_id: str | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------- setup (идемпотентно) ---------- #

    async def ensure_connection(self, *, slug: str, name: str = "") -> str:
        found = await self._client.post("/channels/connections/query", json={"channel": "agenta"})
        if found.status_code == 200:
            for conn in found.json().get("connections") or []:
                if conn.get("slug") == slug:
                    self.connection_id = conn["id"]
                    return conn["id"]
        created = await self._client.post(
            "/channels/connections/",
            json={"connection": {"channel": "agenta", "slug": slug, "name": name or slug,
                                 "data": {"project": self.project_id, "bot": self.bot}}},
        )
        created.raise_for_status()
        self.connection_id = created.json()["connection"]["id"]
        return self.connection_id

    async def ensure_agent(self, *, workflow_id: str, workflow_revision_id: str, slug: str) -> str:
        if self.connection_id is None:
            raise RuntimeError("ensure_connection() сначала")
        query = await self._client.post("/channels/agents/query", json={"connection_id": self.connection_id})
        current = None
        if query.status_code == 200:
            for agent in query.json().get("agents") or []:
                if agent.get("slug") == slug:
                    current = agent
                    break
        self.revision_id = workflow_revision_id
        refs = {"references": {"workflow": {"id": workflow_id}, "workflow_revision": {"id": workflow_revision_id}}}
        if current is None:
            created = await self._client.post(
                "/channels/agents/",
                json={"agent": {"connection_id": self.connection_id, "slug": slug, "name": slug, "data": refs}},
            )
            created.raise_for_status()
            self.agent_id = created.json()["agent"]["id"]
        else:
            self.agent_id = current["id"]
            if (current.get("data") or {}).get("references") != refs["references"]:
                updated = await self._client.put(
                    f"/channels/agents/{self.agent_id}",
                    json={"agent": {"id": self.agent_id, "data": refs}},
                )
                updated.raise_for_status()
        # default обязателен: без него инбокс-диспетчер отвечает «no resolution»
        default = await self._client.post(f"/channels/agents/{self.agent_id}/default")
        default.raise_for_status()
        return self.agent_id

    # ---------- диалог ---------- #

    async def post_message(self, *, user: str, text: str) -> None:
        response = await self._client.post(
            "/channels/agenta/events/",
            json={"project": self.project_id, "bot": self.bot, "user": user, "text": text},
        )
        response.raise_for_status()

    async def session_ids(self, *, revision_id: str | None = None, user: str | None = None,
                          limit: int = 20) -> list[str]:
        """Свежие сессии канала (новые первыми); опционально — только нужной ревизии и/или ЭТОГО пользователя.

        **Сколько сессий опрашиваем.** До 20 свежих (было 3): при большем числе клиентов ответ
        4-го и далее не находился, и сервис-потребитель молчал до таймаута. Плюс `user` фильтрует
        по имени сессии канала («From user tg-tg:<chat_id> …»), поэтому обычно опрашивается ровно
        одна сессия.
        """
        response = await self._client.post("/sessions/streams/query", json={})
        response.raise_for_status()
        candidates: list[tuple[str, str]] = []
        for stream in response.json().get("streams") or []:
            if not stream.get("session_id"):
                continue
            if user and user not in (stream.get("name") or ""):
                continue
            refs = stream.get("references") or []
            if revision_id is not None and not any((r or {}).get("id") == revision_id for r in refs):
                continue
            if revision_id is None and not any((r or {}).get("key") == "workflow_revision" for r in refs):
                continue
            candidates.append((stream.get("created_at") or "", stream["session_id"]))
        candidates.sort(reverse=True)
        return [session_id for _, session_id in candidates[:limit]]

    async def newest_session_id(self, *, since_ts: float = 0.0, revision_id: str | None = None) -> str | None:
        """Самая свежая сессия канала (совместимость со скриптами)."""
        ids = await self.session_ids(revision_id=revision_id, limit=1)
        return ids[0] if ids else None

    async def read_tail(self, *, session_id: str) -> Tail:
        """Хвост журнала сессии: последний ответ агента и sequence последнего `done`.

        Журнал у сессии канала общий на весь тред, а `limit` — фиксированное окно ОТ НАЧАЛА
        (сдвигается `offset`), поэтому при большом журнале читаем хвост: `count` из ответа даёт
        полный размер. Ответ хода определяем по росту `done`-sequence — это надёжнее, чем
        `done=True`: в переиспользуемой сессии `done` предыдущего хода лежит там же.
        """
        payload = {"session_id": session_id, "windowing": {"limit": RECORDS_LIMIT, "through_sequence": THROUGH_SEQUENCE}}
        response = await self._client.post("/sessions/records/query", json=payload)
        response.raise_for_status()
        data = response.json()
        total = int(data.get("count") or 0)
        records = data.get("records") or []
        if total > len(records):
            payload["windowing"] = {
                "limit": RECORDS_LIMIT, "offset": total - RECORDS_LIMIT, "through_sequence": THROUGH_SEQUENCE,
            }
            response = await self._client.post("/sessions/records/query", json=payload)
            response.raise_for_status()
            data = response.json()
            records = data.get("records") or []

        text, text_seq, done_seq, usage = None, 0, 0, None
        for record in records:
            sequence = int(record.get("sequence") or 0)
            record_type = record.get("record_type")
            attrs = record.get("attributes") or {}
            if record_type == "done":
                done_seq = max(done_seq, sequence)
            elif record_type == "usage" and isinstance(attrs, dict):
                usage = attrs
            elif record_type == "message" and isinstance(attrs.get("text"), str):
                candidate = attrs["text"].strip()
                # эхо входящего сообщения: "From user <id> (agenta id <id>):<текст>" — не ответ
                if candidate and not candidate.startswith("From user ") and sequence > text_seq:
                    text, text_seq = candidate, sequence
        return Tail(text=text, done_seq=done_seq, records=total or len(records), usage=usage)

    async def read_reply(self, *, session_id: str) -> tuple[str | None, bool, int, dict | None]:
        """Совместимость: (текст последнего ответа, был ли завершён ход, записей, usage)."""
        tail = await self.read_tail(session_id=session_id)
        return tail.text, tail.done_seq > 0, tail.records, tail.usage

    def _poll_sessions(self, *, user: str, strict: bool = False):
        """Набор сессий для baseline и опроса: ТОЛЬКО сессия этого пользователя.

        **Чужие сессии здесь не появляются никогда, и это исправление дефекта.** Раньше при
        пустом результате шёл фолбэк «свежие сессии любых пользователей», и он давал две
        беды сразу:

          1. **Ответ не доходил.** Поиск своей сессии идёт с фильтром по закреплённой
             ревизии — кандидат обязан на неё ссылаться. У чата, чья сессия создана под
             ПРЕЖНЕЙ ревизией, результат пуст, фолбэк подставлял чужие сессии, потребитель ждал
             ответ в них и отдавал клиенту запасной текст, хотя агент ответил.
          2. **Мог уйти чужой ответ.** `ask` возвращает первый выросший `done` из найденной
             сессии и отправляет этот текст в текущий чат. С чужими сессиями это означало
             возможную отправку одному клиенту ответа, предназначенного другому.

        Поэтому порядок такой: сначала своя сессия под текущей ревизией, затем **своя же**
        без фильтра ревизии (это и лечит случай 1). Имя пользователя обязательно в обоих
        запросах, а «свежие любые» не запрашиваются вовсе.

        `strict=True` сохраняет прежний смысл для служебных уведомлений:
        опрашиваем только сессию текущей ревизии. Отсутствие своей сессии не проблема —
        цикл опроса перечитывает список каждый тик, а первое сообщение создаёт сессию.
        """

        async def load() -> list[str]:
            found = await self.session_ids(revision_id=self.revision_id, user=user)
            if found or strict:
                return found
            return await self.session_ids(user=user)

        return load

    async def ask(self, *, user: str, text: str, wait_s: float = 60.0, poll_s: float = 3.0,
                  strict_user: bool = False) -> Reply:
        """Полный цикл: событие → ожидание ответа ИМЕННО этого хода.

        Признак ответа — рост sequence последнего `done` относительно baseline, снятого ДО отправки.
        Без этого возвращается реплика предыдущего шага: сессия канала переиспользуется на весь тред,
        и её старый `done` «готов» ещё до того, как агент ответит на новое сообщение.
        """
        started = time.time()
        load_sessions = self._poll_sessions(user=user, strict=strict_user)
        sessions = await load_sessions()
        log.info("канал: сессии для %s: %s", user, [sid[:8] for sid in sessions])
        baseline = {session_id: (await self.read_tail(session_id=session_id)).done_seq
                    for session_id in sessions}
        await self.post_message(user=user, text=text)
        while time.time() - started < wait_s:
            await asyncio.sleep(poll_s)
            for session_id in await load_sessions():
                tail = await self.read_tail(session_id=session_id)
                if not tail.text or tail.done_seq <= baseline.get(session_id, 0):
                    continue  # ответа этого хода ещё нет
                log.info(
                    "канал: ответ за %.1f с (сессия %s, записей %s)", time.time() - started, session_id[:8], tail.records
                )
                return Reply(session_id=session_id, text=tail.text, done=True, records=tail.records, usage=tail.usage)
        log.warning("канал: ответа нет за %.0f с (user=%s)", wait_s, user)
        return Reply(session_id="", text="", done=False, records=0)
