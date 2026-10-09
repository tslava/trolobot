"""Шутки чата (CLAUDE.md, "шутки чата и истории людей").

Недельный ``ChatMemorizer`` достаёт из переписки словечки, которые чат подхватил, и
кладёт их в таблицу ``chat_jokes``. ``render_jokes`` собирает из них абзац для слота
``{jokes}`` системного промпта («можно к месту вернуть одну»), а ``joke_used`` решает,
вернул ли персонаж шутку в только что отправленной реплике — тогда responder ставит ей
``last_used_at`` и шутка отдыхает ``cooldown_days`` суток.

Чистые функции, без I/O.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from trolobot.db import JokeRow

_HEADER = (
    "Шутки и словечки вашего чата (можно к месту вернуть одну, не чаще раза в несколько дней): "
)

_LT_RUN_RE = re.compile(r"<{3,}")
_GT_RUN_RE = re.compile(r">{3,}")
# Ведущий буллет/нумерация: строка слота не должна начинаться с «- » (regex:prompt_leak).
_BULLET_RE = re.compile(r"^\s*(?:[-*•–—]+\s+|\d+[.)]\s+)")
_WORD_RE = re.compile(r"\w+", re.UNICODE)

# Сколько общих слов подряд считается «шутку вернули» (и порог «короткой» шутки).
_MIN_RUN = 3


def _clean(text: str) -> str:
    cleaned = _GT_RUN_RE.sub(" ", _LT_RUN_RE.sub(" ", text))
    cleaned = _BULLET_RE.sub("", " ".join(cleaned.split()))
    return cleaned.strip().rstrip(".;")


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower().replace("ё", "е"))


def render_jokes(rows: Sequence[JokeRow], *, now: int, cooldown_days: int) -> str:
    """Блок слота ``{jokes}``. Пусто -> "" (абзац со слотом в system.txt пропадает).

    Шутка показывается, если её ещё не использовали или с использования прошло
    не меньше ``cooldown_days`` суток. Одна строка, без «- » в начале."""
    cooldown = cooldown_days * 86400
    parts: list[str] = []
    for row in rows:
        if row.last_used_at is not None and now - row.last_used_at < cooldown:
            continue
        cleaned = _clean(row.text)
        if cleaned:
            parts.append(f"«{cleaned}»")
    if not parts:
        return ""
    return _HEADER + "; ".join(parts) + "."


def joke_used(text: str, joke: str) -> bool:
    """Шутка прозвучала в ``text``: 3+ общих нормализованных слова подряд, либо шутка
    короче трёх слов и целиком (по словам, подряд) входит в текст."""
    text_words = _words(text)
    joke_words = _words(joke)
    if not joke_words or not text_words:
        return False
    if len(joke_words) < _MIN_RUN:
        size = len(joke_words)
        return any(
            text_words[i : i + size] == joke_words for i in range(len(text_words) - size + 1)
        )
    text_grams = {
        tuple(text_words[i : i + _MIN_RUN]) for i in range(len(text_words) - _MIN_RUN + 1)
    }
    return any(
        tuple(joke_words[i : i + _MIN_RUN]) in text_grams
        for i in range(len(joke_words) - _MIN_RUN + 1)
    )
