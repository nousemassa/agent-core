"""Конфигурация ОБЩЕГО СЛОЯ: только то, что нужно любому продукту.

Доменные настройки (боты, календарь, напоминания, лимиты продукта) объявляются в настройках продукта:
Настройки продукта наследуют `CoreSettings` и добавляют свои поля.
"""
from __future__ import annotations

import os
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _env_files() -> tuple[str, ...]:
    """Файлы окружения в порядке приоритета: позже прочитанный перекрывает раньше прочитанный."""
    return tuple(f for f in os.environ.get("AGENT_ENV_FILES", ".env").split(":") if f)


class CoreSettings(BaseSettings):
    #: ФАЙЛЫ окружения: по умолчанию один — `.env` (как было). Хостовые лончеры задают СПИСОК через
    #: `AGENT_ENV_FILES` (через двоеточие), потому что после разделения окружения
    #: общая основа и дополнения проекта лежат в РАЗНЫХ файлах. Контейнеры в этом списке не нуждаются:
    #: им то же самое приходит переменными окружения (`env_file: [../.env, ./.env]` в композе).
    model_config = SettingsConfigDict(env_file=_env_files(), extra="ignore")

    #: Каталог файлов-сессий Telegram. ❗ НЕ выводится из расположения кода: общий слой живёт в
    #: УСТАНОВЛЕННОМ пакете, и путь «рядом с модулем» ведёт внутрь `site-packages`, куда писать нельзя:
    #: `parents[2]/.sessions` из `agent_core/tg/connection.py` даёт
    #: `/usr/local/lib/python3.13/site-packages/.sessions`, запись отказывает `PermissionError`, и
    #: Telegram не подключается — беззвучно, потому что тесты настоящий файл-сессию не создают. Значение
    #: по умолчанию — путь В КОНТЕЙНЕРЕ (там каталог приходит монтированием); на хосте его задают
    #: лончеры явно.
    sessions_dir: str = "/srv/agent/.sessions"

    telegram_owner_chat_id: str = ""
    telegram_api_id: str = ""           # my.telegram.org (публичные 2040 годятся для dev)
    telegram_api_hash: str = ""
    # MTProto-прокси ОДНИМ адресом — прежняя форма, остаётся рабочей.
    telegram_mt_proxy_server: str = ""  # MTProto-прокси из t.me/proxy: server/port/secret
    telegram_mt_proxy_port: str = ""
    telegram_mt_proxy_secret: str = ""
    # МАССИВ прокси: записи через запятую, точку с запятой или перевод строки; каждая — ссылка
    # `t.me/proxy?...` либо тройка `сервер:порт:секрет`. Когда задан, берётся он, а одиночная тройка
    # выше не используется. Смысл массива: перебирать по очереди — прокси умирают, и один отказ не
    # должен оставлять бота выключенным: одиночная попытка подключения падает, а повторов нет.
    telegram_mt_proxies: str = ""


    # --- HTTP API сервиса ---
    bridge_api_key: str = ""          # X-API-Key для бизнес-ручек (>= 32 байт)
    # HMAC для кнопок (inline/reply) — ОТДЕЛЬНЫЙ от HTTP-ключа: утечка X-API-Key не должна
    # позволять подделывать кнопки. Пустой/короткий ⇒ кнопки не подписываем (работает текст).
    ui_secret: str = ""

    bridge_tz: str = "Europe/Moscow"
    bridge_budget_month_usd: float = 25.0


    # Отладочный лог ввода-вывода хода: полный текст, ушедший в канал (включая блок базы
    # знаний с фрагментами), и полный ответ агента. ВЫКЛЮЧЕНО по умолчанию осознанно: в
    # отладочном режиме в локальный файл попадает текст клиента. Включается, когда нужно
    # видеть ход целиком, а не восстанавливать его по косвенным признакам.
    turn_io_debug: bool = False


    # --- Трассировка ходов ---
    # Спаны уходят ПРЯМО в приём OTLP платформы (`POST {PLATFORM_API_URL}/otlp/v1/traces`): коллектор
    # из vendor в стек не входит и для этого не нужен. Пустой `otel_endpoint` означает «трассировки
    # нет» — сервис работает как раньше, и это штатное состояние, а не ошибка настройки.
    # ⚠️ В `.env` адрес платформы ВНУТРИСЕТЕВОЙ (`http://api:8000/api`); сервис подменяет его на
    # Переопределяется окружением: у хостового запуска адрес отличается от контейнерного.
    otel_endpoint: str = ""
    # Пусто ⇒ берём `platform_api_key`: у приёма требуется право `EDIT_SPANS`.
    otel_api_key: str = ""
    # Привязка спана к нашему агенту: атрибуты ресурса платформа игнорирует, а `ag.references.<ключ>` —
    # читает. Значения берутся из спанов самой платформы (`references`: workflow id/slug).
    otel_workflow_id: str = ""
    otel_workflow_slug: str = ""
    # ❗ Поля `otel_service_name` здесь БОЛЬШЕ НЕТ: имя службы выводится из РОЛИ агента
    # (`telemetry.service_name`: `agent-<роль>`).
    # Оставленный ключ был бы второй правдой: у OpenLIT `service.name` — ключ группировки, и одно
    # плоское имя на процесс снова слило бы все роли в одну «службу».

    # --- Вендор платформы: `agenta` | `openlit` ---
    # Кто даёт ФОРМУ спана и куда её принимает. ОДНА ручка на ось наблюдаемости (решение
    # владельца): без матрицы переопределений по осям. Значение по умолчанию `agenta` ⇒ поведение прежнее,
    # переключения не делаем. Стек вендора поднимается и гасится ВРУЧНУЮ — флаг его не разворачивает.
    # ❗ Ключ `PLATFORM_VENDOR` живёт в КОРНЕВОМ `.env`: контейнер получает его через
    # список файлов окружения, хост — через `AGENT_ENV_FILES`. Дублировать значение в двух местах
    # нельзя: хост и контейнер разошлись бы молча («две правды»).
    platform_vendor: str = "agenta"
    # --- Ось НАБЛЮДАЕМОСТИ: `TELEMETRY_VENDOR` ---
    # Кто даёт ФОРМУ спана и куда её принимает — НЕЗАВИСИМО от `platform_vendor` (тот отвечает за
    # рантайм и промпты). Пусто ⇒ берём `platform_vendor`: поведение прежнее, переключения нет.
    # Зачем отдельная ось: чтобы видеть спаны в OpenLIT, пока диалог ещё исполняет Agenta. Иначе
    # комбинация «рантайм Agenta + приём OpenLIT» невозможна, и второй вендор остаётся без данных.
    # ❗ Ключ живёт в общем окружении (как и `platform_vendor`) — дублировать в файлах продуктов
    # НЕЛЬЗЯ: хост и контейнер разошлись бы молча.
    telemetry_vendor: str = ""
    # --- Ось РАНТАЙМА: `AGENT_RUNTIME` ---
    # Кто ИСПОЛНЯЕТ ход: `platform` (через API платформы, как сегодня) | `own` (наш харнесс) |
    # `auto` (по вендору). Значения названы по СПОСОБУ, а не по бренду — тот же принцип, что у класса
    # `PlatformRuntime`; прежнее `agenta` отвергалось бы как неизвестное (см. `select.py`).
    # Пусто ⇒ `platform`: поведение прежнее. ❗ `own` реализован для роли `dialog`; для остальных ролей
    # он падает громко, поэтому `auto` остаётся выключенным до замера паритета (правило владельца).
    agent_runtime: str = ""

    # Приём OTLP/HTTP — адрес и ключ приёмника ТЕЛЕМЕТРИИ (ось `TELEMETRY_VENDOR`). ❗ Отдельно от
    # `otel_endpoint`: у вендоров разные приёмы, и общий ключ увёл бы спаны не туда, тогда как ось обещает
    # другой приёмник. Имя функциональное: вендор — значение оси, а не часть имени настройки.
    telemetry_otlp_endpoint: str = Field(
        default="", validation_alias=AliasChoices("TELEMETRY_OTLP_ENDPOINT", "OPENLIT_OTLP_ENDPOINT"))
    # Пусто ⇒ приём без авторизации.
    telemetry_otlp_api_key: str = Field(
        default="", validation_alias=AliasChoices("TELEMETRY_OTLP_API_KEY", "OPENLIT_OTLP_API_KEY"))


    # --- Agenta ---
    platform_api_url: str = "http://api:8000/api"
    platform_api_key: str = ""
    platform_project_id: str = ""


    # --- БД ---
    #: Адрес базы объявляет агент: в коде только ФОРМА, без литеральных кредов и имени базы. Пустое
    #: значение — отказ с именем ключа при подключении (`Db`), а не попытка уйти «куда-нибудь».
    db_url: str = Field(default="", validation_alias=AliasChoices("DB_URL", "BRIDGE_DB_URL"))

    # --- База знаний / RAG ---
    #: Векторная база — отдельная и принадлежит продукту-владельцу; адрес объявляет он же.
    vector_db_url: str = ""
    # Адреса сервисов моделей ядро НЕ знает: их объявляет агент (продукт) в своих настройках, потому что
    # адрес — свойство развёртывания, а не механизма. Здесь остаются только параметры выборки.
    # Сколько фрагментов базы знаний добавлять в ход и с какого порога похожести.
    retrieval_top_k: int = 4
    retrieval_min_score: float = 0.0

    # --- MCP-сервер ---
    # Платформа обращается к инструментам ИЗ КОНТЕЙНЕРА, а основной API сервиса слушает
    # только 127.0.0.1 — контейнер до него не дотягивается. Поэтому MCP поднимается
    # ОТДЕЛЬНЫМ слушателем, и на нём нет ничего, кроме MCP: публичный интерфейс
    # бизнес-ручек и служебных роутов остаётся на localhost.
    mcp_enabled: bool = True
    mcp_port: int = 8087
    # Ширина привязки: по умолчанию все интерфейсы — иначе docker-сеть не дотянется.
    # Ужесточается через .env (например, адресом docker-шлюза), если хост доступен извне.
    mcp_bind_host: str = "0.0.0.0"

    # --- Провайдер LLM (шов; Pi-мультипровайдер) ---
    llm_provider: str = "openai"           # openai|routerai|proxyapi
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model_chat: str = "gpt-4o-mini"
    openai_model_vision: str = "gpt-4o"
    routerai_api_key: str = ""
    routerai_base_url: str = ""
    proxyapi_api_key: str = ""
    proxyapi_base_url: str = ""

    # --- ЛЕСТНИЦА РЕШЕНИЙ ---
    # Адреса и ключи решателя объявляет агент: ядро знает только ПОРОГ, по которому решается, закрывать
    # ход локально или уходить в облако. Адрес — свойство развёртывания (служба может стоять на другом
    # хосте), а порог — параметр механизма.
    # ПОРОГ уверенности лестницы решений: не ниже — закрываем локально, ниже — уходим в облако.
    # Параметр, а не литерал: порог подбирается по размеченному набору, и подбор должен быть виден.
    # Значение намеренно высокое: скрыть новость от владельца по догадке хуже, чем спросить облако.
    # 0.75 — ИЗМЕРЕННОЕ значение (на 84 размеченных примерах): при нём решатель закрывает локально
    # 86,9 % случаев и ни разу не скрыл то, что человек счёл нужным; ошибки только в мягкую сторону
    # (12 лишних показов). Поднимать выше 0.85 невыгодно: экономия падает с 87 % до 65 % ради минус
    # двух лишних карточек. Менять только по новому замеру.
    solver_confidence_threshold: float = 0.75  # = DEFAULT_THRESHOLD в decisions/ladder.py

    # --- Лимиты / защита ---
    rate_per_chat_per_min: int = 10

    session_rotate_turns: int = 15
    turn_wait_soft_s: int = 60          # статус-сообщение
    turn_wait_failover_s: int = 120     # failover-текст + outbox
    turn_wait_cap_s: int = 120          # после admission 90 с — settle
    poll_interval_s: float = 2.5

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.bridge_tz)

    def ui_buttons_enabled(self) -> bool:
        """Кнопки подписываются только отдельным секретом — иначе остаётся текстовый путь."""
        return len(self.ui_secret) >= 16

    def vision_credentials(self) -> tuple[str, str, str]:
        """(base_url, api_key, model) для vision — по активному провайдеру."""
        if self.llm_provider == "routerai" and self.routerai_api_key:
            return self.routerai_base_url, self.routerai_api_key, self.openai_model_vision
        if self.llm_provider == "proxyapi" and self.proxyapi_api_key:
            return self.proxyapi_base_url, self.proxyapi_api_key, self.openai_model_vision
        return self.openai_base_url, self.openai_api_key, self.openai_model_vision

    def chat_credentials(self) -> tuple[str, str, str]:
        """(base_url, api_key, model) для текстовых вызовов — та же развилка, что у vision.

        Зачем отдельный метод, а не общий с параметром «какая модель»: моделей две, и общий
        метод с флагом рано или поздно получил бы третье значение. Развилка провайдера при этом
        одна и та же — она продублирована осознанно, рядом, чтобы при добавлении провайдера
        обе ветки правились в одном месте файла.

        Метод — единственный вход к текстовой модели: провайдер, ключ и имя модели берутся здесь,
        поэтому поле `openai_model_chat` читается в одном месте.
        """
        if self.llm_provider == "routerai" and self.routerai_api_key:
            return self.routerai_base_url, self.routerai_api_key, self.openai_model_chat
        if self.llm_provider == "proxyapi" and self.proxyapi_api_key:
            return self.proxyapi_base_url, self.proxyapi_api_key, self.openai_model_chat
        return self.openai_base_url, self.openai_api_key, self.openai_model_chat


settings = CoreSettings()