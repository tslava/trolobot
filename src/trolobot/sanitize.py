"""Чистые синхронные функции для очистки пользовательских данных.

Ничего не знают про aiogram и не делают I/O — см. CLAUDE.md, раздел "Конвенции".
Используются перед записью в БД (display_name, текст сообщения) и перед подстановкой
данных в промпт, чтобы участник чата не мог подделать формат контекста или
внедрить инструкции через своё имя/текст сообщения (PLAN.md, этап 1 и этап 3).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Protocol

# Ограничение длины отображаемого имени в контексте (символов).
_MAX_DISPLAY_NAME_LEN = 24

# Модуль стабильного числа для "Участник N" — см. stable_n().
_STABLE_N_MODULUS = 997

_MULTI_SPACE_RE = re.compile(r" {2,}")
_WHITESPACE_RUN_RE = re.compile(r"\s+")
_LT_RUN_RE = re.compile(r"<{3,}")
_GT_RUN_RE = re.compile(r">{3,}")


def stable_n(user_id: int) -> int:
    """Детерминированное число 1..999 для f"Участник {n}", стабильное по user_id.

    Реализация: user_id % 997 + 1. 997 — простое число, близкое к 999, так что
    результат почти равномерно покрывает диапазон 1..997. Единственное важное
    свойство — стабильность (один user_id всегда даёт одно и то же число),
    а не равномерность или отсутствие коллизий между разными user_id.
    """
    return user_id % _STABLE_N_MODULUS + 1


def sanitize_display_name(raw: str | None, user_id: int, reserved: set[str]) -> str:
    """Санитизирует display_name перед записью в БД и подстановкой в промпт.

    Оставляет только буквы любого алфавита (str.isalpha), пробельные символы
    (любой из них становится обычным пробелом) и дефис —
    эмодзи, цифры, кавычки и прочие знаки вырезаются. Повторные пробелы
    схлопываются, результат обрезается до 24 символов (по границе слова,
    если она есть в пределах обрезки). Если после этого имя пустое, целиком
    совпадает без учёта регистра с одним из `reserved`, или содержит любой
    элемент `reserved` как отдельное слово (без учёта регистра) — возвращается
    f"Участник {stable_n(user_id)}", чтобы никто не мог представиться именем
    бота или его триггером и подменить в контексте, кто говорит.

    Регистр в самом имени сохраняется.
    """
    text = raw or ""
    filtered_chars = (
        " " if ch.isspace() else ch for ch in text if ch.isalpha() or ch.isspace() or ch == "-"
    )
    filtered = _MULTI_SPACE_RE.sub(" ", "".join(filtered_chars)).strip()

    if len(filtered) > _MAX_DISPLAY_NAME_LEN:
        truncated = filtered[:_MAX_DISPLAY_NAME_LEN]
        if " " in truncated:
            truncated = truncated.rsplit(" ", 1)[0]
        filtered = truncated.strip()

    fallback = f"Участник {stable_n(user_id)}"

    if not filtered:
        return fallback

    reserved_words = {item.strip().casefold() for item in reserved if item.strip()}
    if filtered.casefold() in reserved_words:
        return fallback
    for word in reserved_words:
        if re.search(rf"\b{re.escape(word)}\b", filtered, re.IGNORECASE):
            return fallback

    return filtered


def normalize_text(raw: str | None) -> str:
    """Нормализует произвольный пользовательский текст перед записью/подстановкой.

    None -> "". Переносы строк, табы и прочие управляющие символы заменяются
    пробелом (чтобы никто не мог подделать многострочный формат "Имя: текст"
    в контексте промпта). Нулевые и невидимые символы (zero-width, BOM и т.п.)
    удаляются целиком. Последовательности из трёх и более подряд `<` или `>`
    заменяются пробелом (чтобы участник не мог подделать разделители
    `<<<CHAT ... >>>`, которыми промпт оборачивает данные — см. PLAN.md,
    этап 3, "Данные отдельно от инструкций"). Обычная пунктуация и эмодзи
    не трогаются. В конце — схлопывание пробелов и strip.
    """
    if raw is None:
        return ""

    # Нулевые и невидимые символы (категория Cf: zero-width space/joiner, BOM...)
    # вырезаются целиком, а не заменяются пробелом.
    without_invisible = "".join(
        ch for ch in raw if ch != "\x00" and unicodedata.category(ch) != "Cf"
    )

    # Остальные управляющие символы (переносы строк, табы и т.п.) -> пробел.
    without_control = "".join(
        " " if unicodedata.category(ch) == "Cc" else ch for ch in without_invisible
    )

    without_fake_delimiters = _GT_RUN_RE.sub(" ", _LT_RUN_RE.sub(" ", without_control))

    return _WHITESPACE_RUN_RE.sub(" ", without_fake_delimiters).strip()


class MediaMessage(Protocol):
    """Структурный тип сообщения с медиа-атрибутами aiogram (без импорта aiogram).

    Любой объект с такими атрибутами (например, aiogram Message или
    types.SimpleNamespace в тестах) подходит без явного наследования.
    """

    @property
    def sticker(self) -> object | None: ...
    @property
    def photo(self) -> object | None: ...
    @property
    def voice(self) -> object | None: ...
    @property
    def audio(self) -> object | None: ...
    @property
    def video(self) -> object | None: ...
    @property
    def video_note(self) -> object | None: ...
    @property
    def animation(self) -> object | None: ...
    @property
    def document(self) -> object | None: ...


def media_placeholder(message: MediaMessage) -> str | None:
    """Плейсхолдер для медиа-сообщения без текста, иначе None.

    Порядок приоритета (первое совпадение побеждает, если у сообщения
    заполнено сразу несколько полей): sticker -> "[стикер]", photo -> "[фото]",
    voice/audio -> "[голосовое]", video/video_note/animation -> "[видео]",
    document -> "[файл]".
    """
    if message.sticker is not None:
        return "[стикер]"
    if message.photo is not None:
        return "[фото]"
    if message.voice is not None or message.audio is not None:
        return "[голосовое]"
    if message.video is not None or message.video_note is not None or message.animation is not None:
        return "[видео]"
    if message.document is not None:
        return "[файл]"
    return None
