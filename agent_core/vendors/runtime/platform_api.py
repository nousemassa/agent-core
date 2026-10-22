"""Рантайм `agenta`: ход исполняет платформа. ТОНКИЙ ДЕЛЕГАТ — поведение не меняется.

**Зачем этот файл вообще.** Требование владельца: «рантайм agenta не должен ломаться». Самый надёжный
способ это обеспечить — не переписывать рабочий путь, а обернуть его: каждый метод делегирует в
`ChannelPath` ровно тем же вызовом с теми же аргументами. Любое расхождение здесь = регресс, поэтому
`tests/test_runtime_port.py` проверяет делегирование по каждому методу.

**Почему НЕ копия логики.** Копия (переписанный `ensure_connection`/`ask`) означала бы второй источник
правды для сетевого протокола платформы: копия разъезжается молча, и расхождение видно только в
аварию.
"""

from __future__ import annotations

from typing import Any


class PlatformRuntime:
    """Делегат над портом канального пути. Ничего не решает сам — только передаёт вызов."""

    #: Имя для журналов и для проверки «какой рантайм работает».
    #: Совпадает со значением оси `AGENT_RUNTIME` (`platform`): ось говорит СПОСОБ, а не бренд.
    name = "platform"

    def __init__(self, *, client: Any) -> None:
        self._client = client

    @property
    def client(self) -> Any:
        """Доступ к клиенту: нужен местам, которые ещё не переведены на порт (переходный период)."""
        return self._client

    async def ensure_connection(self, *, slug: str = "") -> None:
        """Привязка канала. Пустой `slug` — отказ, а не пустая строка в теле запроса.

        ❗ Передача пустой строки «как есть» уезжала в тело создания соединения, и платформа отвечала
        **422**. Делегат не ПОДМЕНЯЕТ аргумент: slug приходит из роли.
        """
        if not slug:
            raise ValueError("ensure_connection: пустой slug — привязка канала без имени невозможна")
        await self._client.ensure_connection(slug=slug, name=getattr(self, "connection_name", "") or slug)

    async def ensure_agent(self, *, workflow_id: str, workflow_revision_id: str, slug: str) -> None:
        await self._client.ensure_agent(
            workflow_id=workflow_id,
            workflow_revision_id=workflow_revision_id,
            slug=slug,
        )

    async def ask(
        self,
        *,
        user: str,
        text: str,
        wait_s: float = 60.0,
        poll_s: float = 3.0,
        strict_user: bool = False,
    ) -> Any:
        #: ❗ Ни аргументы, ни форма ответа/отказа не меняются: это ровно тот вызов, что был на сайтах.
        return await self._client.ask(
            user=user,
            text=text,
            wait_s=wait_s,
            poll_s=poll_s,
            strict_user=strict_user,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
