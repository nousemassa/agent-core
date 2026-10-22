# `agent-core` — общий слой продуктов

**Что это.** Python-пакет `agent_core`: настройки, доступ к PostgreSQL, подключение к Telegram,
телеметрия и трассировка, метрики, редакция приватного в журналах, лестница решений и поиск по корпусу.
Продукты ставят его **колёсом** (`.whl`) и объявляют свои настройки подклассом `CoreSettings`.

**Чего здесь нет.** Предметных правил и данных продукта: его схемы, его инструментов, его ролей. Пакет
ядра не импортирует ни один продуктовый пакет; своих тестов на граф импортов здесь нет.

## Состав

| Модули | Что делают |
|---|---|
| `config.py`, `api_auth.py`, `ids.py` | настройки (`CoreSettings`), проверка `X-API-Key`, идентификаторы |
| `db/` | `schema_core`/`schema_runtime` (DDL компонентов), `schema.py` (DDL корпуса и `normalize_dsn`), реестр компонентов, накат, `store.py` (пул и операции) |
| `tg/` | подключение MTProto (массив прокси, файл-сессия, сторож), сборка клиента, рендер `Reply` |
| `telemetry.py`, `spans.py`, `metrics.py`, `log_trace.py`, `logging_redact.py` | трассировка, форма спана, `/metrics`, `trace_id` в журналах, маскирование |
| `startup.py` | ожидание зависимостей на старте (бюджет и backoff) |
| `retrieval/` | эмбеддинги (клиент), поиск по pgvector, подсказка о покрытии |
| `decisions/` | клиент решателя и лестница решений (порог уверенности) |
| `vendors/` | формы спана (`agenta.py`, `openlit.py`), нейтральная модель; канальный путь платформы — порт `channels/port.py` и адаптер `channels/agenta.py`; ось рантайма (`runtime/`: `select`, `port`, `own`, `platform_api`, `provider`, `prompt_spec`) |
| `ui/` | `Reply`, токены оформления |

## Установка

```bash
python -m venv .venv
.venv/bin/pip install -e . pytest pytest-asyncio ruff build
```

Колесо для потребителей:

```bash
make wheel            # dist/agent_core-<версия>-py3-none-any.whl
```

Версия объявлена в `pyproject.toml` и в `agent_core/__init__.py`: её видит и пакет, и потребитель.
Совпадение проверяет тест `tests/test_version.py` (иначе колесо ставится под одной версией, а называется
другой). Тестовые двойники (`agent_core.testing`) в колесо **не входят** — это проверяет шаг сборки в CI.

## Настройки

Все значения читаются из окружения (`pydantic-settings`, лишние ключи игнорируются). Продукт наследует
`CoreSettings` и добавляет свои поля.

| Поле (env) | Тип | Значение по умолчанию |
|---|---|---|
| `SESSIONS_DIR` | str | `'/srv/agent/.sessions'` |
| `TELEGRAM_OWNER_CHAT_ID` | str | `''` |
| `TELEGRAM_API_ID` | str | `''` |
| `TELEGRAM_API_HASH` | str | `''` |
| `TELEGRAM_MT_PROXY_SERVER` | str | `''` |
| `TELEGRAM_MT_PROXY_PORT` | str | `''` |
| `TELEGRAM_MT_PROXY_SECRET` | str | `''` |
| `TELEGRAM_MT_PROXIES` | str | `''` |
| `BRIDGE_API_KEY` | str | `''` |
| `UI_SECRET` | str | `''` |
| `BRIDGE_TZ` | str | `'Europe/Moscow'` |
| `BRIDGE_BUDGET_MONTH_USD` | float | `25.0` |
| `TURN_IO_DEBUG` | bool | `False` |
| `OTEL_ENDPOINT` | str | `''` |
| `OTEL_API_KEY` | str | `''` |
| `OTEL_WORKFLOW_ID` | str | `''` |
| `OTEL_WORKFLOW_SLUG` | str | `''` |
| `PLATFORM_VENDOR` | str | `'agenta'` |
| `TELEMETRY_VENDOR` | str | `''` |
| `AGENT_RUNTIME` | str | `''` |
| `TELEMETRY_OTLP_ENDPOINT` | str | `''` |
| `TELEMETRY_OTLP_API_KEY` | str | `''` |
| `PLATFORM_API_URL` | str | `'http://api:8000/api'` |
| `PLATFORM_API_KEY` | str | `''` |
| `PLATFORM_PROJECT_ID` | str | `''` |
| `DB_URL` | str | `''` |
| `VECTOR_DB_URL` | str | `''` |
| `RETRIEVAL_TOP_K` | int | `4` |
| `RETRIEVAL_MIN_SCORE` | float | `0.0` |
| `MCP_ENABLED` | bool | `True` |
| `MCP_PORT` | int | `8087` |
| `MCP_BIND_HOST` | str | `'0.0.0.0'` |
| `LLM_PROVIDER` | str | `'openai'` |
| `OPENAI_API_KEY` | str | `''` |
| `OPENAI_BASE_URL` | str | `'https://api.openai.com/v1'` |
| `OPENAI_MODEL_CHAT` | str | `'gpt-4o-mini'` |
| `OPENAI_MODEL_VISION` | str | `'gpt-4o'` |
| `ROUTERAI_API_KEY` | str | `''` |
| `ROUTERAI_BASE_URL` | str | `''` |
| `PROXYAPI_API_KEY` | str | `''` |
| `PROXYAPI_BASE_URL` | str | `''` |
| `SOLVER_CONFIDENCE_THRESHOLD` | float | `0.75` |
| `RATE_PER_CHAT_PER_MIN` | int | `10` |
| `SESSION_ROTATE_TURNS` | int | `15` |
| `TURN_WAIT_SOFT_S` | int | `60` |
| `TURN_WAIT_FAILOVER_S` | int | `120` |
| `TURN_WAIT_CAP_S` | int | `120` |
| `POLL_INTERVAL_S` | float | `2.5` |

Пустые значения — это не «нет настройки», а «объявляет агент»: адреса баз (`DB_URL`, `VECTOR_DB_URL`),
ключи каналов и адреса сервисов моделей приходят из окружения продукта. Литеральных кредов в коде нет:
при пустом адресе `Db` отказывает с именем ключа, а не идёт подключаться «куда-нибудь».

Имена настроек говорят о ФУНКЦИИ: адрес приёма телеметрии — `TELEMETRY_OTLP_ENDPOINT` (вендор приёма
задаётся осью `TELEMETRY_VENDOR`, а не именем ключа); прежние имена принимаются как алиасы.

## Схема и накат

В реестре два компонента. `core`: `settings`, `meta_metrics`, `audit_log` — 3 проверки. `runtime`:
`turn_state`, `turn_traces`, `outbox`, `conversations`, `consents`, `consumed_updates`, `send_state`,
`dialog_messages` — 8 таблиц, 10 проверок. Продукт накатывает те, что ему нужны, своим CLI; `url_key`
компонента говорит, к какой базе он относится, а прежнее имя компонента (если его писал старый образ)
продукт объявляет сам в `legacy_names`.

DDL корпуса (`knowledge_chunks` с вектором и полнотекстовым индексом) лежит в `db/schema.py`, но
**компонентом ядра не является**: база корпуса принадлежит продукту, и накатывает её он же.

```bash
python -m agent_core.db.migrate --components core,runtime --wait 30
python scripts/ci-schema-check.py --components core,runtime    # чистая база: DSN в CI_DB_URL
```

**Минимум окружения для наката.** Ядру для этого нужен ровно один ключ — `DB_URL`
(`postgresql+asyncpg://…`); адрес можно передать и флагом `--db-url`. Остальные 47 настроек имеют
дефолты, а адреса баз, каналов и сервисов моделей объявляет продукт: файла `.env` в репозитории нет
намеренно — иначе ядро выглядело бы владельцем чужих адресов и ключей. Полный перечень — в таблице
«Настройки» выше.

## Проверки

```bash
make ci             # ruff + офлайн-тесты
make schema-check   # схема накатывается на чистую базу и совпадает с ожиданием
```

То же гоняет CI (`.github/workflows/ci.yml`): линтер, офлайн-тесты, накат `core,runtime` на чистую базу и
сборка колеса с проверкой содержимого.

## Решения

| Решение | Что этим исключено |
|---|---|
| Подключение к Telegram — один механизм на процесс: массив прокси, файл-сессия, сторож соединения. | Копия кода в двух процессах разошлась бы в поведении: у одного перебор прокси, у другого одна попытка. |
| Схема — реестр компонентов: свой DDL, своя ревизия, своя проверка наката. | «Одна общая схема» на все продукты и молчаливое отставание ревизии. |
| Накат — отдельный шаг с ожиданием готовности базы. | Старт сервиса, который падает от того, что база ещё поднимается. |
| Состояние хода — в PostgreSQL, а не в памяти. | Потеря хода при рестарте и дубли после перезапуска. |
| RAG — на pgvector с гибридом (вектор + полнотекст). | Отдельная поисковая система рядом с данными. |
| Эмбеддинги — клиент к OpenAI-совместимому эндпоинту, адрес даёт агент. | Привязка общего слоя к конкретному хосту модели. |
| Решатель — клиент с деградацией на облако; ядро знает только порог. | Запрос «не туда» при переносе службы. |
| Телеметрия — оси вендора и рантайма независимы; неизвестное значение — отказ. | Тихий фолбэк на чужой формат спана. |
| Приватный токен календаря маскируется фильтром на обработчиках журнала, а не точечно в местах логирования. | Токен в журнале из-за строки `httpx`, которая печатает URL запроса. |

## Оси вендора и рантайма

| Переменная | Значения | По умолчанию |
|---|---|---|
| `PLATFORM_VENDOR` | `agenta`, `openlit` | `agenta` |
| `TELEMETRY_VENDOR` | `agenta`, `openlit` | значение платформы |
| `AGENT_RUNTIME` | `platform`, `own`, `auto` | пусто = `platform` |

Пустое значение — «по умолчанию», неизвестное — отказ при сборке. Адаптеры: `vendors/agenta.py`,
`vendors/openlit.py`, `vendors/runtime/own.py`; выбор рантайма — `vendors/runtime/select.py`.

## Лицензия

MIT — см. `LICENSE`.
