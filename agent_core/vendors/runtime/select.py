"""Выбор рантайма: `AGENT_RUNTIME` (ось рантайма), с безопасным значением по умолчанию.

**Значения оси:**
* `` (пусто) — как сегодня: рантайм `platform`. Поведение не меняется, пока паритет `own` не измерен.
* `platform` — явно платформа (СПОСОБ, а не бренд — то же правило, что у класса `PlatformRuntime`).
* `own` — наш харнесс (реализован для роли `dialog`; для прочих ролей — ГРОМКИЙ отказ, см. `own.py`).
* `auto` — по выбранному вендору: `PLATFORM_VENDOR=openlit` ⇒ `own`, иначе `platform`.

**Почему по умолчанию НЕ `auto`.** Требование владельца — «переключаться в зависимости от выбранного
вендора»; это значение `auto`. Но включать его раньше замера паритета нельзя: при `PLATFORM_VENDOR=openlit`
контур молча получил бы НЕДОДЕЛАННЫЙ рантайм. Поэтому механизм есть сразу (и проверяется явным
`AGENT_RUNTIME=own`), а смена поведения — отдельный шаг после паритета (фаза F).

**Почему неизвестное значение — отказ.** Тот же урок, что у `vendors.select`: опечатка в оси должна падать,
иначе спаны/ходы уходят не туда, и по журналу это неотличимо от «всё хорошо».
"""

from __future__ import annotations

import logging
from typing import Any

from agent_core.vendors.runtime.own import OwnRuntime
from agent_core.vendors.runtime.platform_api import PlatformRuntime
from agent_core.vendors.runtime.port import RoleSpec

log = logging.getLogger(__name__)

#: Значения `AGENT_RUNTIME`.
#: ❗ `platform`, а не `agenta`: ось называет СПОСОБ исполнения (через API платформы), а не бренд —
#: то же правило, что у `PlatformRuntime` рядом. Значения `own` (свой харнесс) и `auto` (по вендору)
#: остаются.
RUNTIMES = ("platform", "own", "auto")
DEFAULT = "platform"


def resolve(settings: Any) -> str:
    """Какое имя рантайма действует. Отдельно от `runtime_for`, чтобы это можно было проверить без сборки."""
    axis = (getattr(settings, "agent_runtime", "") or "").strip().lower()
    #: ❗ Пустая ось — это «не задано»: то же, что значение по умолчанию. Проверять её как опечатку
    #: нельзя (в `.env` ключ часто оставляют пустым) — иначе включение оси сломало бы всем пуск.
    if axis and axis not in RUNTIMES:
        raise ValueError(
            f"неизвестный рантайм агента {axis!r}: известны {', '.join(RUNTIMES)} "
            f"(пусто = {DEFAULT}). Молчаливый откат соврал бы: ход исполнил бы не тот рантайм."
        )
    if axis == "auto":
        vendor = (getattr(settings, "platform_vendor", "") or "agenta").strip().lower()
        return "own" if vendor == "openlit" else "platform"
    return axis or DEFAULT


def runtime_for(settings: Any, *, role: RoleSpec) -> PlatformRuntime | OwnRuntime:
    """Собрать рантайм для роли. Параметры роли приходят из `RoleSpec`, а не из имён настроек.

    ❗ Возвращается ДОЛГОЖИВУЩИЙ объект: у рантайма есть состояние (`revision_id`), и создавать его на
    каждый ход нельзя — привязка агента делается один раз в lifespan.
    """
    name = resolve(settings)
    if name == "own" and not role.own_supported:
        #: ❗ НАЗВАННЫЙ откат, а не тихая подмена: роль исполняется платформой, и об этом говорится в
        #: журнале — иначе «ось включена» означало бы разное для разных ролей без следа в логах.
        log.warning("рантайм `own` не реализован для роли %s — роль исполняет платформа "
                    "(названный откат, а не молчаливая подмена)", role.name)
        name = "platform"
    if name == "own":
        runtime = OwnRuntime(role=role, settings=settings)
        runtime.connection_slug = role.connection_slug  # type: ignore[attr-defined]
        runtime.workflow_id = role.workflow_id          # type: ignore[attr-defined]
        runtime.revision_id = role.revision_id          # type: ignore[attr-defined]
        return runtime

    #: Импорт адаптера — здесь, а не на уровне модуля: он тянет httpx, и порт не должен навязывать эту
    #: зависимость тому, кто просто спрашивает имя рантайма. Реализацию выбирает ОСЬ ВЕНДОРА: у второго
    #: вендора канального пути нет, и это отказ, а не тихое использование чужого контракта.
    if settings.platform_vendor == "agenta":
        from agent_core.vendors.channels.agenta import AgentaChannels as ChannelsClient
    else:
        raise ValueError(
            f"канальный путь: у вендора {settings.platform_vendor!r} нет реализации — "
            f"для него доступен только рантайм `own`"
        )

    client = ChannelsClient(
        base_url=settings.platform_api_url,
        api_key=settings.platform_api_key,
        project_id=settings.platform_project_id,
        bot=role.bot,
    )
    runtime = PlatformRuntime(client=client)
    #: Параметры жизненного цикла кладём рядом: сайты зовут `ensure_connection`/`ensure_agent` теми же
    #: аргументами, что и раньше, а значения берутся из одного места — из роли.
    runtime.connection_slug = role.connection_slug  # type: ignore[attr-defined]
    runtime.connection_name = role.connection_name  # type: ignore[attr-defined]
    runtime.workflow_id = role.workflow_id          # type: ignore[attr-defined]
    runtime.revision_id = role.revision_id          # type: ignore[attr-defined]
    return runtime
