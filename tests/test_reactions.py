"""Тесты для trolobot.reactions: pick_reaction (чистая функция), load_reaction_state
(настоящая Database) и react() (фейковый бот, без aiogram)."""

from __future__ import annotations

import random
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SendMessage
from aiogram.types import ReactionTypeEmoji

from trolobot.config_models import Config, ReactionsConfig
from trolobot.db import Database
from trolobot.reactions import ReactionState, load_reaction_state, pick_reaction, react
from trolobot.timeutil import day_key

WARSAW = ZoneInfo("Europe/Warsaw")
TZ = "Europe/Warsaw"
CHAT_ID = -100123456
USER_A = 1001
USER_B = 1002
NOW = 1_768_003_200  # произвольный момент, конкретная дата не важна для этих тестов


def _cfg(**overrides: object) -> ReactionsConfig:
    return ReactionsConfig(**overrides)  # type: ignore[arg-type]


def _state(
    *,
    last_reaction_at: int | None = None,
    last_reaction_user_id: int | None = None,
    count_today: int = 0,
) -> ReactionState:
    return ReactionState(
        last_reaction_at=last_reaction_at,
        last_reaction_user_id=last_reaction_user_id,
        count_today=count_today,
    )


# --- pick_reaction: чистая функция ---


def test_pick_reaction_disabled_returns_none() -> None:
    cfg = _cfg(enabled=False)
    rng = random.Random(1)
    result = pick_reaction(
        drop_reason="gate:dice", user_id=USER_A, state=_state(), cfg=cfg, rng=rng, now=NOW
    )
    assert result is None


def test_pick_reaction_wrong_reason_returns_none() -> None:
    cfg = _cfg(probability=1.0)
    rng = random.Random(1)
    result = pick_reaction(
        drop_reason="gate:not_live", user_id=USER_A, state=_state(), cfg=cfg, rng=rng, now=NOW
    )
    assert result is None


def test_pick_reaction_daily_cap_returns_none() -> None:
    cfg = _cfg(probability=1.0, daily_cap=3)
    rng = random.Random(1)
    result = pick_reaction(
        drop_reason="gate:dice",
        user_id=USER_A,
        state=_state(count_today=3),
        cfg=cfg,
        rng=rng,
        now=NOW,
    )
    assert result is None


def test_pick_reaction_cooldown_returns_none() -> None:
    cfg = _cfg(probability=1.0, cooldown_min=60)
    rng = random.Random(1)
    result = pick_reaction(
        drop_reason="gate:dice",
        user_id=USER_A,
        state=_state(last_reaction_at=NOW - 30 * 60),  # 30 минут назад < 60-минутного кулдауна
        cfg=cfg,
        rng=rng,
        now=NOW,
    )
    assert result is None


def test_pick_reaction_cooldown_elapsed_passes_to_dice() -> None:
    cfg = _cfg(probability=1.0, cooldown_min=60)
    rng = random.Random(1)
    result = pick_reaction(
        drop_reason="gate:dice",
        user_id=USER_A,
        state=_state(last_reaction_at=NOW - 61 * 60),  # 61 минута назад > кулдауна
        cfg=cfg,
        rng=rng,
        now=NOW,
    )
    assert result in cfg.emoji


def test_pick_reaction_same_user_returns_none() -> None:
    cfg = _cfg(probability=1.0)
    rng = random.Random(1)
    result = pick_reaction(
        drop_reason="gate:ambient_cooldown",
        user_id=USER_A,
        state=_state(last_reaction_user_id=USER_A),
        cfg=cfg,
        rng=rng,
        now=NOW,
    )
    assert result is None


def test_pick_reaction_different_user_passes() -> None:
    cfg = _cfg(probability=1.0)
    rng = random.Random(1)
    result = pick_reaction(
        drop_reason="gate:ambient_cooldown",
        user_id=USER_A,
        state=_state(last_reaction_user_id=USER_B),
        cfg=cfg,
        rng=rng,
        now=NOW,
    )
    assert result in cfg.emoji


def test_pick_reaction_dice_fails_returns_none() -> None:
    cfg = _cfg(probability=0.0)
    rng = random.Random(1)
    result = pick_reaction(
        drop_reason="gate:dice", user_id=USER_A, state=_state(), cfg=cfg, rng=rng, now=NOW
    )
    assert result is None


def test_pick_reaction_dice_passes_returns_emoji_from_list() -> None:
    cfg = _cfg(probability=1.0, emoji=["👍", "💩"])
    rng = random.Random(1)
    result = pick_reaction(
        drop_reason="gate:dice", user_id=USER_A, state=_state(), cfg=cfg, rng=rng, now=NOW
    )
    assert result in {"👍", "💩"}


def test_pick_reaction_is_deterministic_with_seed() -> None:
    """Кубик — последняя проверка: rng тратится одинаково при одинаковом seed,
    независимо от того, сколько других (неслучайных) проверок было до него."""
    cfg = _cfg(probability=1.0, emoji=["👍", "💩"])

    result_a = pick_reaction(
        drop_reason="gate:dice",
        user_id=USER_A,
        state=_state(),
        cfg=cfg,
        rng=random.Random(42),
        now=NOW,
    )
    result_b = pick_reaction(
        drop_reason="gate:dice",
        user_id=USER_A,
        state=_state(),
        cfg=cfg,
        rng=random.Random(42),
        now=NOW,
    )
    assert result_a == result_b


# --- load_reaction_state: настоящая Database ---


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "bot.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


async def test_load_reaction_state_empty_db_gives_defaults(db: Database) -> None:
    state = await load_reaction_state(db, TZ, NOW)
    assert state == ReactionState(last_reaction_at=None, last_reaction_user_id=None, count_today=0)


async def test_load_reaction_state_reads_written_state(db: Database) -> None:
    await db.set_state("last_reaction_at", str(NOW - 100))
    await db.set_state("last_reaction_user_id", str(USER_B))
    await db.increment_state(day_key("reaction_count", NOW, TZ), by=2)

    state = await load_reaction_state(db, TZ, NOW)
    assert state.last_reaction_at == NOW - 100
    assert state.last_reaction_user_id == USER_B
    assert state.count_today == 2


async def test_load_reaction_state_garbage_value_falls_back_to_default(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    await db.set_state("last_reaction_at", "не число")
    with caplog.at_level("WARNING", logger="trolobot.reactions"):
        state = await load_reaction_state(db, TZ, NOW)
    assert state.last_reaction_at is None
    assert any("garbage value" in r.message for r in caplog.records)


# --- react(): фейковый бот ---


class FakeBot:
    """Подделка ReactionBotLike: пишет вызовы, при желании кидает ошибку Telegram."""

    def __init__(self, *, raise_error: Exception | None = None) -> None:
        self.calls: list[tuple[int, int, list[ReactionTypeEmoji] | None]] = []
        self._raise_error = raise_error

    async def set_message_reaction(
        self,
        chat_id: int,
        message_id: int,
        reaction: list[ReactionTypeEmoji] | None = None,
    ) -> bool:
        self.calls.append((chat_id, message_id, reaction))
        if self._raise_error is not None:
            raise self._raise_error
        return True


async def test_react_success_updates_state_and_filter_log(db: Database) -> None:
    bot = FakeBot()
    ok = await react(
        bot,
        db,
        chat_id=CHAT_ID,
        tg_message_id=42,
        user_id=USER_A,
        emoji="👍",
        tz=TZ,
        now=NOW,
    )
    assert ok is True
    assert len(bot.calls) == 1
    chat_id, message_id, reaction = bot.calls[0]
    assert chat_id == CHAT_ID
    assert message_id == 42
    assert reaction is not None
    assert reaction[0].emoji == "👍"

    assert await db.get_state("last_reaction_at") == str(NOW)
    assert await db.get_state("last_reaction_user_id") == str(USER_A)
    assert await db.get_state(day_key("reaction_count", NOW, TZ)) == "1"

    summary = dict(await db.filter_log_summary(0))
    assert summary.get("react:sent") == 1


async def test_react_telegram_bad_request_leaves_state_untouched(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    error = TelegramBadRequest(
        method=SendMessage(chat_id=CHAT_ID, text="x"), message="REACTION_INVALID"
    )
    bot = FakeBot(raise_error=error)

    with caplog.at_level("WARNING", logger="trolobot.reactions"):
        ok = await react(
            bot,
            db,
            chat_id=CHAT_ID,
            tg_message_id=42,
            user_id=USER_A,
            emoji="👍",
            tz=TZ,
            now=NOW,
        )

    assert ok is False
    assert await db.get_state("last_reaction_at") is None
    assert await db.get_state("last_reaction_user_id") is None
    assert await db.get_state(day_key("reaction_count", NOW, TZ)) is None

    summary = dict(await db.filter_log_summary(0))
    assert summary.get("react:error") == 1
    assert any("reaction failed" in r.message for r in caplog.records)


async def test_react_telegram_forbidden_is_also_handled(db: Database) -> None:
    error = TelegramForbiddenError(
        method=SendMessage(chat_id=CHAT_ID, text="x"), message="Forbidden"
    )
    bot = FakeBot(raise_error=error)

    ok = await react(
        bot,
        db,
        chat_id=CHAT_ID,
        tg_message_id=42,
        user_id=USER_A,
        emoji="👍",
        tz=TZ,
        now=NOW,
    )
    assert ok is False
    summary = dict(await db.filter_log_summary(0))
    assert summary.get("react:error") == 1


def test_reactions_config_rejects_emoji_outside_allowed() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError, проверен message ниже
        Config(behaviour={"reactions": {"emoji": ["🐸"]}})  # type: ignore[arg-type]
