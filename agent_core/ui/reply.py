"""Действия кнопок и ответ хода — транспорт-агностично (без Telethon).

Виды кнопок объявляет продукт: ядро знает только `Action`, `Reply` и тип клавиатуры.
"""
from __future__ import annotations

from dataclasses import dataclass, field

INLINE = "inline"
REPLY = "reply"


@dataclass(frozen=True)
class Action:
    """Одна кнопка. `arg` — короткий аргумент (индекс/код), НЕ данные из БД."""
    label: str
    kind: str
    arg: str = ""


@dataclass
class Reply:
    """Ответ хода: текст + (опционально) клавиатура.

    `kind` — тип ВСЕЙ клавиатуры: Telegram запрещает смешивать inline и reply-кнопки
    в одном сообщении (`ValueError: You cannot mix inline with normal buttons`).
    """
    text: str
    rows: list[list[Action]] = field(default_factory=list)
    kind: str = INLINE
    # Снять reply-меню: Telegram требует ОДИН `Button.clear()` и никаких других кнопок в сообщении.
    clear_keyboard: bool = False


def rows_to_payload(rows) -> list[list[dict]]:
    """Строки кнопок как ДАННЫЕ — для хранения (очередь).

    Почему это нужно: `Action` — объект, а не данные, и `json.dumps(payload)` его не знает.
    Производитель, положивший строки «как есть», получал от очереди отказ
    (`TypeError: Object of type Action is not JSON serializable`) и молча уезжал прямым путём —
    то есть очередь для него не работала вовсе. Формат берётся из ОДНОГО места (здесь), чтобы
    запись и чтение не разошлись.
    """
    return [[{"label": action.label, "kind": action.kind, "arg": action.arg} for action in row]
            for row in rows or []]


def rows_from_payload(rows) -> list[list[Action]]:
    """Обратная операция к `rows_to_payload` — восстановить строки из данных."""
    return [[Action(label=item["label"], kind=item["kind"], arg=item.get("arg", ""))
             for item in row] for row in rows or []]
