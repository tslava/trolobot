from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from trolobot.db import Database
from trolobot.gate_types import StateChange

EXPECTED_TABLES = {
    "messages",
    "bot_replies",
    "state",
    "night_queue",
    "pending_replies",
    "muted_users",
    "config_overrides",
    "places",
    "filter_log",
    "config_audit",
    "prompt_versions",
    "few_shot_versions",
}


async def test_connect_creates_all_tables_and_bumps_user_version(tmp_path: Path) -> None:
    db = Database(tmp_path / "nested" / "bot.db")
    await db.connect()
    try:
        conn = db._conn
        assert conn is not None
        cursor = await conn.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 1

        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        table_rows = await cursor.fetchall()
        tables = {r["name"] for r in table_rows}
        assert tables >= EXPECTED_TABLES
    finally:
        await db.close()


async def test_reconnect_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"

    db1 = Database(path)
    await db1.connect()
    await db1.insert_message(
        tg_message_id=1,
        chat_id=42,
        user_id=1,
        display_name="A",
        text="привет",
        reply_to_tg_message_id=None,
        is_bot=False,
        created_at=1000,
    )
    await db1.close()

    db2 = Database(path)
    await db2.connect()
    try:
        conn = db2._conn
        assert conn is not None
        cursor = await conn.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 1
        messages = await db2.recent_messages(42, 10)
        assert len(messages) == 1
        assert messages[0].text == "привет"
    finally:
        await db2.close()


async def test_insert_and_recent_messages_order_and_limit(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        for i in range(5):
            await db.insert_message(
                tg_message_id=i,
                chat_id=1,
                user_id=1,
                display_name="A",
                text=f"msg-{i}",
                reply_to_tg_message_id=None,
                is_bot=False,
                created_at=1000 + i,
            )
        # другой чат не должен попадать в выборку
        await db.insert_message(
            tg_message_id=99,
            chat_id=2,
            user_id=1,
            display_name="A",
            text="other chat",
            reply_to_tg_message_id=None,
            is_bot=False,
            created_at=2000,
        )

        messages = await db.recent_messages(1, 3)
        assert [m.text for m in messages] == ["msg-2", "msg-3", "msg-4"]
        assert all(m.chat_id == 1 for m in messages)

        all_messages = await db.recent_messages(1, 100)
        assert [m.text for m in all_messages] == [f"msg-{i}" for i in range(5)]
    finally:
        await db.close()


async def test_state_upsert_and_delete(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        assert await db.get_state("panic") is None

        await db.set_state("panic", "1")
        assert await db.get_state("panic") == "1"

        await db.set_state("panic", "0")
        assert await db.get_state("panic") == "0"

        await db.delete_state("panic")
        assert await db.get_state("panic") is None
    finally:
        await db.close()


async def test_get_overrides(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        assert await db.get_overrides() == {}
        conn = db._conn
        assert conn is not None
        await conn.execute(
            "INSERT INTO config_overrides (key, value, updated_at) VALUES (?, ?, ?)",
            ("behaviour.daily_cap", "3", 1000),
        )
        await conn.execute(
            "INSERT INTO config_overrides (key, value, updated_at) VALUES (?, ?, ?)",
            ("filters.shadow", "false", 1000),
        )
        await conn.commit()

        overrides = await db.get_overrides()
        assert overrides == {"behaviour.daily_cap": "3", "filters.shadow": "false"}
    finally:
        await db.close()


async def test_purge_older_than(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        conn = db._conn
        assert conn is not None

        cutoff = 1_768_003_200  # 2026-01-10 00:00 UTC

        # messages: старое удаляется, свежее остаётся
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

        # night_queue: отвеченное давно удаляется, неотвеченное и свежее остаются
        await conn.execute(
            "INSERT INTO night_queue "
            "(tg_message_id, user_id, display_name, text, created_at, answered_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (10, 1, "A", "old answered", cutoff - 1000, cutoff - 100),
        )
        await conn.execute(
            "INSERT INTO night_queue "
            "(tg_message_id, user_id, display_name, text, created_at, answered_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (11, 1, "A", "not answered", cutoff - 1000, None),
        )
        await conn.execute(
            "INSERT INTO night_queue "
            "(tg_message_id, user_id, display_name, text, created_at, answered_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (12, 1, "A", "answered fresh", cutoff - 1000, cutoff + 100),
        )

        # pending_replies: сделанное давно удаляется, неотправленное и свежее остаются
        await conn.execute(
            "INSERT INTO pending_replies "
            "(trigger_tg_message_id, user_id, trigger, due_at, created_at, done_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (20, 1, "mention", cutoff - 900, cutoff - 1000, cutoff - 100),
        )
        await conn.execute(
            "INSERT INTO pending_replies "
            "(trigger_tg_message_id, user_id, trigger, due_at, created_at, done_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (21, 1, "mention", cutoff - 900, cutoff - 1000, None),
        )
        await conn.execute(
            "INSERT INTO pending_replies "
            "(trigger_tg_message_id, user_id, trigger, due_at, created_at, done_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (22, 1, "mention", cutoff + 900, cutoff + 800, cutoff + 900),
        )

        # filter_log: старый candidate_text обнуляется, свежий остаётся
        await conn.execute(
            "INSERT INTO filter_log "
            "(trigger_tg_message_id, candidate_text, verdict, stage, reason, shadow, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (30, "old candidate", "cut", "regex", "regex:length", 0, cutoff - 100),
        )
        await conn.execute(
            "INSERT INTO filter_log "
            "(trigger_tg_message_id, candidate_text, verdict, stage, reason, shadow, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (31, "fresh candidate", "cut", "regex", "regex:length", 0, cutoff + 100),
        )

        # state: датированные ключи старше cutoff удаляются, остальные остаются
        await db.set_state("ambient_count:2026-01-05", "3")  # YYYY-MM-DD, старый
        await db.set_state("spontaneous_count:2026-W02", "1")  # ISO-неделя, старая (пн 2026-01-05)
        await db.set_state("ambient_count:2026-01-15", "1")  # YYYY-MM-DD, свежий
        await db.set_state("last_mention_reply_at:12345", "999")  # суффикс не дата
        await db.set_state("panic", "0")  # без даты вовсе

        await conn.commit()

        stats = await db.purge_older_than(cutoff)

        assert stats.messages == 1
        assert stats.night_queue == 1
        assert stats.pending_replies == 1
        assert stats.filter_log_texts == 1
        assert stats.state_keys == 2

        messages = await db.recent_messages(1, 100)
        assert [m.text for m in messages] == ["fresh"]

        cursor = await conn.execute("SELECT tg_message_id FROM night_queue ORDER BY tg_message_id")
        remaining_nq = [r["tg_message_id"] for r in await cursor.fetchall()]
        assert remaining_nq == [11, 12]

        cursor = await conn.execute(
            "SELECT trigger_tg_message_id FROM pending_replies ORDER BY trigger_tg_message_id"
        )
        remaining_pr = [r["trigger_tg_message_id"] for r in await cursor.fetchall()]
        assert remaining_pr == [21, 22]

        cursor = await conn.execute(
            "SELECT trigger_tg_message_id, candidate_text FROM filter_log "
            "ORDER BY trigger_tg_message_id"
        )
        filter_rows = await cursor.fetchall()
        assert [dict(r) for r in filter_rows] == [
            {"trigger_tg_message_id": 30, "candidate_text": None},
            {"trigger_tg_message_id": 31, "candidate_text": "fresh candidate"},
        ]

        assert await db.get_state("ambient_count:2026-01-05") is None
        assert await db.get_state("spontaneous_count:2026-W02") is None
        assert await db.get_state("ambient_count:2026-01-15") == "1"
        assert await db.get_state("last_mention_reply_at:12345") == "999"
        assert await db.get_state("panic") == "0"
    finally:
        await db.close()


async def test_purge_older_than_uses_tz_for_dated_state_keys(tmp_path: Path) -> None:
    """cutoff 23:30 UTC = 01:30 Europe/Warsaw следующего дня (летнее время, UTC+2).

    Ключ, датированный "вчера по Варшаве" (= "сегодня по UTC"), удаляется, если cutoff
    считать в варшавской таймзоне, и остаётся при дефолтном tz=UTC — решает не UTC-дата.
    """
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        cutoff = int(datetime(2026, 7, 15, 23, 30, tzinfo=UTC).timestamp())

        await db.set_state("ambient_count:2026-07-15", "1")  # вчера по Варшаве, сегодня по UTC
        await db.set_state("ambient_count:2026-07-16", "1")  # сегодня по Варшаве

        stats = await db.purge_older_than(cutoff, tz="Europe/Warsaw")

        assert stats.state_keys == 1
        assert await db.get_state("ambient_count:2026-07-15") is None
        assert await db.get_state("ambient_count:2026-07-16") == "1"
    finally:
        await db.close()


async def test_purge_older_than_default_tz_is_utc(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        cutoff = int(datetime(2026, 7, 15, 23, 30, tzinfo=UTC).timestamp())

        await db.set_state("ambient_count:2026-07-15", "1")

        stats = await db.purge_older_than(cutoff)  # tz по умолчанию UTC

        # По UTC cutoff_date = 2026-07-15, ключ той же даты не строго старше -> не удаляется.
        assert stats.state_keys == 0
        assert await db.get_state("ambient_count:2026-07-15") == "1"
    finally:
        await db.close()


async def test_concurrent_inserts_and_purge_are_serialized(tmp_path: Path) -> None:
    """50 конкурентных insert_message + purge_older_than: без исключений, ожидаемое число строк.

    Заранее вставленные "старые" сообщения (created_at < cutoff) существуют до старта
    gather и не меняются им — purge, в какой бы момент он ни выполнился среди 50
    конкурентных вставок, обязан вычистить их все. Сами 50 вставляемых конкурентно
    сообщений — свежие (created_at > cutoff), purge их не касается. Поэтому итоговое
    число строк детерминировано (= 50) независимо от порядка чередования корутин —
    единственное, что тут проверяется благодаря write-lock, это отсутствие гонок/исключений
    при многостейтментном purge, идущем параллельно вставкам.
    """
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        now = 1_768_003_200  # 2026-01-10 00:00 UTC
        cutoff = now - 30 * 86400

        for i in range(3):
            await db.insert_message(
                tg_message_id=-(i + 1),
                chat_id=1,
                user_id=1,
                display_name="A",
                text=f"old-{i}",
                reply_to_tg_message_id=None,
                is_bot=False,
                created_at=cutoff - 100,
            )

        async def insert(i: int) -> int:
            return await db.insert_message(
                tg_message_id=i,
                chat_id=1,
                user_id=1,
                display_name="A",
                text=f"msg-{i}",
                reply_to_tg_message_id=None,
                is_bot=False,
                created_at=cutoff + 100,
            )

        results = await asyncio.gather(
            *(insert(i) for i in range(50)),
            db.purge_older_than(cutoff),
            return_exceptions=True,
        )

        exceptions = [r for r in results if isinstance(r, BaseException)]
        assert exceptions == []

        messages = await db.recent_messages(1, 1000)
        assert len(messages) == 50
        assert all(m.text.startswith("msg-") for m in messages)
    finally:
        await db.close()


async def test_methods_before_connect_raise_runtime_error(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    with pytest.raises(RuntimeError):
        await db.get_state("panic")


async def test_insert_filter_log_returns_rowid(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        row_id = await db.insert_filter_log(
            trigger_tg_message_id=1,
            candidate_text=None,
            verdict="drop",
            stage="gate",
            reason="gate:night",
            shadow=False,
            created_at=1000,
        )
        assert row_id > 0

        conn = db._conn
        assert conn is not None
        cursor = await conn.execute(
            "SELECT trigger_tg_message_id, candidate_text, verdict, stage, reason, shadow, "
            "created_at FROM filter_log WHERE id = ?",
            (row_id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert dict(row) == {
            "trigger_tg_message_id": 1,
            "candidate_text": None,
            "verdict": "drop",
            "stage": "gate",
            "reason": "gate:night",
            "shadow": 0,
            "created_at": 1000,
        }
    finally:
        await db.close()


async def test_muted_user_ids(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        assert await db.muted_user_ids() == frozenset()

        conn = db._conn
        assert conn is not None
        await conn.execute(
            "INSERT INTO muted_users (user_id, display_name, muted_by, created_at) "
            "VALUES (?, ?, ?, ?)",
            (1, "A", 99, 1000),
        )
        await conn.execute(
            "INSERT INTO muted_users (user_id, display_name, muted_by, created_at) "
            "VALUES (?, ?, ?, ?)",
            (2, "B", 99, 1000),
        )
        await conn.commit()

        assert await db.muted_user_ids() == frozenset({1, 2})
    finally:
        await db.close()


async def test_recent_activity_filters_bot_since_and_chat(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        since = 1000

        # не-бот в окне -> попадает
        await db.insert_message(
            tg_message_id=1,
            chat_id=1,
            user_id=10,
            display_name="A",
            text="hi",
            reply_to_tg_message_id=None,
            is_bot=False,
            created_at=1000,
        )
        # бот -> исключается
        await db.insert_message(
            tg_message_id=2,
            chat_id=1,
            user_id=999,
            display_name="Bot",
            text="hi",
            reply_to_tg_message_id=None,
            is_bot=True,
            created_at=1001,
        )
        # старше since -> исключается
        await db.insert_message(
            tg_message_id=3,
            chat_id=1,
            user_id=11,
            display_name="B",
            text="old",
            reply_to_tg_message_id=None,
            is_bot=False,
            created_at=999,
        )
        # другой чат -> исключается
        await db.insert_message(
            tg_message_id=4,
            chat_id=2,
            user_id=12,
            display_name="C",
            text="other chat",
            reply_to_tg_message_id=None,
            is_bot=False,
            created_at=1002,
        )
        # второй в окне, позже -> попадает, порядок по created_at
        await db.insert_message(
            tg_message_id=5,
            chat_id=1,
            user_id=13,
            display_name="D",
            text="hi2",
            reply_to_tg_message_id=None,
            is_bot=False,
            created_at=1003,
        )

        activity = await db.recent_activity(1, since)
        assert [(a.user_id, a.created_at) for a in activity] == [(10, 1000), (13, 1003)]
    finally:
        await db.close()


async def test_enqueue_night_returns_rowid(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        row_id = await db.enqueue_night(
            tg_message_id=5,
            user_id=1,
            display_name="A",
            text="привет ночью",
            created_at=1000,
        )
        assert row_id > 0

        conn = db._conn
        assert conn is not None
        cursor = await conn.execute(
            "SELECT tg_message_id, user_id, display_name, text, created_at, answered_at "
            "FROM night_queue WHERE id = ?",
            (row_id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert dict(row) == {
            "tg_message_id": 5,
            "user_id": 1,
            "display_name": "A",
            "text": "привет ночью",
            "created_at": 1000,
            "answered_at": None,
        }
    finally:
        await db.close()


async def test_apply_state_changes_upsert_and_delete_in_one_batch(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await db.set_state("to_delete", "1")
        await db.set_state("to_update", "old")

        await db.apply_state_changes(
            [
                StateChange(key="to_delete", value=None),
                StateChange(key="to_update", value="new"),
                StateChange(key="brand_new", value="v"),
            ]
        )

        assert await db.get_state("to_delete") is None
        assert await db.get_state("to_update") == "new"
        assert await db.get_state("brand_new") == "v"
    finally:
        await db.close()


async def test_apply_state_changes_empty_iterable_is_noop(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await db.set_state("untouched", "1")

        await db.apply_state_changes([])

        assert await db.get_state("untouched") == "1"
    finally:
        await db.close()


async def test_filter_log_summary_sorted_by_count_desc_then_reason(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        since = 1000
        entries = [
            ("gate:night", 3, since + 1),
            ("gate:night", 3, since + 2),
            ("gate:night", 3, since + 3),
            ("gate:topic", 2, since + 1),
            ("gate:topic", 2, since + 2),
            ("gate:ambient_cap", 2, since + 1),
            ("gate:ambient_cap", 2, since + 2),
            ("gate:is_bot", 1, since - 1),  # раньше since -> не считается
        ]
        for reason, _count, created_at in entries:
            await db.insert_filter_log(
                trigger_tg_message_id=None,
                candidate_text=None,
                verdict="drop",
                stage="gate",
                reason=reason,
                shadow=False,
                created_at=created_at,
            )

        summary = await db.filter_log_summary(since)
        assert summary == [
            ("gate:night", 3),
            ("gate:ambient_cap", 2),
            ("gate:topic", 2),
        ]
    finally:
        await db.close()
