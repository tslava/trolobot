"""Реакции-эмодзи на чужие сообщения, срезанные гейтом по кубику (CLAUDE.md,
"Интерфейсы: реакции").

Решение владельца: когда гейт срезает сообщение по недетерминированной причине
(``gate:dice`` или ``gate:ambient_cooldown``) — вместо полного молчания бот иногда
ставит реакцию через Bot API ``setMessageReaction``. Дёшево: без вызова модели,
без нового сообщения, эффект присутствия. Решение о реакции живёт снаружи гейта
(``gate.py`` не меняется) — этот модуль вызывается из ``bot.py`` уже после того,
как гейт вернул ``Verdict.DROP``.

``pick_reaction`` — чистая функция, как и ``should_consider`` в ``gate.py``: на входе
причина дропа, автор, снимок состояния, конфиг, ``rng`` и ``now``, на выходе — эмодзи
или ``None``. Состояние не меняет, изменения делает вызывающий (``react``) после
успешной отправки реакции.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Protocol

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ReactionTypeEmoji, ReactionTypeUnion

from trolobot.config_models import ReactionsConfig
from trolobot.db import Database
from trolobot.timeutil import day_key

logger = logging.getLogger(__name__)

# Причины DROP гейта, на которые вместо молчания иногда ставится реакция —
# обе недетерминированные ("кости" п.11 и кулдаун ambient п.10). Все прочие причины
# (panic/stop/muted/topic/night/logistics/not_live/injection/mention_cap/ambient_cap...) —
# там решено молчать полностью, реакция не ставится никогда.
REACT_REASONS: frozenset[str] = frozenset({"gate:dice", "gate:ambient_cooldown"})


class ReactionBotLike(Protocol):
    """Узкий протокол вместо ``aiogram.Bot`` — тесты подделывают его без aiogram
    (по образцу ``responder._BotLike``).

    ``reaction`` типизирован как ``list[ReactionTypeUnion] | None`` (тип реального
    ``Bot.set_message_reaction``, не только ``ReactionTypeEmoji``): ``list`` в Python
    инвариантен по параметру, так что более узкий тип здесь не прошёл бы структурную
    проверку mypy при передаче настоящего ``aiogram.Bot`` в ``Deps.bot``.
    """

    async def set_message_reaction(
        self,
        chat_id: int,
        message_id: int,
        reaction: list[ReactionTypeUnion] | None = None,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class ReactionState:
    """Снимок состояния реакций на момент решения (по образцу ``GateState``)."""

    last_reaction_at: int | None
    last_reaction_user_id: int | None
    count_today: int


def _parse_int_state(key: str, raw: str | None) -> int | None:
    """int(raw) с дефолтом None; мусор в значении -> WARNING в лог, не падать
    (дублирует gate_state._parse_int_state — тот приватный к своему модулю)."""
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("reactions: garbage value %r for state key %r, using default", raw, key)
        return None


def pick_reaction(
    *,
    drop_reason: str,
    user_id: int,
    state: ReactionState,
    cfg: ReactionsConfig,
    rng: random.Random,
    now: int,
) -> str | None:
    """Решить, ставить ли реакцию на срезанное сообщение, и какую.

    Порядок проверок (CLAUDE.md, "Интерфейсы: реакции"): enabled -> причина ->
    потолок -> кулдаун -> тот же пользователь -> кубик. Кубик — последним, чтобы
    ``rng`` тратился только когда реакция вообще возможна: тесты с фиксированным
    seed так стабильнее (ровно как одиннадцатый шаг гейта — последний).
    """
    if not cfg.enabled:
        return None
    if drop_reason not in REACT_REASONS:
        return None
    if state.count_today >= cfg.daily_cap:
        return None
    if state.last_reaction_at is not None and state.last_reaction_at + cfg.cooldown_min * 60 > now:
        return None
    if state.last_reaction_user_id is not None and state.last_reaction_user_id == user_id:
        return None
    if rng.random() >= cfg.probability:
        return None
    return rng.choice(cfg.emoji)


async def load_reaction_state(db: Database, tz: str, now: int) -> ReactionState:
    """Снимок state для pick_reaction. Сутки счётчика — по persona.timezone,
    как у mention_count/ambient_count (timeutil.day_key)."""
    last_reaction_at_raw = await db.get_state("last_reaction_at")
    last_reaction_user_id_raw = await db.get_state("last_reaction_user_id")
    count_key = day_key("reaction_count", now, tz)
    count_raw = await db.get_state(count_key)
    return ReactionState(
        last_reaction_at=_parse_int_state("last_reaction_at", last_reaction_at_raw),
        last_reaction_user_id=_parse_int_state("last_reaction_user_id", last_reaction_user_id_raw),
        count_today=_parse_int_state("reaction_count", count_raw) or 0,
    )


async def react(
    bot: ReactionBotLike,
    db: Database,
    *,
    chat_id: int,
    tg_message_id: int,
    user_id: int,
    emoji: str,
    tz: str,
    now: int,
) -> bool:
    """Поставить реакцию и обновить состояние. Провал (реакции запрещены в чате,
    сообщение уже удалено) -> WARNING + filter_log react:error, состояние не трогать.

    Другие исключения не ловятся — их ловит общий except хендлера в bot.py.
    """
    try:
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=tg_message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.warning("reaction failed on message %s: %s", tg_message_id, exc)
        await db.insert_filter_log(
            trigger_tg_message_id=tg_message_id,
            candidate_text=emoji,
            verdict="cut",
            stage="react",
            reason="react:error",
            shadow=False,
            created_at=now,
        )
        return False

    await db.set_state("last_reaction_at", str(now))
    await db.set_state("last_reaction_user_id", str(user_id))
    await db.increment_state(day_key("reaction_count", now, tz))
    await db.insert_filter_log(
        trigger_tg_message_id=tg_message_id,
        candidate_text=emoji,
        verdict="pass",
        stage="react",
        reason="react:sent",
        shadow=False,
        created_at=now,
    )
    logger.info("reaction %s on %s", emoji, tg_message_id)
    return True
