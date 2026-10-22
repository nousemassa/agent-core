"""Лестница решений: порог и переходы — чистая логика, без сети и моделей.

Именно поэтому порог можно проверить тестом: он обязан быть видимым и параметром настройки, а не
литералом, подобранным «на глаз» где-то в месте применения.
"""
from __future__ import annotations

import pytest

from agent_core.decisions.client import Decision
from agent_core.decisions.ladder import (
    LEVEL_CLOUD,
    LEVEL_SOLVER,
    REASON_UNAVAILABLE,
    REASON_UNSURE,
    choose,
    threshold_from,
)


def test_confidence_at_the_threshold_closes_locally():
    """Сравнение НЕстрогое: порог 0.9 значит «0.90 уже достаточно».

    Иначе порог читался бы как 0.900000…1, и объяснить это человеку было бы нечем.
    """
    step = choose(Decision(answer="да", top_probability=0.9), threshold=0.9)

    assert step.level == LEVEL_SOLVER
    assert step.closed_locally is True
    assert step.answer == "да"


def test_confidence_below_threshold_escalates_to_cloud():
    step = choose(Decision(answer="да", top_probability=0.89), threshold=0.9)

    assert step.level == LEVEL_CLOUD
    assert step.closed_locally is False
    assert step.reason == REASON_UNSURE
    assert step.confidence == pytest.approx(0.89)


def test_missing_solver_escalates_and_names_the_reason():
    """Нет решателя — идём в облако, и причина названа: «недоступен» и «не уверен» — разные вещи."""
    step = choose(None, threshold=0.9)

    assert step.level == LEVEL_CLOUD
    assert step.reason == REASON_UNAVAILABLE


def test_threshold_comes_from_settings_not_from_code():
    class Settings:
        solver_confidence_threshold = 0.75

    assert threshold_from(Settings()) == pytest.approx(0.75)


def test_out_of_range_threshold_is_clipped_and_reported(caplog):
    """Порог вне [0..1] — ошибка настройки: приводим к границе и говорим об этом в журнал.

    Молча работать с порогом 1.5 значило бы никогда не закрывать случаи локально и объяснять это
    «решателем», а не опечаткой в настройке.
    """
    class Settings:
        solver_confidence_threshold = 1.5

    with caplog.at_level("WARNING"):
        assert threshold_from(Settings()) == 1.0
    assert "вне" in caplog.text


def test_non_numeric_threshold_falls_back_with_a_warning(caplog):
    class Settings:
        solver_confidence_threshold = "высокий"

    from agent_core.decisions.ladder import DEFAULT_THRESHOLD

    with caplog.at_level("WARNING"):
        assert threshold_from(Settings()) == pytest.approx(DEFAULT_THRESHOLD)
    assert "не число" in caplog.text


def test_escalation_does_not_carry_the_solvers_answer():
    """У ушедшего в облако случая ответа нет: иначе «закрыто локально» теряет однозначность."""
    step = choose(Decision(answer="да", top_probability=0.6), threshold=0.9)

    assert step.answer == ""
    assert step.confidence == pytest.approx(0.6), "уверенность сохраняем: она объясняет переход"
