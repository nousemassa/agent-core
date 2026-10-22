"""Шапка файла агента — единственный источник параметров хода (`own`-рантайм).

**Что доказывается.** Что `temperature` и `max_tokens` берутся ИЗ ФАЙЛА, а не из константы в коде: иначе
две правды разъедутся молча, и правка шапки никем не будет замечена. И что сломанная шапка — отказ с
путём, а не тихий дефолт (дефолт бюджета ответа = непознаваемый счёт за ход).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.vendors.runtime.prompt_spec import PromptSpecError, spec_for

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "prompts"
DIALOG = FIXTURES / "dialog.md"
LISTING = FIXTURES / "alt.md"


def test_dialog_header_is_parsed_from_the_file():
    spec = spec_for(DIALOG)

    assert spec.temperature == 0.2
    assert spec.max_tokens == 400
    assert spec.model_platform == "deepseek/deepseek-v4.1-flash"
    #: Системное сообщение — ВЕСЬ файл: текст агента и есть промпт, а не выжимка из него.
    assert spec.system_text == DIALOG.read_text(encoding="utf-8")


def test_model_may_be_bare_and_not_only_in_backticks():
    """`listing-writer.md` называет модель ГОЛОЙ строкой — шапка обязана читаться в обеих формах."""
    spec = spec_for(LISTING)

    assert spec.model_platform == "gpt-4o"
    assert spec.temperature == 0.4
    assert spec.max_tokens == 600


def test_header_without_max_tokens_is_a_loud_refusal(tmp_path: Path):
    broken = tmp_path / "broken.md"
    broken.write_text("**Харнесс**: Pi. **Модель**: `m`. **temperature 0.2**.\nтекст\n", encoding="utf-8")

    with pytest.raises(PromptSpecError) as caught:
        spec_for(broken)

    #: Отказ обязан НАЗЫВАТЬ файл и чего не хватило — иначе разбор начинается вслепую.
    assert str(broken) in str(caught.value)
    assert "max_tokens" in str(caught.value)


def test_two_temperatures_are_a_refusal_not_a_guess(tmp_path: Path):
    doubled = tmp_path / "double.md"
    doubled.write_text(
        "**Модель**: `m`. **temperature 0.2**, **max_tokens 400**. затем ещё temperature 0.9\n",
        encoding="utf-8",
    )

    with pytest.raises(PromptSpecError, match="temperature"):
        spec_for(doubled)


def test_missing_file_names_the_setting_that_should_point_at_it(tmp_path: Path):
    with pytest.raises(PromptSpecError) as caught:
        spec_for(tmp_path / "nope.md")

    assert "DIALOG_PROMPT_PATH" in str(caught.value)
