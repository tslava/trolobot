"""Мягкая пост-обработка кандидата ДО выходного фильтра (CLAUDE.md,
"Интерфейсы: пост-обработка").

Решение владельца по живому чату: эмодзи в конце почти каждой реплики и
типографские тире (модель их скопировала из самого промпта) — это косметика,
а не нарушение характера. Молчать из-за них — слишком дорого, поэтому такие
вещи правятся руками, детерминированно, а не срезаются выходным фильтром
(``filters.py``). ``responder.py`` зовёт ``soften`` один раз, сразу после
``parse_reply`` и проверки ``speak``, до сборки ``FilterContext``/``check_output``
— дальше везде используется уже поправленный текст.

Чистые функции, без I/O. Детект эмодзи — те же примитивы, что ``regex:emoji`` в
``filters.py`` (``_is_emoji_char``/``_is_emoji_modifier``, реимпортированы отсюда,
цикла импорта нет: ``filters.py`` этот модуль не импортирует).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from trolobot.config_models import FiltersConfig
from trolobot.filters import _has_emoji, _is_emoji_char, _is_emoji_modifier

# «—» U+2014, «–» U+2013, «‒» U+2012, «―» U+2015 -> дефис-минус «-». Пробелы вокруг
# не в классе символов, поэтому не трогаются; дефис в словах («по-русски») уже «-»
# и не совпадает с классом.
_DASH_CHARS = "—–‒―"
_DASH_RE = re.compile(f"[{_DASH_CHARS}]")

_MULTI_SPACE_RE = re.compile(r" {2,}")
_SPACE_BEFORE_PUNCT_RE = re.compile(r" +([.,!?…:;])")

# Записи стикеров в recent_bot_replies ("[стикер #N] текст надписи") эмодзи не
# содержат, но на будущее — пропускаем их явно при подсчёте (CLAUDE.md).
_STICKER_PLACEHOLDER_PREFIX = "[стикер #"


@dataclass(frozen=True)
class Fixed:
    text: str
    # причины в стиле filter_log: "fix:dash", "fix:emoji_freq", "fix:emoji_count"
    fixes: tuple[str, ...]


def normalize_dashes(text: str) -> str:
    """Типографские тире -> дефис-минус «-». Пробелы вокруг и дефис в словах не трогаются."""
    return _DASH_RE.sub("-", text)


def _emoji_spans(text: str) -> list[tuple[int, int]]:
    """Индексы (start, end) каждого эмодзи в тексте вместе с модификаторами
    (вариационный селектор, тон кожи) — та же классификация символов, что у
    regex:emoji в filters.py, но со своим обходом (там хелпер отдаёт (char, end),
    без start)."""
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        if _is_emoji_char(text[i]):
            end = i + 1
            while end < n and _is_emoji_modifier(text[end]):
                end += 1
            spans.append((i, end))
            i = end
        else:
            i += 1
    return spans


def _remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return text
    parts: list[str] = []
    last = 0
    for start, end in spans:
        parts.append(text[last:start])
        last = end
    parts.append(text[last:])
    return "".join(parts)


def _cleanup(text: str) -> str:
    """После удаления эмодзи: схлопнуть повторы пробелов, снять пробел перед
    знаком препинания ("слово ," -> "слово,"), strip()."""
    cleaned = _MULTI_SPACE_RE.sub(" ", text)
    cleaned = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", cleaned)
    return cleaned.strip()


def strip_emoji(text: str) -> str:
    """Убрать ВСЕ эмодзи (вместе с модификаторами), без двойных/висячих пробелов."""
    spans = _emoji_spans(text)
    if not spans:
        return text
    return _cleanup(_remove_spans(text, spans))


def keep_first_emoji(text: str) -> str:
    """Оставить только первое эмодзи, остальные убрать тем же способом, что strip_emoji."""
    spans = _emoji_spans(text)
    if len(spans) <= 1:
        return text
    return _cleanup(_remove_spans(text, spans[1:]))


def _recent_has_emoji(recent_replies: Sequence[str], window: int) -> bool:
    if window <= 0:
        return False
    candidates = [
        reply
        for reply in recent_replies[-window:]
        if not reply.startswith(_STICKER_PLACEHOLDER_PREFIX)
    ]
    return any(_has_emoji(reply) for reply in candidates)


def _count_allowed_emoji(text: str, allowed_emoji: Sequence[str]) -> int:
    allowed = frozenset(allowed_emoji)
    return sum(1 for start, _end in _emoji_spans(text) if text[start] in allowed)


def soften(text: str, *, recent_replies: Sequence[str], cfg: FiltersConfig) -> Fixed:
    """Порядок правок: normalize_dashes -> emoji_freq -> emoji_count (см. CLAUDE.md).

    ``recent_replies`` — хронологически, последние записи свежие (как
    ``db.recent_bot_replies``); записи-стикеры при подсчёте эмодзи пропускаются.
    """
    fixes: list[str] = []

    dashed = normalize_dashes(text)
    if dashed != text:
        fixes.append("fix:dash")
    text = dashed

    if _has_emoji(text) and _recent_has_emoji(recent_replies, cfg.emoji_recent_window):
        text = strip_emoji(text)
        fixes.append("fix:emoji_freq")
    elif _count_allowed_emoji(text, cfg.allowed_emoji) > cfg.emoji_max_per_reply:
        text = keep_first_emoji(text)
        fixes.append("fix:emoji_count")

    return Fixed(text=text, fixes=tuple(fixes))
