"""Тесты нормализации jsonb из asyncpg (иначе state диалога уезжает строкой)."""
from __future__ import annotations

from agent_core.db.store import decode_jsonb


def test_decode_jsonb_string_form():
    assert decode_jsonb('{"entity_id": "L-1", "amount": null}') == {"entity_id": "L-1", "amount": None}


def test_decode_jsonb_none_and_dict_passthrough():
    assert decode_jsonb(None) == {}
    assert decode_jsonb({"step": "name"}) == {"step": "name"}


def test_decode_jsonb_nested_and_unicode():
    raw = '{"address": "\u0443\u043b. \u041f\u0443\u0448\u043a\u0438\u043d\u0430", "rooms": 2}'
    assert decode_jsonb(raw)["rooms"] == 2


def test_parse_slot_accepts_iso_and_space_forms():
    from datetime import UTC, datetime

    from agent_core.db.store import parse_slot

    expected = datetime(2026, 10, 1, 11, 0, tzinfo=UTC)  # по умолчанию naive-слот = UTC
    assert parse_slot("2026-10-01T11:00") == expected
    assert parse_slot("2026-10-01 11:00") == expected
    assert parse_slot(" 2026-10-01T11:00 ") == expected


def test_parse_slot_rejects_garbage():
    import pytest

    from agent_core.db.store import parse_slot

    with pytest.raises(ValueError):
        parse_slot("завтра в 11")


def test_parse_slot_empty_raises_clear_error():
    import pytest

    from agent_core.db.store import parse_slot

    with pytest.raises(ValueError, match="пустой слот"):
        parse_slot("")


def test_parse_slot_binds_naive_time_to_business_timezone():
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from agent_core.db.store import parse_slot

    msk = ZoneInfo("Europe/Moscow")
    moment = parse_slot("2026-10-01T10:00", tz=msk)
    assert moment.tzinfo == msk, "naive-слот трактуем как время бизнес-таймзоны, а не UTC"
    assert moment.astimezone(UTC).hour == 7, "10:00 МСК = 07:00 UTC"
    assert parse_slot("2026-10-01T10:00+03:00", tz=msk) == datetime.fromisoformat("2026-10-01T10:00+03:00"), (
        "явный offset в строке не перетираем"
    )
