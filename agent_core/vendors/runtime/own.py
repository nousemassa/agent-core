"""Рантайм `own`: ход исполняем МЫ — свой харнесс, без платформы Agenta.

**Что реализовано и для каких ролей.** Каждый ход клиента и ход по готовому набору полей (второй
случай — текст плюс описание вложения). Общее у них: данные хода целиком в тексте сообщения и НИ
ОДНОГО инструмента — диалогу данные приходят блоком состояния (`channels/turn_context.py`), второму
случаю — поля запроса, а вложение разбирает сервис-потребитель (`vision`), а не агент. Роль с
собственным графовым харнессом отказывает громко: там цикл инструментов, и «сделать вид, что
работает» здесь было бы хуже отказа.

**Чем роли РАЗЛИЧАЮТСЯ** (и почему это таблица, а не два куска кода): своя настройка пути к файлу
агента, свои цены за токены (модели и тарифы могут не совпадать) и своя история — у диалога окно
последних реплик, у второго случая её нет по смыслу (каждый ход начинается с нового треда).

**Откуда берётся ход:**
* системный промпт, `temperature` и `max_tokens` — из ФАЙЛА агента (шапка), единственного источника
  (`vendors/runtime/prompt_spec.py`); путь приходит настройкой РОЛИ (`<РОЛЬ>_PROMPT_PATH`), и файл
  лежит в образе;
* история реплик — из `dialog_messages` через загрузчик, который ставит сервис-потребитель
  (`history_loader`). ❗ Загрузчик не открывает своё соединение с базой: у потребителя уже есть пул,
  и второй пул в том же процессе — это две правды о подключении и лишние соединения;
* текст текущего хода приходит от сайта как `text` и уходит в модель без правок: маскирование телефона
  к этому моменту уже сделано вызывающим кодом.

**Форма ответа и отказа — как у платформы** (это контракт, а не вкус):
* успех: `done=True`, `text`, `usage` СЛОВАРЁМ `{input,output,total,cost}`, `session_id`;
* дедлайн (не дождались): `done=False`, пустой текст — платформа на таймауте тоже НЕ бросает;
* провайдер ответил ошибкой: БРОСАЕМ `RuntimeUnavailable` — как канальный путь бросает на не-2xx;
* `strict_user` принимается и **названно игнорируется**: у своего рантайма нет чужих сессий, поэтому
  флаг нечего ограничивать. Молчаливое игнорирование было бы неотличимо от «флаг учли».

**Чего этот файл НЕ делает.** Не пишет историю (её пишет САЙТ — иначе таблица заполнялась бы только на
своём рантайме и переключение рвало бы контекст в одну сторону), не дедуплицирует входящие (это
транспорт потребителя) и не лечит провайдера (ретраи — в `provider.py`, с дедлайном хода).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from agent_core.vendors.runtime import provider
from agent_core.vendors.runtime.port import AgentReply, RoleSpec
from agent_core.vendors.runtime.prompt_spec import PromptSpec, PromptSpecError, spec_for

log = logging.getLogger(__name__)

#: Свойства роли приходят из `RoleSpec`: поддерживает ли роль этот рантайм и сколько реплик истории
#: читать. Ядро не знает имён ролей.


class OwnRuntime:
    """Свой харнесс хода: промпт из репозитория, история из базы, один вызов модели."""

    #: Имя для журналов и для `/health` — совпадает со значением оси `AGENT_RUNTIME`.
    name = "own"

    def __init__(self, *, role: RoleSpec, settings: Any = None) -> None:
        if not role.own_supported:
            raise NotImplementedError(
                f"рантайм `own` не реализован для роли {role.name!r}. Для этой роли остаётся "
                f"AGENT_RUNTIME=platform; расширять own — отдельным шагом, а не тихой подменой."
            )
        self._role = role
        self._agent = role.name
        self._settings = settings
        #: Окно истории этой роли. Ноль — «истории нет по смыслу», а не «не настроили».
        self._history_limit = role.history_limit
        self._spec: PromptSpec | None = None
        #: Загрузчик истории ставит потребитель (у него есть пул базы). Без него ход идёт с ПУСТЫМ окном —
        #: и это названо в журнале, а не выглядит как «истории не было».
        self.history_loader: Callable[[str, int], Awaitable[list[dict[str, str]]]] | None = None
        self._history_warned = False
        #: Транспорт провайдера. Пусто — `provider.chat` создаёт свой на один ход. Шов нужен замерам и
        #: тестам: подменять сеть честнее, чем подменять наш собственный вызов.
        self.http_client: Any = None
        #: Клиент, который рантайм создаёт САМ и держит между ходами (пусто, пока хода не было).
        self._own_http: Any = None
        #: Метка ревизии для отчёта о паритете: какие ключи рабочего процесса действовали в процессе.
        self.revision_label: dict[str, str] = {}

    @property
    def agent(self) -> str:
        return self._agent

    @property
    def ready(self) -> bool:
        """Готов ли рантайм исполнять ход. Читается `/health`, чтобы «какой рантайм работает» было видно."""
        return self._spec is not None

    @property
    def spec(self) -> PromptSpec | None:
        return self._spec

    async def ensure_connection(self, *, slug: str = "") -> None:
        """Проверка готовности на старте: спека читается, ключ есть, цены названы.

        ❗ Это не привязка канала (привязывать нечего), а ПЕРЕНОС отказа на старт: иначе первый ход
        клиента выяснял бы, что файл промпта не доехал в образ или ключ провайдера пуст, — и клиент
        получал бы заготовку вместо ответа.
        """
        path = self._role.prompt_path
        if not path:
            raise PromptSpecError(
                f"свой рантайм включён (AGENT_RUNTIME=own), но путь к файлу агента роли {self._agent!r} пуст. "
                f"Задайте путь промпта роли: в контейнере — путь в образе, на хосте — путь из лончера. "
                f"Угадывать путь я не буду."
            )
        self._spec = spec_for(path)
        base_url, api_key, model = self._settings.chat_credentials()
        if not (base_url and api_key):
            raise provider.RuntimeUnavailable(
                "свой рантайм включён, но провайдер модели не настроен: `chat_credentials()` пуст. "
                "Задайте LLM_PROVIDER и ключ этого провайдера — иначе ход не исполним в принципе."
            )
        price_in, price_out = self._role.price_in, self._role.price_out
        if not (price_in or price_out):
            # Не отказ: ход исполнить можно. Но цену мы не знаем, и это надо СКАЗАТЬ: ноль в отчёте
            # читался бы как «ход бесплатный».
            log.warning("own: цены роли %s не заданы, цена хода будет НЕИЗВЕСТНОЙ (cost=None), "
                        "а не нулевой", self._agent)
        log.info("own: готов — промпт %s, модель %s, temperature %s, max_tokens %s",
                 self._spec.path.name, model, self._spec.temperature, self._spec.max_tokens)

    async def ensure_agent(self, *, workflow_id: str, workflow_revision_id: str, slug: str) -> None:
        """У своего рантайма нет агента на платформе — ключи сохраняются как МЕТКА для отчёта.

        Смысл честно другой: не «привязка», а «чем этот процесс считал себя» — чтобы в замере паритета
        было видно, с какой ревизией платформы сравнивали.
        """
        self.revision_label = {"workflow_id": workflow_id, "revision_id": workflow_revision_id, "slug": slug}

    async def ask(
        self,
        *,
        user: str,
        text: str,
        wait_s: float = 60.0,
        poll_s: float = 3.0,
        strict_user: bool = False,
    ) -> AgentReply:
        """Исполнить ход. `poll_s` и `strict_user` у своего рантайма смысла не имеют — названно."""
        if self._spec is None:
            # Рантайм собрали, но `ensure_connection` не звали: отказ громкий, а не «пустой ответ».
            raise provider.RuntimeUnavailable(
                "own: ход до проверки готовности (ensure_connection не вызывался). "
                "Это ошибка сборки сервиса, а не клиента."
            )
        history = await self._history(user)
        try:
            result = await provider.chat(
                settings=self._settings,
                spec=self._spec,
                history=history,
                text=text,
                wait_s=wait_s,
                #: Цены читаем НА ХОДУ, а не из `ensure_connection`: настройку могли поправить и
                #: пересоздать процесс без пересборки рантайма; цена — свойство роли, а не сессии.
                prices=(self._role.price_in, self._role.price_out),
                client=self.http_client or self._client(),
            )
        except provider.RuntimeTimeout as error:
            # Форма платформы: не дождались — не бросаем, а отдаём `done=False` (сайт подставит текст).
            log.warning("own: дедлайн хода (%.0f с) — %s", wait_s, error)
            return AgentReply(text="", done=False, usage={}, session_id="", records=0)
        if not result.text:
            # Заплатили, но модели нечего сказать. `usage` заполнен: деньги потрачены, и скрывать это
            # нельзя — иначе отчёт покажет бесплатный ход.
            return AgentReply(text="", done=False, usage=result.usage, session_id="", records=0)
        return AgentReply(
            text=result.text,
            done=True,
            usage=result.usage,
            #: `user` — ключ порта: по нему `turn_traces.session_id` группирует ходы одного клиента.
            session_id=user,
            records=1,
        )

    async def _history(self, user: str) -> list[dict[str, str]]:
        """Окно истории из базы. У роли без истории — пусто МОЛЧА; у роли с историей без загрузчика —
        пусто и сказано один раз (иначе «истории не было» неотличимо от «не смогли прочитать»)."""
        if self._history_limit <= 0:
            return []
        if self.history_loader is None:
            if not self._history_warned:
                log.warning("own: история реплик недоступна (рантайм собран без загрузчика) — "
                            "ход идёт с пустым окном; уточнения вида «второй» могут быть неверно поняты")
                self._history_warned = True
            return []
        return await self.history_loader(user, self._history_limit)

    async def aclose(self) -> None:
        """Закрыть транспорт рантайма.

        ❗ Подставленный извне (`http_client`) — не наш: его закрывает тот, кто поставил (замеры и тесты).
        Свой клиент держим между ходами, поэтому закрывать его обязан именно этот метод: иначе процесс
        оставил бы открытый пул соединений.
        """
        if self._own_http is not None:
            await self._own_http.aclose()
            self._own_http = None

    def _client(self) -> Any:
        """Клиент провайдера на РАНТАЙМ, а не на ход: иначе каждый ход — новый TCP+TLS.

        Кэш живёт на экземпляре, а не на модуле: клиент привязан к петле, в которой создан, и общий кэш
        ломал бы тесты, где каждая проверка идёт своим `asyncio.run`. Закрытый клиент пересоздаём —
        иначе после остановки/падения пула ход упал бы в `RuntimeError` мимо всех `except`.
        """
        if self._own_http is None or self._own_http.is_closed:
            self._own_http = provider.make_client()
        return self._own_http
