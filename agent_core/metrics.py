"""Метрики для алертов: `/metrics` в формате Prometheus.

**Зачем.** Сторож умеет будить человека (алерт в Telegram), но по числам не умеет
сравнивать: «бот молчит» он видит, а «стоимость дня выросла втрое» или «доля ходов без облака упала»
— нет. Здесь те же числа, что уже пишутся в базу, отдаются в формате, который забирают
Grafana/Prometheus.

**Почему текст собирается вручную, а не через `prometheus_client`.** Наши числа — не счётчики в
памяти процесса, а строки в базе за день: их надо прочитать на момент опроса и подставить здесь же.
Реестр `prometheus_client` под это не нужен, и лишняя зависимость в живом пути не оправдана.

**Почему при недоступной базе бизнес-числа НЕ отдаются.** `<prefix>_day_cost_usd 0` при лежащей базе
читался бы как «сегодня всё бесплатно». Поэтому причина видна отдельной метрикой `<prefix>_db_up 0`, а
числа отсутствуют — отсутствие данных не должно выглядеть как нулевые данные.

**Имена метрик строятся из префикса продукта:** ядро не знает, как называется служба, которую оно
обслуживает. Свои бизнес-числа продукт передаёт готовыми строками (`extra`).
"""
from __future__ import annotations

import time


def rss_mb() -> int:
    """RSS процесса в МБ. Живёт здесь, а не в `main.py`: те же числа нужны и `/health`, и `/metrics`.

    Возвращает `-1`, если прочитать не удалось: `0` выглядел бы как «память не растёт».
    """
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return -1


def _line(name: str, value: float, help_text: str, kind: str = "gauge") -> list[str]:
    """Строка экспозиции вместе с HELP и TYPE — по одному разу на метрику, как требует формат."""
    return [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}", f"{name} {value}"]


def render(*, prefix: str, started_at: float, telegram_running: bool, stats: dict, day: dict | None,
           outbox: dict | None = None, extra=()) -> str:
    """Текст экспозиции: состояние процесса всегда, числа дня — только когда база ответила."""
    lines: list[str] = []
    lines += _line(f"{prefix}_up", 1, "процесс отвечает")
    lines += _line(f"{prefix}_uptime_seconds", int(time.time() - started_at), "время работы процесса")
    lines += _line(f"{prefix}_rss_mb", rss_mb(), "память процесса (RSS), МБ")
    lines += _line(f"{prefix}_telegram_up", 1 if telegram_running else 0,
                   "соединение с Telegram фактически установлено")
    updates = int(stats.get("updates_processed") or 0) if isinstance(stats, dict) else 0
    lines += _line(f"{prefix}_updates_processed", updates, "обработано апдейтов Telegram за процесс")

    if day is None:
        # База недоступна: причина видна, числа отсутствуют — см. докстроку модуля.
        lines += _line(f"{prefix}_db_up", 0, "доступность базы (1 — отвечает)")
        return "\n".join(lines) + "\n"

    lines += _line(f"{prefix}_db_up", 1, "доступность базы (1 — отвечает)")
    lines += _line(f"{prefix}_day_turns", day.get("turns", 0), "клиентских ходов за окно наблюдения")
    lines += _line(f"{prefix}_day_turns_with_agent", day.get("with_agent", 0), "ходов с облачным вызовом")
    lines += _line(f"{prefix}_day_local_closes", day.get("local_closed", 0),
                   "ходов, закрытых локально без облака")
    lines += _line(f"{prefix}_day_cost_usd", day.get("cost_usd", 0.0), "расход за окно, USD")
    for name, value, help_text in extra:
        lines += _line(f"{prefix}_{name}", value, help_text)
    if outbox is not None:
        # Очередь исходящих. Лаг — МАКСИМУМ за день (его нельзя складывать, см. `set_max_metric`),
        # остальные — счётчики. Показываем только когда база ответила: без базы очередь неизвестна,
        # и ноль читался бы как «всё доставлено».
        lines += _line(f"{prefix}_outbox_sent", outbox.get("outbox_sent", 0), "исходящих доставлено за день")
        lines += _line(f"{prefix}_outbox_failed", outbox.get("outbox_failed", 0), "исходящих не доставлено (разбор)")
        lines += _line(f"{prefix}_outbox_retries", outbox.get("outbox_retries", 0), "отложено из-за лимитов")
        lines += _line(f"{prefix}_outbox_lag_seconds_max", outbox.get("outbox_lag_s", 0),
                       "пик задержки доставки за день, секунды")
    return "\n".join(lines) + "\n"
