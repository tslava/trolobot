from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from trolobot.db import Database
from trolobot.retention import retention_loop, run_retention


@dataclass
class _Behaviour:
    message_retention_days: int


@dataclass
class _Persona:
    timezone: str


@dataclass
class _Config:
    behaviour: _Behaviour
    persona: _Persona


async def test_run_retention_returns_stats(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        now = 1_768_003_200  # 2026-01-10 00:00 UTC
        retention_days = 30
        cutoff = now - retention_days * 86400

        await db.insert_message(
            tg_message_id=1,
            chat_id=1,
            user_id=1,
            display_name="A",
            text="old",
            reply_to_tg_message_id=None,
            is_bot=False,
            created_at=cutoff - 100,
        )
        await db.insert_message(
            tg_message_id=2,
            chat_id=1,
            user_id=1,
            display_name="A",
            text="fresh",
            reply_to_tg_message_id=None,
            is_bot=False,
            created_at=cutoff + 100,
        )

        stats = await run_retention(db, retention_days, "UTC", now)

        assert stats.messages == 1
        messages = await db.recent_messages(1, 100)
        assert [m.text for m in messages] == ["fresh"]
    finally:
        await db.close()


async def test_run_retention_no_op_when_nothing_expired(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        now = 1_768_003_200
        stats = await run_retention(db, 30, "UTC", now)
        assert stats.messages == 0
        assert stats.night_queue == 0
        assert stats.pending_replies == 0
        assert stats.filter_log_texts == 0
        assert stats.state_keys == 0
    finally:
        await db.close()


async def test_run_retention_uses_persona_timezone_for_dated_state_keys(tmp_path: Path) -> None:
    """cutoff 23:30 UTC = 01:30 Europe/Warsaw следующего дня (летнее время, UTC+2).

    Ключ, датированный "вчера по Варшаве" (совпадает с "сегодня по UTC"), должен
    удаляться при tz=Europe/Warsaw и оставаться при tz=UTC — решает варшавская дата.
    """
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        now = int(datetime(2026, 7, 15, 23, 30, tzinfo=UTC).timestamp())
        await db.set_state("ambient_count:2026-07-15", "1")

        stats = await run_retention(db, 0, "Europe/Warsaw", now)

        assert stats.state_keys == 1
        assert await db.get_state("ambient_count:2026-07-15") is None
    finally:
        await db.close()


async def test_retention_loop_cancels_cleanly(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        calls = 0

        def cfg_getter() -> _Config:
            nonlocal calls
            calls += 1
            return _Config(
                behaviour=_Behaviour(message_retention_days=30), persona=_Persona(timezone="UTC")
            )

        task = asyncio.create_task(retention_loop(db, cfg_getter, interval_sec=0.01))
        await asyncio.sleep(0.05)
        assert calls >= 2

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert task.cancelled()
    finally:
        await db.close()


async def test_retention_loop_survives_iteration_errors(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        calls = 0

        def failing_cfg_getter() -> _Config:
            nonlocal calls
            calls += 1
            raise RuntimeError("boom")

        task = asyncio.create_task(retention_loop(db, failing_cfg_getter, interval_sec=0.01))
        await asyncio.sleep(0.05)
        assert calls >= 2  # цикл пережил ошибки и продолжил итерации

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await db.close()
