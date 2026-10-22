"""Спецификация промпта роли: файл агента + его шапка как ЕДИНСТВЕННЫЙ источник параметров хода.

**Зачем отдельный модуль.** Своему рантайму (`own`) нужны системный промпт, `temperature` и
`max_tokens`. Все три уже написаны человеком в шапке файла агента
(шапка файла агента), и второе место для них (конфиг, константа в коде) означало бы
две правды: правку в файле никто бы не заметил, а поведение агента разъехалось бы молча.

**Почему файл, а не платформа.** Пока ход исполняет платформа, эти параметры живут в её ревизии. Свой
рантайм обязан работать БЕЗ неё, значит параметры читаются из репозитория. Файл приходит в образ
файл кладёт в образ продукт, а путь задаёт настройка — модуль пути НЕ угадывает.

**Почему отказ, а не дефолт.** Пропавший `max_tokens` — это не «возьмём 400 по памяти»: без него
провайдер выбирает бюджет сам, и счёт за ход становится непредсказуемым (в клиенте продукта его нет
вовсе). Поэтому шапка без поля — `PromptSpecError` с путём и тем, чего не хватило.

**Чего здесь НЕТ и почему.** Модель провайдера (`chat_credentials()`) сюда не входит: шапка называет
модель ПЛАТФОРМЫ (строка вида `provider/model`), а в тело запроса идёт модель из настроек. Поэтому
`model_platform` хранится для отчёта и сверки, а не подставляется в запрос.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: Шапка промпта: ровно одно совпадение каждого поля. Шаблоны нарочно узкие — «похожее» поле не должно
#: приниматься за нужное (иначе опечатка в шапке станет тихой сменой бюджета).
#: ❗ Модель бывает и в обратных кавычках, и голой строкой до скобки
#: (`listing-writer.md`: «`gpt-4o` (снизить до mini…)»), поэтому два варианта одной группой выбора.
_MODEL = re.compile(r"\*\*Модель\*\*:\s*(?:`([^`]+)`|([^\s(.]+))")
_TEMPERATURE = re.compile(r"temperature\s+([0-9]+(?:\.[0-9]+)?)")
_MAX_TOKENS = re.compile(r"max_tokens\s+([0-9]+)")


class PromptSpecError(ValueError):
    """Шапка промпта не разобрана. Текст называет файл и что именно не так — без догадок."""


@dataclass(frozen=True)
class PromptSpec:
    """Параметры хода, снятые с файла агента."""

    path: Path
    system_text: str          # весь файл — системное сообщение
    model_platform: str       # как модель названа в шапке (для отчёта; в запрос идёт модель настроек)
    temperature: float
    max_tokens: int


def _one(pattern: re.Pattern[str], text: str, *, what: str, path: Path) -> str:
    found = pattern.findall(text)
    if len(found) != 1:
        raise PromptSpecError(
            f"{path}: в шапке {what} найдено {len(found)} раз, ожидаю ровно один. "
            f"Файл — единственный источник параметров хода, поэтому угадывать не буду."
        )
    #: Групповая форма (`(?:a|b)`) даёт кортеж: берём непустую половину, а не первую по счёту.
    match = found[0]
    return next(part for part in (match if isinstance(match, tuple) else (match,)) if part)


def spec_for(path: str | Path) -> PromptSpec:
    """Прочитать файл агента и снять с шапки модель, `temperature` и `max_tokens`."""
    file = Path(path)
    if not file.is_file():
        raise PromptSpecError(
            f"файл промпта не найден: {file}. Путь задаёт настройка `DIALOG_PROMPT_PATH`; "
            f"в контейнере файл обязан лежать в образе — его кладёт туда продукт."
        )
    text = file.read_text(encoding="utf-8")
    return PromptSpec(
        path=file,
        system_text=text,
        model_platform=_one(_MODEL, text, what="`**Модель**: `…``", path=file),
        temperature=float(_one(_TEMPERATURE, text, what="`temperature`", path=file)),
        max_tokens=int(_one(_MAX_TOKENS, text, what="`max_tokens`", path=file)),
    )
