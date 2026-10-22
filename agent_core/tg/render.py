"""Единственное место привязки UI-слоя к Telegram (Telethon).

Telegram запрещает смешивать inline и reply-кнопки в ОДНОМ сообщении
(`ValueError: You cannot mix inline with normal buttons`), поэтому тип задаётся для всей клавиатуры
(`Reply.kind`), а не для отдельной кнопки — и проверяется здесь ДО обращения к Telegram, чтобы ошибка
была понятной, а не «тихой».

Виды кнопок объявляет продукт: рендер получает `ButtonPolicy` — какие виды имеют фиксированный
callback, какие требуют reply-раскладки, какие рендерятся запросом телефона. Остальные подписываются
`ui/tokens.py` (привязаны к чату и токену хода).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from telethon import Button

from agent_core.ui import tokens
from agent_core.ui.reply import REPLY, Action, Reply


@dataclass(frozen=True)
class ButtonPolicy:
    """Что рендер знает о видах кнопок продукта."""

    #: Вид → фиксированный `callback_data` (без подписи: побочных действий нет).
    callbacks: Mapping[str, str] = field(default_factory=dict)
    #: Виды, которые рендерятся запросом телефона (`Button.request_phone`).
    phone_kinds: frozenset[str] = frozenset()
    #: Виды, допустимые в reply-раскладке.
    reply_kinds: frozenset[str] = frozenset()


DEFAULT_POLICY = ButtonPolicy()


def to_buttons(reply: Reply, *, secret: str, chat_id: str, token: str,
               policy: ButtonPolicy = DEFAULT_POLICY) -> list[list] | None:
    """Клавиатура сообщения. Без UI_SECRET подписанные кнопки не рендерим: они были бы мёртвыми."""
    if not reply.rows:
        return None
    _check_layout(reply, policy)
    rows: list[list] = []
    for row in reply.rows:
        buttons = [button for button in
                   (_to_button(action, keyboard=reply.kind, secret=secret, chat_id=chat_id, token=token,
                               policy=policy)
                    for action in row)
                   if button is not None]
        if buttons:
            rows.append(buttons)
    return rows or None


def _check_layout(reply: Reply, policy: ButtonPolicy) -> None:
    kinds = {action.kind for row in reply.rows for action in row}
    if reply.kind == REPLY and kinds - set(policy.reply_kinds):
        raise ValueError(
            f"reply-клавиатура допускает только объявленные виды, а получено: {sorted(kinds)}"
        )


def _to_button(action: Action, *, keyboard: str, secret: str, chat_id: str, token: str,
               policy: ButtonPolicy):
    if action.kind in policy.callbacks:
        return Button.inline(action.label, policy.callbacks[action.kind])
    if action.kind in policy.phone_kinds:
        # Только reply-раскладка: Telegram отдаёт контакт, только если кнопка в reply-клавиатуре.
        return Button.request_phone(action.label)
    if keyboard == REPLY:
        return Button.text(action.label)
    if not secret:
        # Подписанные кнопки без секрета не работают (verify вернёт None) — лучше не показывать вовсе.
        return None
    data = tokens.sign(secret, chat_id=chat_id, kind=action.kind, arg=action.arg, token=token)
    return Button.inline(action.label, data)


async def send(event, reply: Reply, *, secret: str, chat_id: str, token: str = "",
               policy: ButtonPolicy = DEFAULT_POLICY) -> None:
    """Отправляет ответ хода. Пустой текст сохраняет прежний фолбэк «…»."""
    if reply.clear_keyboard and reply.rows:
        raise ValueError("снятие меню несовместимо с кнопками: Telegram оставит только clear")
    await event.respond(
        reply.text or "…",
        buttons=to_buttons(reply, secret=secret, chat_id=chat_id, token=token, policy=policy),
        clear_buttons=reply.clear_keyboard,
    )
