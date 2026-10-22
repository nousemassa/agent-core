"""Порт канального пути платформы: что ядро ждёт от реализации.

**Зачем порт.** Контракт канального пути принадлежит платформе (её HTTP-пути, её имя канала, её
авторизация), а ядро этот контракт знать не должно: иначе вторая платформа потребует правок в общем слое,
а не нового адаптера. Здесь остаётся только набор операций, которыми пользуются потребители.

**Почему порт шире, чем «спросить агента».** Жизненный цикл (создать connection/agent, выставить default)
и чтение журнала сессии нужны вызывающему коду напрямую; порт, ограниченный одним `ask`, был бы неполон
именно там, где ошибки дороже всего.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Tail:
    """Хвост журнала сессии: ответ, sequence последнего завершённого хода и размер журнала."""

    text: str | None
    done_seq: int   # 0 — в сессии ещё не было завершённого хода
    records: int
    usage: dict | None = None


@dataclass(frozen=True)
class Reply:
    """Ответ канала: текст, признак завершения и служебные числа хода."""

    session_id: str
    text: str
    done: bool
    records: int
    usage: dict | None = None


class ChannelPath(Protocol):
    """Операции канального пути, которые зовут потребители (рантайм, инструменты, тесты)."""

    async def aclose(self) -> None: ...

    async def ensure_connection(self, *, slug: str, name: str = "") -> str: ...

    async def ensure_agent(self, *, workflow_id: str, workflow_revision_id: str, slug: str) -> str: ...

    async def post_message(self, *, user: str, text: str) -> None: ...

    async def session_ids(self, *, revision_id: str | None = None, user: str | None = None,
                          limit: int = 50) -> list[str]: ...

    async def newest_session_id(self, *, since_ts: float = 0.0, revision_id: str | None = None) -> str | None: ...

    async def read_tail(self, *, session_id: str) -> Tail: ...

    async def read_reply(self, *, session_id: str) -> tuple[str | None, bool, int, dict | None]: ...

    async def ask(self, *, user: str, text: str, wait_s: float = 60.0, poll_s: float = 3.0,
                  revision_id: str | None = None) -> Reply: ...
