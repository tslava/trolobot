"""Типы гейта (этап 2). Чистые данные, без I/O и без aiogram.

Гейт решает, рассматривать ли сообщение как повод для ответа. Он ничего не пишет:
изменения состояния возвращаются в Decision, применяет их вызывающий код.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class Trigger(StrEnum):
    MENTION = "mention"  # @username в тексте
    REPLY = "reply"  # реплай на сообщение бота
    NAME = "name"  # слово из persona.name_triggers
    AMBIENT = "ambient"  # без обращения, внутрь живого разговора


class Verdict(StrEnum):
    PASS = "pass"  # рассматривать: дальше дебаунс и генерация (этап 3)
    QUEUE_NIGHT = "queue_night"  # обращение ночью: в night_queue, ответ утром
    DROP = "drop"  # молчание, причина в reason


@dataclass(frozen=True, slots=True)
class GateMessage:
    chat_id: int
    tg_message_id: int
    user_id: int
    is_bot: bool
    text: str  # уже normalize_text
    reply_to_bot: bool  # реплай на сообщение бота
    created_at: int  # unix seconds


@dataclass(frozen=True, slots=True)
class RecentActivity:
    """Одно сообщение не-бота за окно live_talk. Нужно только кто и когда."""

    user_id: int
    created_at: int


@dataclass(frozen=True, slots=True)
class GateState:
    """Снимок состояния на момент решения. Собирается из таблицы state и messages."""

    panic: bool
    stop_until: int | None
    topic_cooldown_until: int | None
    muted_user_ids: frozenset[int]
    mention_count_today: int
    last_mention_reply_at: int | None  # по чату
    last_mention_reply_at_user: int | None  # по автору текущего сообщения
    ambient_count_today: int  # ambient + spontaneous
    last_ambient_at: int | None
    recent: tuple[RecentActivity, ...]  # не-бот сообщения за live_talk.window_min, включая текущее


@dataclass(frozen=True, slots=True)
class StateChange:
    key: str
    value: str | None  # None = удалить ключ


@dataclass(frozen=True, slots=True)
class Decision:
    verdict: Verdict
    trigger: Trigger | None  # при PASS и QUEUE_NIGHT
    reason: str  # "gate:is_bot", "gate:night_queued", ... ; при PASS — "pass:<trigger>"
    state_changes: tuple[StateChange, ...] = ()


class PatternsLike(Protocol):
    """Интерфейс patterns.Patterns, чтобы гейт тестировался с подделкой."""

    def topic_stop(self, text: str) -> str | None: ...
    def injection(self, text: str) -> str | None: ...
    def logistics(self, text: str) -> str | None: ...
    def name_trigger(self, text: str) -> str | None: ...
    def mentions_bot(self, text: str) -> bool: ...
