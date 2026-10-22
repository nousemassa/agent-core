"""Порт РАНТАЙМА АГЕНТА: кто исполняет ход — платформа или мы сами.

**Зачем порт.** Сегодня ход диалога исполняет Agenta: наш код только постит событие и ждёт ответ
(`agent_core/vendors/channels/agenta.py`). Пока рантайм один, это невидимо. Но у нас ДВА вендора
наблюдаемости, и требование владельца — «рантаймы должны быть реализованы для обоих и переключаться в
зависимости от выбранного; рантайм agenta не должен ломаться». Порт и есть та единственная точка, через
которую сайты говорят с рантаймом; всё остальное (промпты, память, инструменты, цена) — дело реализации.

**Почему порт ШИРЕ, чем `ask`.** Разведка по коду показала: жизненный цикл не пустой —
вызывающий код на старте зовёт `ensure_connection`/`ensure_agent`, а на остановке `aclose`. И без `ensure_agent`
`ask` не падает: он МОЛЧА таймаутится, потому что опрос идёт по `revision_id`, который ставится только
там (`vendors/channels/agenta.py`). Порт, ограниченный `ask`, был бы неполон ровно в самом опасном
месте — там, где отказ выглядит как «нет ответа».

**Почему `ensure_agent` нельзя звать из `ask`.** Это ГЛОБАЛЬНЫЙ побочный эффект: `PUT` перезакрепляет
агента всем ходам — такой вызов перезакрепляет агента глобально. Поэтому привязка живёт в `ensure_agent` жизненного цикла,
а не в каждом вызове.

**Форма ответа.** `AgentReply` объявляет ровно те поля, которые читают сайты (`text`, `done`, `usage`,
`session_id`; `records` — для скриптов). ❗ `usage` — СЛОВАРЬ платформенной формы (`cost/input/output/total`):
именно так его читают отчёты и витрины, и реализация «own» обязана привести свои данные к этой
форме, иначе цена молча обнулится.

**Форма отказа НЕ переопределяется портом.** Сегодня транспорт бросает на не-2xx и возвращает `done=False`
на таймауте; сайты уже написаны под это. Порт требует от ЛЮБОЙ реализации ТОЙ ЖЕ формы: не бросать там, где
старая реализация не бросала, и наоборот.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class RoleSpec:
    """Параметры роли агента: как к ней обращаться и чем исполнять.

    Роль объявляет продукт: ядро не знает ни имён ролей, ни их каналов. `own_supported` и
    `history_limit` — свойства роли для своего рантайма, а не настройка ядра.
    """

    name: str
    bot: str = ""
    connection_slug: str = ""
    connection_name: str = ""
    workflow_id: str = ""
    revision_id: str = ""
    prompt_path: str = ""
    price_in: float = 0.0
    price_out: float = 0.0
    own_supported: bool = True
    history_limit: int = 0


@dataclass(frozen=True)
class AgentReply:
    """Ответ хода. Заморожен намеренно: сайты присваивают поля соседним объектам, а не этому."""

    text: str
    done: bool
    usage: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    records: int = 0


@runtime_checkable
class AgentRuntime(Protocol):
    """Что обязан уметь рантайм. Реализации: `platform_api.PlatformRuntime` (делегат), `own.OwnRuntime`."""

    async def ensure_connection(self, *, slug: str = "") -> None:
        """Привязка канала. Идемпотентна; зовётся в lifespan, не в `ask`."""

    async def ensure_agent(self, *, workflow_id: str, workflow_revision_id: str, slug: str) -> None:
        """Привязка агента к ревизии. ❗ Глобальный побочный эффект — только в lifespan."""

    async def ask(
        self,
        *,
        user: str,
        text: str,
        wait_s: float = 60.0,
        poll_s: float = 3.0,
        strict_user: bool = False,
    ) -> Any:
        """Исполнить ход и вернуть ответ. Форма `AgentReply`-совместимая."""

    async def aclose(self) -> None:
        """Закрыть соединения. Зовётся на остановке службы."""
