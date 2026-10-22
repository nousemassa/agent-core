"""Подключение к Telegram: ОДИН механизм на все процессы проекта.

**Архитектурное правило: механизм, нужный двум потребителям, живёт в одном месте и имеет один вход
обращения.** Копия в двух процессах расходится в поведении: у одного появляются перебор прокси и сторож
соединения, у другого остаётся однократная попытка — и после перезагрузки хоста оба стоят выключенными
при отвечающем `/health`.

**Что здесь есть:** массив MTProto-прокси с перебором по очереди, файл-сессия (рестарт и повтор не
тратят bot-логин — Telegram отвечает на это FloodWait), ожидание готовности маршрута при старте и
сторож, который держит соединение после обрыва.

**Чего здесь нет:** ничего про то, ЧТО делать после подключения. Команды, приветствие, регистрация
обработчиков и фоновые петли — дело потребителя, он передаёт это одной функцией `on_connected`.
Так у механизма одна причина меняться, а у потребителей — своя.

Второй вход обращения к тому же механизму запрещён проверкой в CI
(проверка архитектуры проекта): «построить клиента» можно ровно в одном модуле.
"""
from __future__ import annotations

import asyncio
import logging
import pathlib
from collections.abc import Awaitable, Callable

from agent_core import startup
from agent_core.tg.mtproto import build_client, mtproxies

log = logging.getLogger(__name__)

# Файл-сессии (ключи доступа к Telegram) лежат ВНЕ кода и в git не попадают (`.gitignore: .sessions/`).
# ❗ Каталог берётся из НАСТРОЙКИ (`SESSIONS_DIR`), а не из расположения ЭТОГО файла: общий слой живёт в
# установленном пакете, и `parents[…]/".sessions"` ведёт внутрь `site-packages`, где запись отказывает
# `PermissionError: '/usr/local/lib/python3.13/site-packages/.sessions'` — и бот не подключается молча.
# Угадывать путь нельзя: неверный = бот не подключится.

# Как часто сторож проверяет, жив ли Telegram. Проверка дешёвая (`is_connected()` — локальное
# состояние), поэтому тик короткий: цена ошибки — молчащий бот.
WATCHDOG_TICK_S = 30.0
# Бюджет одной попытки переподключения. Больше бюджета старта: во время обрыва сети ждать дольше
# полезнее, чем чаще дёргать прокси.
WATCHDOG_BUDGET_S = 90.0

# Что потребитель делает после успешного подключения. `first=True` — это старт, `False` — повтор
# после обрыва: приветствие и разовые действия уместны только на старте.
OnConnected = Callable[[bool], Awaitable[None]]


class TelegramConnection:
    """Подключение к Telegram с перебором прокси, ожиданием готовности и сторожем.

    Один экземпляр на процесс. Потребитель читает `client`, спрашивает `running` и передаёт свою
    функцию `on_connected`.
    """

    def __init__(self, *, settings, name: str, session_name: str, token: str,
                 start_budget_s: float | None = None) -> None:
        self._settings = settings
        # Имя нужно только для журнала: роли пишут в разные файлы, и по строке
        # «соединение восстановлено» должно быть ясно, у кого именно.
        self._name = name
        self._session_path = pathlib.Path(settings.sessions_dir) / f"{session_name}.session"
        self._token = token
        # Бюджет ожидания на старте — параметр, а не значение по умолчанию `wait_ready`: умолчание
        # вычисляется при импорте, и настроить его (в том числе в проверке) было бы нельзя.
        self._start_budget_s = start_budget_s
        self.client = None
        self.me = None
        self._proxy_index = 0
        self._proxy_last_ok = 0
        self._connected_proxy: tuple[str, int, str] | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._on_connected: OnConnected | None = None

    @property
    def running(self) -> bool:
        """Соединение живо прямо сейчас. Это же поле видит `/health`."""
        return bool(self.client and self.client.is_connected())

    @property
    def proxy(self) -> tuple[str, int, str] | None:
        """Прокси действующего соединения — для журнала (секрет в журнал не попадает)."""
        return self._connected_proxy

    # ---------- подключение ----------

    async def _connect_one(self, proxy: tuple[str, int, str] | None) -> None:
        """Одна попытка через КОНКРЕТНЫЙ прокси. Бросает при неудаче."""
        client = build_client(self._settings, session_path=self._session_path, proxy=proxy)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.start(bot_token=self._token)
            me = await client.get_me()
            if me is None:
                raise ConnectionError("get_me() вернул пусто — соединение не подтверждено")
        except Exception:
            # Неудачная попытка не оставляет полуклиента: следующая запись массива строится с нуля,
            # а висящее соединение только съедало бы сокеты.
            try:
                await client.disconnect()
            except Exception as error:  # noqa: BLE001 — ошибка отключения не подменяет причину отказа
                log.debug("%s: отключение неудачной попытки не удалось (%s)", self._name,
                          type(error).__name__)
            raise
        previous, self.client = self.client, client
        if previous is not None and previous is not client:
            try:
                await previous.disconnect()
            except Exception as error:  # noqa: BLE001 — старое соединение уже не нужно
                log.debug("%s: прежнее соединение закрылось с ошибкой (%s)", self._name,
                          type(error).__name__)
        self._connected_proxy = proxy
        self.me = me

    async def connect(self) -> None:
        """Перебрать прокси ПО ОЧЕРЕДИ, начиная с последнего удачного. Бросает, если не вышел никто.

        Начинать с удачного — не оптимизация ради красоты: иначе каждый повтор ждал бы таймаут на
        заведомо мёртвой записи массива.
        """
        proxies = mtproxies(self._settings)
        if not proxies:
            await self._connect_one(None)  # прокси не задан — работаем напрямую
            return
        start = self._proxy_index % len(proxies)
        problems: list[str] = []
        for offset in range(len(proxies)):
            index = (start + offset) % len(proxies)
            proxy = proxies[index]
            try:
                await self._connect_one(proxy)
            except Exception as error:  # noqa: BLE001 — пробуем следующую запись массива
                problems.append(f"{proxy[0]}:{proxy[1]} — {type(error).__name__}: {error}")
                log.warning("%s: прокси %s:%s не подошёл (%s: %s), пробую следующую запись массива",
                            self._name, proxy[0], proxy[1], type(error).__name__, error)
                continue
            self._proxy_last_ok = index
            self._proxy_index = index
            log.info("%s: подключение через прокси %s:%s (запись %d из %d)",
                     self._name, proxy[0], proxy[1], index + 1, len(proxies))
            return
        # Причины по КАЖДОЙ записи: «не подключилось» без перечня неотличимо от отказа сети.
        raise ConnectionError("ни один прокси из массива не подошёл: " + " | ".join(problems))

    async def start(self, on_connected: OnConnected) -> None:
        """Дождаться подключения и поднять сторож. НЕ выключает бота при неудаче.

        Готовность ждём тем же механизмом, которым сервис ждёт БД и каналы платформы: после перезагрузки
        хоста маршрут наружу поднимается позже приложения, и одиночная попытка означала бы «бот
        выключен» без повторов. Исчерпание бюджета — не повод сдаваться: сторож продолжит попытки.
        """
        # Каталог сессий проверяем ДО попыток подключения: если он не тот, попытки бессмысленны, и причина
        # должна быть названа прямо (иначе это выглядит как «прокси не подошёл» и уводит в сторону).
        directory = self._session_path.parent
        if not directory.is_dir():
            log.error(
                "%s: каталога файлов-сессий НЕТ: %s (настройка SESSIONS_DIR=%r). Подключение не начинаю: "
                "неверный путь означает, что бот не подключится, и повторять это бессмысленно. В контейнере "
                "каталог приходит монтированием, на хосте его задаёт лончер.",
                self._name, directory, self._settings.sessions_dir,
            )
            return
        self._on_connected = on_connected
        self._proxy_index = self._proxy_last_ok
        budget = self._start_budget_s if self._start_budget_s is not None else startup.WAIT_BUDGET_S
        try:
            await startup.wait_ready(self.connect, what=f"Telegram ({self._name}, MTProto)",
                                     budget_s=budget)
        except Exception as error:  # noqa: BLE001 — бюджет исчерпан, но бот не выключаем
            log.error(
                "%s: не подключился за отведённое время (%s: %s). Продолжаю попытки в фоне; "
                "остальные функции работают. Проверьте TELEGRAM_MT_PROXIES (ссылки t.me/proxy или "
                "тройки сервер:порт:секрет), TELEGRAM_API_ID/HASH и токен.",
                self._name, type(error).__name__, error,
            )
        else:
            await self._after_connect(first=True)
        self._watchdog_task = asyncio.create_task(self._watchdog())

    async def _after_connect(self, *, first: bool) -> None:
        """Что делаем после успешного подключения: сначала общее, потом — потребителя."""
        if not first:
            log.warning("%s: соединение восстановлено, бот снова принимает сообщения", self._name)
        if self._on_connected is not None:
            await self._on_connected(first)

    async def _watchdog(self) -> None:
        """Держит соединение: проверяет и переподключается.

        Отдельно от сторожа-таймера: тот умеет только СООБЩИТЬ, причём каналом доставки служит тот же
        Telegram, поэтому при недоступном Telegram алерт уходит в маркер-файл. Здесь бот лечит себя сам.
        """
        while True:
            try:
                await asyncio.sleep(WATCHDOG_TICK_S)
                if self.running:
                    continue
                log.warning("%s: соединение потеряно — переподключаюсь", self._name)
                await startup.wait_ready(self.connect, what=f"Telegram ({self._name}, повтор)",
                                         budget_s=WATCHDOG_BUDGET_S)
                await self._after_connect(first=False)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 — сторож не должен падать от неудачи
                log.warning("%s: переподключение не удалось (%s: %s)",
                            self._name, type(error).__name__, error)

    async def stop(self) -> None:
        """Снять сторож и закрыть соединение. Остановка не должна падать."""
        task, self._watchdog_task = self._watchdog_task, None
        if task is not None:
            task.cancel()
        client, self.client = self.client, None
        self._connected_proxy = None
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception as error:  # noqa: BLE001
            log.warning("%s: отключение с ошибкой (%s)", self._name, type(error).__name__)
