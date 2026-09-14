from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from trolobot.db import Database
from trolobot.retention import retention_loop, run_retention


@dataclass
class _ChatMemory:
    keep_days: int = 365


@dataclass
class _Behaviour:
    message_retention_days: int
    chat_memory: _ChatMemory = field(default_factory=_ChatMemory)


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


async def test_run_retention_purges_chat_memory_by_its_own_keep_days(tmp_path: Path) -> None:
    """Пересказы живут дольше сообщений (CLAUDE.md, "долгая память чата"): свой
    срок keep_days, своё поле в PurgeStats."""
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        now = 1_768_003_200
        keep_days = 365
        cutoff = now - keep_days * 86400

        old_id = await db.insert_chat_memory(
            period_start=cutoff - 8 * 86400,
            period_end=cutoff - 100,
            text="давняя неделя",
            created_at=cutoff - 100,
        )
        fresh_id = await db.insert_chat_memory(
            period_start=cutoff + 100,
            period_end=cutoff + 8 * 86400,
            text="свежая неделя",
            created_at=cutoff + 8 * 86400,
        )

        stats = await run_retention(db, 30, "UTC", now, keep_days)

        assert stats.chat_memory_deleted == 1
        assert await db.chat_memory(old_id) is None
        assert await db.chat_memory(fresh_id) is not None
    finally:
        await db.close()


async def test_run_retention_leaves_chat_memory_alone_without_keep_days(tmp_path: Path) -> None:
    """Сообщения старше message_retention_days уходят, пересказ того же периода — нет."""
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        now = 1_768_003_200
        memory_id = await db.insert_chat_memory(
            period_start=0, period_end=1000, text="очень давняя неделя", created_at=1000
        )

        stats = await run_retention(db, 30, "UTC", now)

        assert stats.chat_memory_deleted == 0
        assert await db.chat_memory(memory_id) is not None
    finally:
        await db.close()


async def test_retention_loop_passes_chat_memory_keep_days(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        memory_id = await db.insert_chat_memory(
            period_start=0, period_end=1000, text="очень давняя неделя", created_at=1000
        )

        def cfg_getter() -> _Config:
            return _Config(
                behaviour=_Behaviour(
                    message_retention_days=30, chat_memory=_ChatMemory(keep_days=7)
                ),
                persona=_Persona(timezone="UTC"),
            )

        task = asyncio.create_task(retention_loop(db, cfg_getter, interval_sec=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await db.chat_memory(memory_id) is None
    finally:
        await db.close()
