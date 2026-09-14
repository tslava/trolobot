"""Сборка GateState из БД (state + messages) для одного решения гейта."""

from __future__ import annotations

import logging

from trolobot.config_models import Config
from trolobot.db import Database
from trolobot.gate_types import GateMessage, GateState
from trolobot.timeutil import day_key

logger = logging.getLogger(__name__)


def _parse_int_state(key: str, raw: str | None) -> int | None:
    """int(raw) с дефолтом None; мусор в значении -> WARNING в лог, не падать."""
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("gate_state: garbage value %r for state key %r, using default", raw, key)
        return None


def _parse_int_state_default_zero(key: str, raw: str | None) -> int:
    value = _parse_int_state(key, raw)
    return 0 if value is None else value


async def load_gate_state(db: Database, cfg: Config, msg: GateMessage, now: int) -> GateState:
    tz = cfg.persona.timezone

    panic_raw = await db.get_state("panic")
    panic = panic_raw is not None and panic_raw == "1"

    stop_until_raw = await db.get_state("stop_until")
    stop_until = _parse_int_state("stop_until", stop_until_raw)

    topic_cooldown_until_raw = await db.get_state("topic_cooldown_until")
    topic_cooldown_until = _parse_int_state("topic_cooldown_until", topic_cooldown_until_raw)

    last_mention_reply_at_raw = await db.get_state("last_mention_reply_at")
    last_mention_reply_at = _parse_int_state("last_mention_reply_at", last_mention_reply_at_raw)

    user_key = f"last_mention_reply_at:{msg.user_id}"
    last_mention_reply_at_user_raw = await db.get_state(user_key)
    last_mention_reply_at_user = _parse_int_state(user_key, last_mention_reply_at_user_raw)

    last_ambient_at_raw = await db.get_state("last_ambient_at")
    last_ambient_at = _parse_int_state("last_ambient_at", last_ambient_at_raw)

    mention_count_key = day_key("mention_count", now, tz)
    mention_count_raw = await db.get_state(mention_count_key)
    mention_count_today = _parse_int_state_default_zero(mention_count_key, mention_count_raw)

    ambient_count_key = day_key("ambient_count", now, tz)
    ambient_count_raw = await db.get_state(ambient_count_key)
    ambient_count_today = _parse_int_state_default_zero(ambient_count_key, ambient_count_raw)

    muted_user_ids = await db.muted_user_ids()

    since = now - cfg.behaviour.live_talk.window_min * 60
    recent = await db.recent_activity(msg.chat_id, since)

    hot_until_raw = await db.get_state("hot_until")
    hot_until = _parse_int_state("hot_until", hot_until_raw)

    hot_ambient_count_raw = await db.get_state("hot_ambient_count")
    hot_ambient_count = _parse_int_state_default_zero("hot_ambient_count", hot_ambient_count_raw)

    return GateState(
        panic=panic,
        stop_until=stop_until,
        topic_cooldown_until=topic_cooldown_until,
        muted_user_ids=muted_user_ids,
        mention_count_today=mention_count_today,
        last_mention_reply_at=last_mention_reply_at,
        last_mention_reply_at_user=last_mention_reply_at_user,
        ambient_count_today=ambient_count_today,
        last_ambient_at=last_ambient_at,
        recent=tuple(recent),
        hot_until=hot_until,
        hot_ambient_count=hot_ambient_count,
    )
