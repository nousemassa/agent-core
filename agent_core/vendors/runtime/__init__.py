"""Рантайм агента: порт, две реализации и выбор оси.

`platform` — делегат над портом канального пути (поведение прежнее), `own` — свой харнесс для роли, которая
его объявила (промпт из файла, история реплик из базы, один вызов модели). Смысл не в том, чтобы «было
два класса», а в том, чтобы у сайтов была ОДНА точка разговора с рантаймом: при смене оси сайты не
меняются.
"""

from agent_core.vendors.runtime.own import OwnRuntime
from agent_core.vendors.runtime.platform_api import PlatformRuntime
from agent_core.vendors.runtime.port import AgentReply, AgentRuntime, RoleSpec
from agent_core.vendors.runtime.select import DEFAULT, RUNTIMES, resolve, runtime_for

__all__ = [
    "DEFAULT",
    "RUNTIMES",
    "AgentReply",
    "AgentRuntime",
    "OwnRuntime",
    "PlatformRuntime",
    "RoleSpec",
    "resolve",
    "runtime_for",
]
