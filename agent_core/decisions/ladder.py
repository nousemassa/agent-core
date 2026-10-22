"""Лестница решений: порог уверенности и переход на уровень выше.

Три уровня, и каждый следующий дороже предыдущего:

    уровень 1 — детерминированный код     (0 моделей, микросекунды, бесплатно)
       ↓ не разобрал
    уровень 2 — solver, локальная модель     (~1 с, бесплатно, калиброванная вероятность)
       ↓ неуверен
    уровень 3 — облачный агент платформы  (деньги и латентность, но максимум качества)

**Здесь нет ни сети, ни моделей** — только правило перехода и решение «закрыто локально или нет».
Так порог проверяется тестом, а не наблюдением в бою: подбор порога — это то, что легко сделать
незаметно, и именно поэтому он должен быть видимым и параметром настройки, а не литералом в коде.

**Почему вверх, а не вниз.** Уровень 3 — не наказание, а страховка: если локально не решили или
решили неуверенно, работает прежний путь. Обратного нет: облако никогда не вызывается «для проверки
локального решения», иначе экономии не было бы вовсе.
"""
from __future__ import annotations

from dataclasses import dataclass

from agent_core.decisions.client import Decision

# Порог по умолчанию — ОДНО значение на весь проект: измеренное и зафиксированное (см.
# и `config.py`). Резервные пути берут именно его: копии с разными значениями разошлись бы.
# значениями (0.75 против 0.9) — расхождение настройки и константы, найденное разбором.
DEFAULT_THRESHOLD = 0.75

LEVEL_CODE = 1
LEVEL_SOLVER = 2
LEVEL_CLOUD = 3

# Причина перехода на облако. Словами, потому что она попадает в журнал и в отчёт: «ушли в облако»
# и «ушли в облако, потому что решатель не поднят» — разные состояния системы.
REASON_CONFIDENT = "уверенность не ниже порога"
REASON_UNSURE = "уверенность ниже порога"
REASON_UNAVAILABLE = "решатель недоступен"
REASON_BAD_ANSWER = "ответ решателя не разобран"


@dataclass(frozen=True)
class Step:
    """Итог лестницы: на каком уровне закрыт случай и с какой уверенностью.

    **`answer` непустой ТОЛЬКО у закрытого локально случая.** При уходе на уровень 3 он пуст, даже
    если решатель что-то сказал: иначе «закрыто локально» перестало бы быть однозначным — вызывающий
    не смог бы отличить решение лестницы от мнения, которое она же признала недостаточным.
    """

    level: int
    answer: str
    confidence: float
    reason: str

    @property
    def closed_locally(self) -> bool:
        """Случай закрыт локально, облако не вызывалось — это и есть экономия."""
        return self.level == LEVEL_SOLVER


def choose(decision: Decision | None, *, threshold: float, problem: str = "") -> Step:
    """Решить, закрыт ли случай локально или уходит на уровень 3.

    `decision=None` означает «решатель не ответил»; `problem` тогда называет причину словами
    (`REASON_UNAVAILABLE` / `REASON_BAD_ANSWER`). Порог сравнивается с уверенностью решателя:
    **не ниже** порога — закрываем локально, иначе идём в облако. Сравнение нестрогое осознанно:
    порог 0.75 означает «0.75 — уже достаточно».
    """
    if decision is None:
        return Step(level=LEVEL_CLOUD, answer="", confidence=0.0,
                    reason=problem or REASON_UNAVAILABLE)
    if decision.confident >= threshold:
        return Step(level=LEVEL_SOLVER, answer=decision.answer,
                    confidence=decision.confident, reason=REASON_CONFIDENT)
    return Step(level=LEVEL_CLOUD, answer="", confidence=decision.confident, reason=REASON_UNSURE)


def threshold_from(settings) -> float:
    """Порог из настроек с проверкой границ.

    Порог — параметр конфига, а не литерал: его подбирают по размеченному набору. Значение вне
    [0..1] — ошибка настройки, а не «просто странный порог», поэтому оно приводится к границе с
    предупреждением в журнал: молча работать с порогом 1.5 значило бы никогда не закрывать случаи
    локально и объяснять это «решателем».
    """
    import logging

    log = logging.getLogger(__name__)
    try:
        value = float(getattr(settings, "solver_confidence_threshold", DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        log.warning("solver: порог не число (%r) — беру %s", getattr(settings, "solver_confidence_threshold", None),
                    DEFAULT_THRESHOLD)
        return DEFAULT_THRESHOLD
    if 0.0 <= value <= 1.0:
        return value
    clipped = min(max(value, 0.0), 1.0)
    log.warning("solver: порог %s вне [0..1] — беру %s", value, clipped)
    return clipped
