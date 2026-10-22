"""Подпись данных кнопок: привязка к чату и токену хода, лимит `callback_data`."""
from __future__ import annotations

import pytest

from agent_core.ui import tokens

SECRET = "test-secret"


def test_sign_verify_roundtrip():
    data = tokens.sign(SECRET, chat_id="42", kind="t", arg="3", token="abc123")
    assert tokens.verify(SECRET, data, chat_id="42", token="abc123") == ("t", "3")


def test_other_chat_rejected():
    data = tokens.sign(SECRET, chat_id="42", kind="t", arg="3", token="abc123")
    assert tokens.verify(SECRET, data, chat_id="43", token="abc123") is None, "кнопка чужого чата"


def test_other_token_rejected():
    data = tokens.sign(SECRET, chat_id="42", kind="t", arg="3", token="abc123")
    assert tokens.verify(SECRET, data, chat_id="42", token="abc124") is None, "кнопка прошлого хода"


def test_tampered_arg_rejected():
    data = tokens.sign(SECRET, chat_id="42", kind="t", arg="3", token="abc123")
    tampered = data.replace(":3:", ":4:")
    assert tokens.verify(SECRET, tampered, chat_id="42", token="abc123") is None


def test_other_secret_rejected():
    data = tokens.sign(SECRET, chat_id="42", kind="t", arg="1", token="abc123")
    assert tokens.verify("another-secret", data, chat_id="42", token="abc123") is None


def test_foreign_data_shapes_rejected():
    for data in ("consent:yes", "", "c:t:1", "x:t:1:deadbeef"):
        assert tokens.verify(SECRET, data, chat_id="42", token="abc123") is None


def test_callback_data_fits_telegram_limit():
    data = tokens.sign(SECRET, chat_id="123456789012", kind="t", arg="11", token="abcdef")
    assert len(data.encode()) <= tokens.MAX_CALLBACK_BYTES


def test_too_long_payload_raises():
    with pytest.raises(ValueError):
        tokens.sign(SECRET, chat_id="1", kind="t", arg="x" * 60, token="abc")


def test_tag_is_wide_enough():
    data = tokens.sign(SECRET, chat_id="42", kind="t", arg="1", token="abc123")
    assert len(data.rsplit(":", 1)[1]) == tokens.TAG_HEX >= 16, "32 бита перебираемы"
