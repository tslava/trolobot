from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from trolobot.config_models import Config
from trolobot.db import Database
from trolobot.gate_state import load_gate_state
from trolobot.gate_types import GateMessage

CONFIG = Config()


def _msg(
    *,
    chat_id: int = 1,
    tg_message_id: int = 1,
    user_id: int = 10,
    is_bot: bool = False,
    text: str = "привет",
    reply_to_bot: bool = False,
    created_at: int = 1000,
) -> GateMessage:
    return GateMessage(
        chat_id=chat_id,
        tg_message_id=tg_message_id,
        user_id=user_id,
        is_bot=is_bot,
        text=text,
        reply_to_bot=reply_to_bot,
        created_at=created_at,
    )


async def test_load_gate_state_empty_db_gives_defaults(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        state = await load_gate_state(db, CONFIG, _msg(), now=1_768_003_200)

        assert state.panic is False
        assert state.stop_until is None
        assert state.topic_cooldown_until is None
        assert state.muted_user_ids == frozenset()
        assert state.mention_count_today == 0
        assert state.last_mention_reply_at is None
        assert state.last_mention_reply_at_user is None
        assert state.ambient_count_today == 0
        assert state.last_ambient_at is None
        assert state.recent == ()
        assert state.hot_until is None
        assert state.hot_ambient_count == 0
    finally:
        await db.close()


async def test_load_gate_state_reads_filled_keys(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        now = 1_768_003_200  # 2026-01-10 00:00 UTC
        msg = _msg(user_id=42, created_at=now)

        from trolobot.timeutil import day_key

        await db.set_state("panic", "1")
        await db.set_state("stop_until", "12345")
        await db.set_state("topic_cooldown_until", "6789")
        await db.set_state("last_mention_reply_at", "111")
        await db.set_state("last_mention_reply_at:42", "222")
        await db.set_state("last_ambient_at", "333")
        await db.set_state(day_key("mention_count", now, CONFIG.persona.timezone), "5")
        await db.set_state(day_key("ambient_count", now, CONFIG.persona.timezone), "7")
        await db.set_state("hot_until", str(now + 1800))
        await db.set_state("hot_ambient_count", "2")

        conn = db._conn
        assert conn is not None
        await conn.execute(
            "INSERT INTO muted_users (user_id, display_name, muted_by, created_at) "
            "VALUES (?, ?, ?, ?)",
            (99, "Muted", 1, now),
        )
        await conn.commit()

        state = await load_gate_state(db, CONFIG, msg, now)

        assert state.panic is True
        assert state.stop_until == 12345
        assert state.topic_cooldown_until == 6789
        assert state.muted_user_ids == frozenset({99})
        assert state.mention_count_today == 5
        assert state.last_mention_reply_at == 111
        assert state.last_mention_reply_at_user == 222
        assert state.ambient_count_today == 7
        assert state.last_ambient_at == 333
        assert state.hot_until == now + 1800
        assert state.hot_ambient_count == 2
    finally:
        await db.close()


async def test_load_gate_state_panic_requires_exact_value_one(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await db.set_state("panic", "0")
        state = await load_gate_state(db, CONFIG, _msg(), now=1000)
        assert state.panic is False
    finally:
        await db.close()


async def test_load_gate_state_garbage_numeric_value_falls_back_to_default(
    tmp_path: Path, caplog: object
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await db.set_state("stop_until", "not-a-number")

        import logging as _logging

        with caplog.at_level(_logging.WARNING, logger="trolobot.gate_state"):  # type: ignore[attr-defined]
            state = await load_gate_state(db, CONFIG, _msg(), now=1000)

        assert state.stop_until is None
        assert "stop_until" in caplog.text  # type: ignore[attr-defined]
        assert "WARNING" in caplog.text  # type: ignore[attr-defined]
    finally:
        await db.close()


async def test_load_gate_state_garbage_count_falls_back_to_zero(
    tmp_path: Path, caplog: object
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        now = 1_768_003_200
        from trolobot.timeutil import day_key

        key = day_key("mention_count", now, CONFIG.persona.timezone)
        await db.set_state(key, "garbage")

        with caplog.at_level(logging.WARNING, logger="trolobot.gate_state"):  # type: ignore[attr-defined]
            state = await load_gate_state(db, CONFIG, _msg(), now)

        assert state.mention_count_today == 0
        assert "WARNING" in caplog.text  # type: ignore[attr-defined]
    finally:
        await db.close()


async def test_load_gate_state_recent_activity_within_window(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        now = 100_000
        window_sec = CONFIG.behaviour.live_talk.window_min * 60

        await db.insert_message(
            tg_message_id=1,
            chat_id=1,
            user_id=10,
            display_name="A",
            text="hi",
            reply_to_tg_message_id=None,
            is_bot=False,
            created_at=now - window_sec + 1,  # внутри окна
        )
        await db.insert_message(
            tg_message_id=2,
            chat_id=1,
            user_id=11,
            display_name="B",
            text="old",
            reply_to_tg_message_id=None,
            is_bot=False,
            created_at=now - window_sec - 100,  # вне окна
        )

        state = await load_gate_state(db, CONFIG, _msg(chat_id=1, created_at=now), now)

        assert [a.user_id for a in state.recent] == [10]
    finally:
        await db.close()


async def test_load_gate_state_mention_count_key_uses_warsaw_local_day(tmp_path: Path) -> None:
    """now = 00:30 Варшавы (лето, UTC+2) = 22:30 UTC предыдущего дня.

    Ключ mention_count должен браться за варшавскую дату (следующий UTC-день),
    а не за UTC-дату.
    """
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        # 2026-07-15 22:30 UTC == 2026-07-16 00:30 Europe/Warsaw (лето, UTC+2)
        now = int(datetime(2026, 7, 15, 22, 30, tzinfo=UTC).timestamp())

        await db.set_state("mention_count:2026-07-16", "9")  # варшавская дата
        await db.set_state("mention_count:2026-07-15", "1")  # UTC-дата, не должна использоваться

        state = await load_gate_state(db, CONFIG, _msg(created_at=now), now)

        assert state.mention_count_today == 9
    finally:
        await db.close()
