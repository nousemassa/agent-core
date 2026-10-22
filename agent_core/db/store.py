"""ХРАНИЛИЩЕ общей базы: пул соединений и операции, не зависящие от предметной области.

Здесь: согласия и их отзыв, состояние разговора, реплики диалога, трассы ходов, метрики дня, настройки,
журнал действий и очередь исходящих. Доменные операции живут в репозиториях продуктов: они наследуют
`Db` и добавляют свои методы.

Структура общей базы — в `schema_core.py`, накат — в `migrate.py`.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg

from agent_core.db.migrate import REVISION_DDL, apply_on_connection
from agent_core.db.registry import CORE_COMPONENTS, Component

log = logging.getLogger(__name__)


def usage_fields(usage: dict | None) -> tuple[int | None, int | None, int | None, float | None]:
    """Разобрать запись `usage` платформы: `{cost, input, output, total, type}`.

    Живёт рядом с таблицей трасс, а не у вызывающего: форма ответа платформы — её дело, и если она
    изменится, править надо в одном месте. Значения приходят и числами, и строками, поэтому приводим
    терпимо, а непонятное поле отдаём как пустое, а не нулём:
    ноль в отчёте читался бы как «бесплатно», а это неправда.
    """
    if not isinstance(usage, dict):
        return None, None, None, None

    def as_int(key: str) -> int | None:
        try:
            return int(float(usage.get(key)))
        except (TypeError, ValueError):
            return None

    try:
        cost = float(usage.get("cost")) if usage.get("cost") is not None else None
    except (TypeError, ValueError):
        cost = None
    return as_int("input"), as_int("output"), as_int("total"), cost


def decode_jsonb(raw: Any) -> dict:
    """jsonb из asyncpg приходит строкой — нормализуем в dict.

    Без этого `continuum` из `conversations` уезжал строкой в state-логику диалога
    (`continuum.get(...)` → AttributeError). Правило: любое чтение jsonb-колонки
    проходит через эту функцию.
    """
    if raw is None:
        return {}
    if isinstance(raw, str):
        return json.loads(raw)
    return dict(raw)


def parse_slot(slot_iso: str, *, tz: ZoneInfo | timezone = UTC) -> datetime:
    """ISO-время из разобранного значения → datetime для параметра `timestamptz`.

    Значение приходит как «настенное» время бизнес-таймзоны (`bridge_tz`, по умолчанию настроенная
    таймзона), поэтому naive-строку привязываем к ней: иначе БД истолкует «10:00» как UTC и пользователь
    увидит 07:00.
    asyncpg требует datetime (строку в `$n::timestamptz` не принимает — DataError).
    """
    text = (slot_iso or "").strip().replace(" ", "T")
    if not text:
        raise ValueError("пустой слот: вызывающая сторона должна проверять выбор до вызова")
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=tz)


class Db:
    def __init__(
        self,
        url: str,
        *,
        tz: ZoneInfo | timezone = UTC,
        apply_ddl: bool = True,
        components: tuple[Component, ...] = CORE_COMPONENTS,
        expected_tables: tuple[str, ...] | None = None,
        expected_component: str | None = None,
        expected_revision: int | None = None,
    ) -> None:
        #: Пустой адрес — отказ с именем настройки: иначе asyncpg скажет «нет пароля», и искать будут
        #: в пароле, а не в пропущенном ключе окружения.
        if not (url or "").strip():
            raise ValueError("адрес базы не задан: задайте DB_URL (или VECTOR_DB_URL) в окружении продукта")
        # asyncpg принимает postgresql:// (без +asyncpg)
        self._url = url.replace("postgresql+asyncpg://", "postgresql://")
        self._tz = tz  # бизнес-таймзона для слотов
        self._apply_ddl = apply_ddl  # False ⇒ процесс схемы не создаёт, а требует её
        #: Компоненты, чью схему этот процесс создаёт (при `apply_ddl=True`) или требует (при `False`).
        self._components = components
        #: Какие таблицы обязаны существовать, если схему создаёт кто-то другой. `None` ⇒ не проверяем.
        self._expected_tables = expected_tables
        #: Компонент, ревизию которого процесс требует. Проверяется только если задан.
        self._expected_component = expected_component
        self._expected_revision = expected_revision
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self._url, min_size=1, max_size=5)
        try:
            await self._check_schema()
        except BaseException:
            # ❗ Пул создавался ДО проверки схемы, и при её ошибке оставался жить: `wait_ready` ретраит
            # такую ошибку, то есть каждая попытка оставляла свой пул без владельца. Закрываем своё
            # перед тем, как бросить, — ретраи и их политику не меняем.
            await self.close()
            raise

    async def _check_schema(self) -> None:
        async with self.pool.acquire() as conn:
            if not self._apply_ddl:
                # Схему создаёт кто-то другой. Проверяем ровно те таблицы, которые назвал вызывающий;
                # не назвал — не проверяем вовсе (его схема не наша).
                if self._expected_tables is not None:
                    found = await conn.fetchval(
                        """SELECT count(*) FROM information_schema.tables
                           WHERE table_schema='public' AND table_name = ANY($1::text[])""",
                        list(self._expected_tables),
                    )
                    if found != len(self._expected_tables):
                        raise RuntimeError(
                            "схема не применена: выполните накат схемы — `python -m <продукт>.db.migrate`"
                        )
                if self._expected_component:
                    recorded = await conn.fetchval(
                        "SELECT revision FROM schema_revision WHERE component=$1", self._expected_component
                    )
                    required = self._expected_revision
                    if required is None:
                        required = next(
                            (c.revision for c in self._components if c.name == self._expected_component), None)
                    if recorded is None:
                        raise RuntimeError(
                            f"схема не накатана: в базе нет записи о компоненте {self._expected_component!r} — "
                            f"выполните накат схемы этого компонента"
                        )
                    if required is not None and recorded < required:
                        raise RuntimeError(
                            f"схема устарела: компонент {self._expected_component!r} накатан ревизией "
                            f"{recorded}, код требует {required} — выполните накат"
                        )
                return
            # Накат своим процессом: применяем переданные компоненты (общий слой идёт первым).
            await conn.execute(REVISION_DDL)
            for component in self._components:
                await apply_on_connection(conn, component)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Db.connect() не выполнен")
        return self._pool


    async def has_consent(self, *, platform: str, chat_id: str) -> bool:
        return bool(await self.pool.fetchval(
            "SELECT 1 FROM consents WHERE platform=$1 AND chat_id=$2", platform, chat_id))

    async def save_consent(self, *, platform: str, chat_id: str, version: str, allow_ai: bool = True) -> None:
        await self.pool.execute(
            """INSERT INTO consents (platform, chat_id, version, allow_ai) VALUES ($1,$2,$3,$4)
               ON CONFLICT (platform, chat_id) DO UPDATE SET version=$3, allow_ai=$4, created_at=now()""",
            platform, chat_id, version, allow_ai)

    async def revoke_consent(self, *, platform: str, chat_id: str) -> None:
        """Отзыв согласия: удаляем ПД клиента (анонимизация), согласие снимаем.

        Данные предметной области чистит продукт: он переопределяет метод и добавляет свои таблицы.
        """
        async with self.pool.acquire() as conn, conn.transaction():
                await conn.execute("DELETE FROM consents WHERE platform=$1 AND chat_id=$2", platform, chat_id)
                await conn.execute("DELETE FROM conversations WHERE platform=$1 AND chat_id=$2", platform, chat_id)
                #: ❗ Переписка стирается ВМЕСТЕ с состоянием, в ТОЙ ЖЕ транзакции: это единственная точка
                #: исполнения `/stop` и `/noai`, и история реплик — такой же ПД клиента, как его телефон.
                #: Без этой строки текст разговора пережил бы отзыв согласия и уехал бы в следующий промпт.
                await conn.execute("DELETE FROM dialog_messages WHERE platform=$1 AND chat_id=$2", platform, chat_id)


    async def append_dialog_message(self, *, platform: str, chat_id: str, turn_key: str, role: str,
                                    text: str, step: str = "") -> None:
        """Записать реплику хода. Текст обязан быть уже МАСКИРОВАННЫМ — это проверяет вызывающий.

        Пишет продукт, а не рантайм: иначе история заполнялась бы только на своём рантайме,
        и переключение `AGENT_RUNTIME` рвало бы контекст в одну сторону.
        """
        await self.pool.execute(
            """INSERT INTO dialog_messages (platform, chat_id, turn_key, role, text, step)
               VALUES ($1,$2,$3,$4,$5,$6)""",
            platform, chat_id, turn_key, role, text, step)

    async def load_dialog_history(self, *, platform: str, chat_id: str, limit: int = 8) -> list[dict]:
        """Окно истории для промпта: последние `limit` реплик В ХРОНОЛОГИЧЕСКОМ порядке.

        `ORDER BY id DESC` + разворот: так работает индекс `(platform, chat_id, id DESC)`, а модели нужен
        прямой порядок — «раньше → позже», иначе реплики читаются как перевёрнутый разговор.
        """
        rows = await self.pool.fetch(
            """SELECT role, text FROM dialog_messages
               WHERE platform=$1 AND chat_id=$2 ORDER BY id DESC LIMIT $3""",
            platform, chat_id, limit)
        return [{"role": row["role"], "content": row["text"]} for row in reversed(rows)]

    async def prune_dialog_messages(self, *, platform: str, chat_id: str, keep: int = 40) -> int:
        """Оставить `keep` последних реплик клиента. Возвращает, сколько удалено.

        Окно для промпта — 8 реплик, но хранить ровно 8 нельзя: правка окна вверх иначе упиралась бы в
        исторические данные, которых уже нет. `40` — запас на смену окна без потери свежих ходов.
        """
        deleted = await self.pool.execute(
            """DELETE FROM dialog_messages WHERE platform=$1 AND chat_id=$2 AND id NOT IN (
                   SELECT id FROM dialog_messages WHERE platform=$1 AND chat_id=$2
                   ORDER BY id DESC LIMIT $3)""",
            platform, chat_id, keep)
        #: `execute` возвращает строку вида `DELETE 5`; берём число, а не гадаем.
        return int(str(deleted).split()[-1]) if str(deleted).split() else 0


    async def load_conversation(self, *, platform: str, chat_id: str) -> dict | None:
        row = await self.pool.fetchrow(
            """SELECT session_id, seq, step, continuum, last_inbound_at
               FROM conversations WHERE platform=$1 AND chat_id=$2""", platform, chat_id)
        if not row:
            return None
        data = dict(row)
        data["continuum"] = decode_jsonb(data.get("continuum"))
        return data

    async def save_conversation(self, *, platform: str, chat_id: str, step: str, continuum: dict) -> None:
        await self.pool.execute(
            """INSERT INTO conversations (platform, chat_id, session_id, seq, step, continuum, last_inbound_at)
               VALUES ($1,$2,$3,0,$4,$5::jsonb, now())
               ON CONFLICT (platform, chat_id) DO UPDATE
               SET step=$4, continuum=$5::jsonb, last_inbound_at=now(), updated_at=now()""",
            platform, chat_id, f"{platform}-{chat_id}", step, json.dumps(continuum, ensure_ascii=False))


    async def ai_allowed(self, *, platform: str, chat_id: str) -> bool:
        """False только если согласие есть, но ИИ отключён командой `/noai` (M7)."""
        value = await self.pool.fetchval(
            "SELECT allow_ai FROM consents WHERE platform=$1 AND chat_id=$2", platform, chat_id)
        return True if value is None else bool(value)


    async def bump_metric(self, *, metric: str, delta: float = 1, day: str | None = None) -> None:
        """Счётчики дня для отчёта/статуса (пишем только то, что показываем)."""
        await self.pool.execute(
            """INSERT INTO meta_metrics (day, metric, value) VALUES (COALESCE($3::date, now()::date), $1, $2)
               ON CONFLICT (day, metric) DO UPDATE SET value = meta_metrics.value + EXCLUDED.value""",
            metric, delta, day)


    async def log_audit(self, *, actor: str, action: str, result: str | None = None,
                        meta: dict | None = None) -> None:
        """Записать действие в общий журнал: кто, что, с каким итогом."""
        await self.pool.execute(
            """INSERT INTO audit_log (actor, action, result, meta)
               VALUES ($1, $2, $3, $4::jsonb)""",
            actor, action, result, json.dumps(meta) if meta else None)

    async def set_setting(self, key: str, value: Any) -> None:
        """Писатель настроек (был только читатель) — нужен для паузы/запуска из меню."""
        await self.pool.execute(
            "INSERT INTO settings (key, value) VALUES ($1,$2::jsonb)	ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
            key, json.dumps(value))


    async def day_metrics(self, day: date | None, names: tuple[str, ...]) -> dict:
        """Метрики дня одним запросом.

        `day=None` — «сегодня по часам БАЗЫ», как и при записи (`bump_metric` пишет `now()::date`).
        Это не придирка: если у процесса и у postgres разные пояса, «сегодня» у них разные дни, и
        метрика, записанная по базе, читалась бы по календарю процесса как нулевая.
        """
        rows = await self.pool.fetch(
            """SELECT metric, value FROM meta_metrics
                WHERE day = COALESCE($1::date, now()::date) AND metric = ANY($2::text[])""",
            day, list(names))
        return {row["metric"]: float(row["value"]) for row in rows}


    async def save_turn_trace(self, *, day: date, platform: str, chat_hash: str, step: str,
                              context_items: int, agent_called: bool, outcome: str,
                              parser_verdict: str, prompt_hash: str = "", turn_id: int | None = None,
                              session_id: str = "", model: str | None = None,
                              provider: str | None = None, usage: dict | None = None,
                              latency_ms: int | None = None, trace_id: str = "") -> None:
        """Одна строка на клиентский ход: токены, стоимость, латентность, исход разбора.

        Стоимость и токены берём из записи `usage` платформы, а не считаем по прайсу: платформа
        знает цену своего вызова, а прайс устаревает. Разбор `usage` живёт здесь, а не у
        вызывающего: форма ответа платформы — её дело, и место правки должно быть одно.

        Отсутствие `usage` — не ошибка: ход был, строка пишется, числа просто пустые.
        """
        tokens_in, tokens_out, tokens_total, cost = usage_fields(usage)
        await self.pool.execute(
            """INSERT INTO turn_traces (day, platform, chat_hash, turn_id, session_id, step,
                                        prompt_hash, context_items, agent_called, outcome,
                                        parser_verdict, model, provider, tokens_in, tokens_out,
                                        tokens_total, cost_usd, latency_ms, trace_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)""",
            day, platform, chat_hash, turn_id, session_id, step, prompt_hash, context_items,
            agent_called, outcome, parser_verdict, model, provider, tokens_in, tokens_out,
            tokens_total, cost, latency_ms, trace_id)

    async def cost_usd(self, day: date) -> float:
        """Расход за день — для отчёта владельцу (вместо «нет данных»)."""
        total = await self.pool.fetchval(
            "SELECT coalesce(sum(cost_usd), 0) FROM turn_traces WHERE day = $1", day)
        return float(total or 0)

    async def cost_usd_month(self, day: date) -> float:
        """Расход с начала месяца: печатается рядом с капом, но кап НЕ ограничивает (решение владельца)."""
        total = await self.pool.fetchval(
            """SELECT coalesce(sum(cost_usd), 0) FROM turn_traces
               WHERE day >= date_trunc('month', $1::date)::date""", day)
        return float(total or 0)


    async def turn_traces(self, *, days: int = 7, limit: int = 200) -> list[dict]:
        """Последние строки трасс — без ПДн: в `chat_hash` хеш, самого `chat_id` в таблице нет."""
        rows = await self.pool.fetch(
            """SELECT id, created_at, day, platform, chat_hash, turn_id, session_id, step,
                      context_items, agent_called, outcome, parser_verdict, model, provider,
                      tokens_in, tokens_out, tokens_total, cost_usd, latency_ms, trace_id
                 FROM turn_traces WHERE day >= (current_date - $1::int)
                ORDER BY id DESC LIMIT $2""", days, limit)
        return [dict(row) for row in rows]

    # ---------- очередь исходящих ---------- #
    #
    # Семантика перенесена из `turns.py`, где та же задача решена для хода диалога:
    # захват — условный `UPDATE … RETURNING` (в проекте нигде нет `FOR UPDATE SKIP LOCKED`, только
    # докстринги, которые его обещают), а «отправка могла начаться» фиксируется **ДО** сетевого
    # вызова. Здесь это делает сам `claim_outbox`: он переводит строку в `in_flight` ещё до отправки,
    # поэтому смерть процесса в любой момент после захвата оставляет строку в `in_flight` — и
    # `sweep_outbox` её НЕ переигрывает, а отправляет на разбор. Это и есть защита от дубля.

    async def enqueue_outbox(self, *, kind: str, payload: dict) -> int:
        """Поставить исходящее в очередь. Возвращает id строки."""
        return int(await self.pool.fetchval(
            "INSERT INTO outbox (kind, payload) VALUES ($1, $2::jsonb) RETURNING id",
            kind, json.dumps(payload)))

    async def claim_outbox(self, *, lease_seconds: float, owner: str) -> dict | None:
        """Взять одну строку в работу. `None` — работы нет.

        Берутся только `new` с наступившим `next_attempt_at`: строки `in_flight` (отправка могла
        начаться) не трогаются никогда — их разбирает sweeper.
        """
        row = await self.pool.fetchrow(
            """
            UPDATE outbox
               SET status = 'in_flight',
                   lease_until = now() + ($1 || ' seconds')::interval,
                   lease_owner = $2,
                   attempts = attempts + 1
             WHERE id = (SELECT id FROM outbox
                          WHERE status = 'new'
                            AND (next_attempt_at IS NULL OR next_attempt_at <= now())
                          ORDER BY seq
                          LIMIT 1)
            RETURNING id, kind, payload, attempts
            """,
            str(lease_seconds), owner)
        if not row:
            return None
        # `jsonb` приходит строкой — нормализуем (правило проекта: любое чтение jsonb идёт через
        # `decode_jsonb`, иначе отправителю досталась бы строка вместо данных).
        return {**dict(row), "payload": decode_jsonb(row["payload"])}

    async def mark_outbox_sent(self, outbox_id: int) -> None:
        """Отправлено и подтверждено. Lease снимается: строка больше не наша."""
        await self.pool.execute(
            """UPDATE outbox
                  SET status = 'sent', sent_at = now(), last_error = NULL,
                      lease_until = NULL, lease_owner = NULL
                WHERE id = $1""",
            outbox_id)

    async def release_outbox(self, outbox_id: int, *, delay_seconds: float, error: str) -> None:
        """Вернуть в очередь после ВРЕМЕННОЙ ошибки (лимит, таймаут): строка не теряется, а ждёт."""
        await self.pool.execute(
            """UPDATE outbox
                  SET status = 'new',
                      next_attempt_at = now() + ($2 || ' seconds')::interval,
                      lease_until = NULL, lease_owner = NULL, last_error = $3
                WHERE id = $1""",
            outbox_id, str(delay_seconds), error)

    async def mark_outbox_failed(self, outbox_id: int, *, error: str) -> None:
        """Постоянная ошибка: строка остаётся в таблице вместе с причиной (не «исчезает в логе»)."""
        await self.pool.execute(
            """UPDATE outbox
                  SET status = 'failed', last_error = $2, lease_until = NULL, lease_owner = NULL
                WHERE id = $1""",
            outbox_id, error)

    async def stuck_outbox(self, *, limit: int = 50) -> list[dict]:
        """Строки `in_flight` с просроченным lease — «отправка могла начаться, исход неизвестен»."""
        rows = await self.pool.fetch(
            """SELECT id, kind, attempts, lease_owner, payload
                 FROM outbox
                WHERE status = 'in_flight' AND lease_until IS NOT NULL AND lease_until < now()
                ORDER BY seq
                LIMIT $1""",
            limit)
        return [{**dict(row), "payload": decode_jsonb(row["payload"])} for row in rows]

    async def sweep_outbox(self) -> list[dict]:
        """Разобрать залипшие отправки. **НЕ переигрываем** — исход неизвестен.

        Ровно как `turns.sweep`: повторная отправка создала бы второй ответ клиенту, то есть
        тот самый дефект, ради которого очередь и делается. Поэтому строка помечается `failed` с
        причиной «разбор вручную», а не возвращается в `new`.
        """
        resolved: list[dict] = []
        for item in await self.stuck_outbox():
            reason = (f"исход неизвестен: процесс умер после начала отправки "
                      f"(попытка {item['attempts']}, владелец {item['lease_owner'] or '—'}) — разбор вручную")
            await self.mark_outbox_failed(int(item["id"]), error=reason)
            resolved.append({**item, "status": "failed", "last_error": reason})
            log.warning("outbox: строка %s (%s) залипла в in_flight — помечена failed, повтор НЕ отправлен",
                        item["id"], item["kind"])
        return resolved

    async def outbox_lag_s(self) -> float:
        """Возраст самой старой неотправленной строки, секунды. Пустая очередь — 0."""
        value = await self.pool.fetchval(
            """SELECT COALESCE(max(extract(epoch FROM (now() - created_at))), 0)
                 FROM outbox WHERE status IN ('new', 'in_flight')""")
        return float(value or 0.0)

    async def set_max_metric(self, *, metric: str, value: float, day: str | None = None) -> None:
        """Метрика-МАКСИМУМ за день: `bump_metric` складывает, а лаг складывать нельзя."""
        await self.pool.execute(
            """INSERT INTO meta_metrics (day, metric, value) VALUES (COALESCE($3::date, now()::date), $1, $2)
               ON CONFLICT (day, metric) DO UPDATE SET value = GREATEST(meta_metrics.value, EXCLUDED.value)""",
            metric, value, day)


    async def setting(self, key: str, default: Any = None) -> Any:
        raw = await self.pool.fetchval("SELECT value FROM settings WHERE key=$1", key)
        return default if raw is None else json.loads(raw)
