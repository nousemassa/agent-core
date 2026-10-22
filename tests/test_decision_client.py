"""Клиент решателя: разбор ответа и сборка вызова, без внешней сети.

Сеть задевается только намеренно и только на закрытый локальный порт: это отказ соединения, а не
обращение наружу. Так проверяется деградация, без которой пункт не принимается.
"""
from __future__ import annotations

import asyncio

import pytest

from agent_core.decisions.client import (
    Decision,
    SolverBadAnswer,
    SolverUnavailable,
    decide,
    make_ask,
    parse_response,
)

FULL = {
    "answer": "да",
    "probabilities": {"да": 0.93, "нет": 0.07},
    "scores": {"да": 3.1, "нет": -1.2},
    "top_probability": 0.93,
    "model": "Jev-Style-2B-Decision-v3",
    "backend": "llama.cpp",
}


def test_response_is_parsed_into_answer_and_confidence():
    decision = parse_response(FULL)

    assert decision.answer == "да"
    assert decision.confident == pytest.approx(0.93)
    assert decision.probabilities["нет"] == pytest.approx(0.07)


def test_confidence_falls_back_to_probabilities_when_top_is_missing():
    """Без `top_probability` уверенность берётся из вероятностей, а НЕ как ноль.

    Ноль всегда уходил бы в облако, и «локальный уровень не работает» выглядело бы свойством
    решателя, а не следствием неполного ответа сервиса.
    """
    assert parse_response({"answer": "нет", "probabilities": {"да": 0.2, "нет": 0.8}}).confident == pytest.approx(0.8)


def test_answer_without_probabilities_is_zero_confidence_not_an_error():
    """Ответ без вероятностей разбирается: уверенность 0 ⇒ уходим в облако. Это безопасно."""
    assert parse_response({"answer": "да"}).confident == 0.0


@pytest.mark.parametrize("body", [None, [], {}, {"probabilities": {"да": 1.0}}])
def test_broken_answers_are_named_as_bad_answers(body):
    """Битый ответ — не «недоступность»: это разные причины, и в отчёте они должны различаться."""
    with pytest.raises(SolverBadAnswer):
        parse_response(body)


def test_non_numeric_top_probability_is_a_bad_answer():
    with pytest.raises(SolverBadAnswer):
        parse_response({"answer": "да", "top_probability": "высокая"})


def test_ask_requires_an_address():
    """Адреса у ядра нет: без него сборка вызова отказывает, а не уходит «куда-нибудь» по умолчанию."""
    with pytest.raises(ValueError):
        make_ask("")
    with pytest.raises(ValueError):
        make_ask("   ")


def test_ask_takes_address_and_key_from_the_caller(monkeypatch):
    """Адрес и ключ приходят от агента: ядро их не подставляет и из окружения не вычитывает."""
    seen: dict[str, object] = {}

    async def fake_decide(**kwargs):
        seen.update(kwargs)
        return Decision(answer="да", top_probability=0.9, probabilities={"да": 0.9})

    monkeypatch.setattr("agent_core.decisions.client.decide", fake_decide)
    ask = make_ask("http://solver.example:9000", api_key="ключ-агента", timeout_s=7.0)
    asyncio.run(ask("текст", "показывать?", {"да": "х"}))

    assert seen["base_url"] == "http://solver.example:9000"
    assert seen["api_key"] == "ключ-агента"
    assert seen["timeout_s"] == 7.0


def test_empty_arguments_are_bad_answers():
    """Пустое состояние или пустой список вариантов — ошибка вызова, а не вопрос к решателю."""
    with pytest.raises(SolverBadAnswer):
        asyncio.run(decide(state="  ", question="что?", options={"да": "х"}, base_url="http://127.0.0.1:9"))


def test_unreachable_solver_is_degradation_not_crash():
    """Закрытый порт — штатная деградация: `SolverUnavailable`, а не падение прогона."""
    with pytest.raises(SolverUnavailable):
        asyncio.run(decide(state="текст", question="показывать?", options={"да": "х", "нет": "у"},
                           base_url="http://127.0.0.1:9", timeout_s=1.0))


def test_confidence_is_a_number_even_without_probability():
    assert Decision(answer="да", top_probability=None).confident == 0.0
