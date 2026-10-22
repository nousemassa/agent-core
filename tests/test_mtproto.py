"""Офлайн-тесты MTProto-транспорта: разбор прокси, сборка клиента, отсутствие сети."""
from __future__ import annotations

import pytest
from telethon import TelegramClient
from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate

from agent_core.config import CoreSettings
from agent_core.tg.mtproto import (
    build_client,
    client_kwargs,
    mtproxies,
    parse_mtproxy,
    parse_proxies,
)

MT_SECRET = "ee" + "a1b2" * 8 + "c3d4e5f6a7b8"  # синтетический: ee + 16Б ключ + hex(хост), НЕ боевой секрет
RAW_SECRET = "a1b2" * 8                                  # синтетический 16-байтный ключ


def _settings(**overrides) -> CoreSettings:
    base = {
        "_env_file": None,
        "telegram_bot_token": "123:abc",
        "telegram_api_id": "2040",
        "telegram_api_hash": "b" * 32,
        "telegram_mt_proxy_server": "10.0.0.1",
        "telegram_mt_proxy_port": "8095",
        "telegram_mt_proxy_secret": MT_SECRET,
    }
    return CoreSettings(**{**base, **overrides})


def test_parse_mtproxy_accepts_tme_and_raw_secrets():
    assert parse_mtproxy("10.0.0.1", "8095", MT_SECRET) == ("10.0.0.1", 8095, MT_SECRET)
    assert parse_mtproxy(" 10.0.0.1 ", 443, RAW_SECRET) == ("10.0.0.1", 443, RAW_SECRET)
    assert parse_mtproxy("h", "80", "dd" + RAW_SECRET)[2] == "dd" + RAW_SECRET
    assert parse_mtproxy("h", "80", MT_SECRET.upper())[2] == MT_SECRET, "секрет нормализуется в нижний регистр"


@pytest.mark.parametrize(
    ("server", "port", "secret"),
    [
        ("", "8095", MT_SECRET),          # нет хоста
        ("h", "abc", MT_SECRET),          # порт не число
        ("h", "0", MT_SECRET),            # порт вне диапазона
        ("h", "70000", MT_SECRET),        # порт вне диапазона
        ("h", "8095", ""),                # нет секрета
        ("h", "8095", "zzzz"),            # не hex
        ("h", "8095", "ee00ff"),          # короче 16 байт
    ],
)
def test_parse_mtproxy_rejects_bad_input(server, port, secret):
    with pytest.raises(ValueError):
        parse_mtproxy(server, port, secret)


def test_client_kwargs_uses_mtproxy_and_disables_ipv6():
    kwargs = client_kwargs(_settings())
    assert kwargs["connection"] is ConnectionTcpMTProxyRandomizedIntermediate
    assert kwargs["proxy"] == ("10.0.0.1", 8095, MT_SECRET)
    assert kwargs["use_ipv6"] is False, "IPv6 на dev-хосте не проходит наружу"


def test_client_kwargs_without_proxy_has_no_proxy():
    kwargs = client_kwargs(_settings(telegram_mt_proxy_server=""))
    assert "proxy" not in kwargs and "connection" not in kwargs


def test_client_kwargs_bad_proxy_raises():
    with pytest.raises(ValueError):
        client_kwargs(_settings(telegram_mt_proxy_secret="not-a-secret"))


def test_build_client_needs_api_credentials():
    with pytest.raises(ValueError, match="TELEGRAM_API_ID"):
        build_client(_settings(telegram_api_id=""))
    with pytest.raises(ValueError, match="TELEGRAM_API_HASH"):
        build_client(_settings(telegram_api_hash=""))


def test_build_client_constructs_offline_client():
    client = build_client(_settings())
    assert isinstance(client, TelegramClient)
    assert client.session is not None, "сессия в памяти (без файла на диске)"
    # Прокси не «игнорируется»: он уходит и в класс соединения, и в InitConnectionRequest.proxy
    assert client._init_request.proxy is not None


def test_build_client_without_proxy_sends_no_proxy_hint():
    client = build_client(_settings(telegram_mt_proxy_server=""))
    assert client._init_request.proxy is None


def test_build_client_uses_file_session_when_requested(tmp_path):
    """Сессия в памяти означает новый bot-логин на каждый рестарт, а за ним — FloodWait."""
    from telethon.sessions import SQLiteSession, StringSession

    from agent_core.testing import make_settings
    from agent_core.tg.mtproto import build_client

    settings = make_settings(
        telegram_api_id="12345", telegram_api_hash="deadbeef",
        telegram_mt_proxy_server="", telegram_mt_proxy_port="", telegram_mt_proxy_secret="",
    )
    memory_client = build_client(settings)
    assert isinstance(memory_client.session, StringSession), "без пути — прежнее поведение (память)"

    path = tmp_path / "news.session"
    client = build_client(settings, session_path=path)
    assert isinstance(client.session, SQLiteSession), "с путём — файл-сессия переживает рестарты"
    assert str(path) in str(client.session.filename)


# --- массив прокси: одиночная попытка подключения оставляла бота выключенным ---



def test_parse_proxies_accepts_link_and_triple_forms():
    """Обе формы из Telegram: ссылку человек копирует из канала, тройка удобна в `.env`."""
    entries, problems = parse_proxies(
        f"https://t.me/proxy?server=10.0.0.1&port=443&secret={MT_SECRET},"
        f"10.0.0.2:443:{RAW_SECRET}"
    )

    assert problems == ()
    assert entries == (("10.0.0.1", 443, MT_SECRET), ("10.0.0.2", 443, RAW_SECRET))


def test_parse_proxies_splits_on_comma_semicolon_and_newline():
    """Три разделителя равноправны: выбирать один было бы произволом."""
    raw = f"10.0.0.1:443:{RAW_SECRET};10.0.0.2:443:{RAW_SECRET}\n10.0.0.3:443:{RAW_SECRET}"

    entries, problems = parse_proxies(raw)

    assert problems == ()
    assert [entry[0] for entry in entries] == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]


def test_parse_proxies_keeps_working_entries_when_one_is_broken():
    """Опечатка в одной строке не должна уносить весь массив: он и нужен ради замены.

    Но причина отказа ВОЗВРАЩАЕТСЯ: иначе «прокси не подключается» выглядело бы свойством сети,
    а не опечаткой в конфиге.
    """
    entries, problems = parse_proxies(f"10.0.0.1:443:{RAW_SECRET}, мусор, 10.0.0.2:443:{RAW_SECRET}")

    assert [entry[0] for entry in entries] == ["10.0.0.1", "10.0.0.2"]
    assert len(problems) == 1 and "мусор" in problems[0]


def test_parse_proxies_drops_duplicates():
    """Один и тот же прокси дважды — не два прокси: иначе перебор ждал бы таймаут дважды."""
    entries, _ = parse_proxies(f"10.0.0.1:443:{RAW_SECRET},10.0.0.1:443:{RAW_SECRET}")

    assert entries == (("10.0.0.1", 443, RAW_SECRET),)


def test_parse_proxies_reports_missing_link_fields():
    entries, problems = parse_proxies("https://t.me/proxy?server=10.0.0.1&port=443")

    assert entries == ()
    assert problems and "secret" in problems[0]


def test_mtproxies_prefers_the_array_over_the_single_triple():
    settings = _settings(telegram_mt_proxies=f"10.0.0.7:443:{RAW_SECRET}")

    assert mtproxies(settings) == (("10.0.0.7", 443, RAW_SECRET),), "массив старше одиночной тройки"


def test_mtproxies_falls_back_to_the_single_triple():
    """Одиночная настройка остаётся рабочей: она стоит в живом `.env`, ломать её нельзя."""
    assert mtproxies(_settings()) == (("10.0.0.1", 8095, MT_SECRET),)


def test_mtproxies_empty_means_no_proxy_at_all():
    settings = _settings(telegram_mt_proxy_server="", telegram_mt_proxy_port="", telegram_mt_proxy_secret="")

    assert mtproxies(settings) == ()


def test_client_kwargs_uses_the_chosen_proxy():
    """`build_client` получает КОНКРЕТНЫЙ прокси: перебор массива выбирает его снаружи."""
    kwargs = client_kwargs(_settings(), proxy=("10.0.0.5", 8443, RAW_SECRET))

    assert kwargs["proxy"] == ("10.0.0.5", 8443, RAW_SECRET)
    assert kwargs["connection"] is ConnectionTcpMTProxyRandomizedIntermediate
