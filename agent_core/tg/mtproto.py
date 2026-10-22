"""MTProto-транспорт: разбор прокси-параметров и сборка Telethon-клиента.

Почему MTProto, а не Bot API: с этого хоста `api.telegram.org` заблокирован (HTTPS 000 по
IPv4 и IPv6), а MTProto-прокси из t.me-ссылки работает — он проксирует протокол Telegram,
поэтому сервера Telegram отвечают через него: через прокси приходит `AccessTokenExpiredError`, а на
подставном адресе — `ConnectionRefused`/`Timeout`.

Секрет передаём **verbatim** из ссылки `t.me/proxy?...&secret=ee...`: Telethon сам снимает
префикс `ee`/`dd` и отбрасывает hex-домен (`normalize_secret` в
`telethon/network/connection/tcpmtproxy.py`). Важно: fake-TLS-режим Telethon пока не
поддерживает (в исходнике «until domain support is added») — прокси работает в
randomized-intermediate.
"""
from __future__ import annotations

import pathlib
import re
import urllib.parse
import warnings

from telethon import TelegramClient
from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate
from telethon.sessions import SQLiteSession, StringSession

HEX_DIGITS = set("0123456789abcdef")
MIN_SECRET_HEX = 32  # 16 байт


def parse_mtproxy(server: str, port: str | int, secret: str) -> tuple[str, int, str]:
    """Нормализует и валидирует MTProto-прокси. Возвращает (host, port, secret-verbatim)."""
    host = (server or "").strip()
    if not host:
        raise ValueError("пустой MTProto-хост")
    try:
        number = int(str(port).strip())
    except ValueError as error:
        raise ValueError(f"порт не число: {port!r}") from error
    if not 1 <= number <= 65535:
        raise ValueError(f"порт вне диапазона 1..65535: {number}")

    raw = (secret or "").strip().lower()
    body = raw[2:] if raw[:2] in ("ee", "dd") else raw
    if len(body) < MIN_SECRET_HEX or any(char not in HEX_DIGITS for char in body):
        raise ValueError(
            "секрет MTProto должен быть hex: 32 символа (16 байт) "
            "или ee/dd + 32 hex-символа + hex-домен (как в t.me-ссылке)"
        )
    return host, number, raw


# Разделители записей массива. Запятая, точка с запятой и перевод строки — все три удобны в .env,
# и выбирать один было бы произволом: человек пишет так, как ему читается.
_PROXY_SPLIT = re.compile(r"[,;\n]+")


def parse_proxy_entry(entry: str) -> tuple[str, int, str]:
    """Разобрать ОДНУ запись массива прокси. Поддержаны обе формы, которые даёт Telegram.

    * ссылка `https://t.me/proxy?server=…&port=…&secret=…` — то, что человек копирует из канала;
    * тройка `сервер:порт:секрет` — компактная форма для `.env`.

    Проверка одна на обе формы: `parse_mtproxy`. Бросает `ValueError` с причиной.
    """
    raw = (entry or "").strip()
    if not raw:
        raise ValueError("пустая запись")
    if "t.me/" in raw or raw.startswith(("http://", "https://", "tg://")):
        query = urllib.parse.urlparse(raw).query or urllib.parse.urlparse(raw).path.partition("?")[2]
        params = urllib.parse.parse_qs(query)
        missing = [name for name in ("server", "port", "secret") if not params.get(name)]
        if missing:
            raise ValueError(f"в ссылке нет полей: {', '.join(missing)}")
        return parse_mtproxy(params["server"][0], params["port"][0], params["secret"][0])
    parts = raw.split(":")
    if len(parts) < 3:
        raise ValueError("ожидается ссылка t.me/proxy?... или тройка сервер:порт:секрет")
    secret = ":".join(parts[2:])  # секрет может содержать двоеточие? нет, но лишнее не отрезаем молча
    return parse_mtproxy(parts[0], parts[1], secret)


def parse_proxies(raw: str) -> tuple[tuple[tuple[str, int, str], ...], tuple[str, ...]]:
    """Разобрать МАССИВ прокси. Возвращает (годные, причины отказа).

    **Почему негодная запись не отменяет остальные.** Массив нужен ровно для того, чтобы прокси
    заменялись по очереди; выкинуть весь список из-за опечатки в одной строке значило бы потерять и
    рабочие. Но и молчать нельзя: причина отказа возвращается вызывающему и пишется в журнал —
    иначе «прокси не подключается» выглядит свойством сети, а не опечаткой в конфиге.
    """
    good: list[tuple[str, int, str]] = []
    problems: list[str] = []
    for entry in _PROXY_SPLIT.split(raw or ""):
        if not entry.strip():
            continue
        try:
            proxy = parse_proxy_entry(entry)
        except ValueError as error:
            problems.append(f"{entry.strip()[:40]}… — {error}")
            continue
        if proxy not in good:
            good.append(proxy)
    return tuple(good), tuple(problems)


def mtproxies(settings) -> tuple[tuple[str, int, str], ...]:
    """Массив прокси из настроек, с откатом на одиночную тройку.

    Одиночные `TELEGRAM_MT_PROXY_SERVER/PORT/SECRET` остаются рабочими: они стоят в живом `.env`, и
    ломать работающую настройку ради новой формы нельзя. Массив старше: если задан — берётся он.
    """
    proxies, _problems = parse_proxies(getattr(settings, "telegram_mt_proxies", "") or "")
    if proxies:
        return proxies
    if settings.telegram_mt_proxy_server:
        return (parse_mtproxy(
            settings.telegram_mt_proxy_server,
            settings.telegram_mt_proxy_port,
            settings.telegram_mt_proxy_secret,
        ),)
    return ()


def client_kwargs(settings, *, proxy: tuple[str, int, str] | None = None) -> dict:
    """Kwargs для TelegramClient: прокси (если задан) и отключённый IPv6.

    IPv6 с этого хоста наружу не проходит (HTTPS 000 по IPv6 при работающем ICMP), поэтому не даём
    Telethon доходить до IPv6-адресов Telegram.
    """
    kwargs: dict = {"use_ipv6": False}
    chosen = proxy
    if chosen is None:
        configured = mtproxies(settings)
        chosen = configured[0] if configured else None
    if chosen is not None:
        kwargs["connection"] = ConnectionTcpMTProxyRandomizedIntermediate
        kwargs["proxy"] = chosen
    return kwargs


def build_client(settings, *, session_path: str | pathlib.Path | None = None,
                 proxy: tuple[str, int, str] | None = None) -> TelegramClient:
    """Клиент без подключения: `await client.start(bot_token=...)` — уже в транспорте.

    `session_path` — файл-сессия: авторизация переживает рестарты и НЕ тратит bot-логины
    (`ImportBotAuthorizationRequest`), на которые Telegram накладывает FloodWait: частые перезапуски
    дают ответ «A wait of N seconds is required» на часы вперёд.
    Без `session_path` поведение прежнее — сессия в памяти.
    """
    api_id = str(settings.telegram_api_id or "").strip()
    if not api_id.isdigit():
        raise ValueError("TELEGRAM_API_ID не задан или не число (для MTProto-логина нужен api_id)")
    if not (settings.telegram_api_hash or "").strip():
        raise ValueError("TELEGRAM_API_HASH не задан (для MTProto-логина нужен api_hash)")
    # Предупреждение Telethon «proxy argument will be ignored because python-socks is not
    # installed» к MTProxy НЕ относится: python-socks нужен только для HTTP/SOCKS, а для
    # TcpMTProxy прокси уходит в сам класс соединения и в InitConnectionRequest.proxy
    # (`init_proxy = None if not issubclass(connection, TcpMTProxy) else …` и
    # `self._connection(…, proxy=self._proxy)` в telegrambaseclient.py). Гасим шум,
    # чтобы не искали несуществующую проблему и не тянули лишнюю зависимость.
    session = StringSession()  # сессия в памяти: бот-логин восстанавливается из токена
    if session_path is not None:
        path = pathlib.Path(session_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        session = SQLiteSession(str(path))
        try:
            # Файл-сессия — это credential: только владельцу. Если файл создастся позже, chmod сработает на след. старте.
            path.chmod(0o600)
        except OSError:
            pass
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="proxy argument will be ignored", category=UserWarning)
        return TelegramClient(
            session,
            int(api_id),
            settings.telegram_api_hash.strip(),
            timeout=20,
            connection_retries=3,
            retry_delay=2,
            request_retries=3,
            **client_kwargs(settings, proxy=proxy),
        )
