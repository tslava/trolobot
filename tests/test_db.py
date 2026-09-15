from __future__ import annotations

import asyncio
import importlib.resources
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from trolobot.db import Database, PlaceRow
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
    "life_events",
    "chat_memory",
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
        assert row[0] == 3

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
        assert row[0] == 3
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


async def test_display_names_empty_list_returns_empty_dict(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        assert await db.display_names([]) == {}
    finally:
        await db.close()


async def test_display_names_returns_latest_per_user(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await db.insert_message(
            tg_message_id=1,
            chat_id=1,
            user_id=10,
            display_name="Дима",
            text="привет",
            reply_to_tg_message_id=None,
            is_bot=False,
            created_at=1000,
        )
        # тот же user_id, более новое сообщение -> более новое имя побеждает
        await db.insert_message(
            tg_message_id=2,
            chat_id=1,
            user_id=10,
            display_name="Дмитрий",
            text="переименовался",
            reply_to_tg_message_id=None,
            is_bot=False,
            created_at=2000,
        )
        await db.insert_message(
            tg_message_id=3,
            chat_id=1,
            user_id=20,
            display_name="Оля",
            text="привет",
            reply_to_tg_message_id=None,
            is_bot=False,
            created_at=1500,
        )

        names = await db.display_names([10, 20, 999])

        assert names == {10: "Дмитрий", 20: "Оля"}  # 999 отсутствует в messages -> не в результате
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


async def test_insert_bot_reply_returns_rowid_and_stores_fields(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        row_id = await db.insert_bot_reply(
            tg_message_id=100,
            reply_to_tg_message_id=42,
            trigger="mention",
            trigger_tg_message_id=42,
            text="привет",
            prompt_version=1,
            few_shot_version=1,
            delay_sec=5,
            created_at=1000,
        )
        assert row_id > 0

        conn = db._conn
        assert conn is not None
        cursor = await conn.execute(
            "SELECT tg_message_id, reply_to_tg_message_id, trigger, trigger_tg_message_id, "
            "text, prompt_version, few_shot_version, delay_sec, created_at "
            "FROM bot_replies WHERE id = ?",
            (row_id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert dict(row) == {
            "tg_message_id": 100,
            "reply_to_tg_message_id": 42,
            "trigger": "mention",
            "trigger_tg_message_id": 42,
            "text": "привет",
            "prompt_version": 1,
            "few_shot_version": 1,
            "delay_sec": 5,
            "created_at": 1000,
        }
    finally:
        await db.close()


async def test_recent_bot_replies_chronological_order_and_limit(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        for i in range(5):
            await db.insert_bot_reply(
                tg_message_id=i,
                reply_to_tg_message_id=None,
                trigger="ambient",
                trigger_tg_message_id=None,
                text=f"reply-{i}",
                prompt_version=1,
                few_shot_version=1,
                delay_sec=0,
                created_at=1000 + i,
            )

        replies = await db.recent_bot_replies(3)
        assert replies == ["reply-2", "reply-3", "reply-4"]

        all_replies = await db.recent_bot_replies(100)
        assert all_replies == [f"reply-{i}" for i in range(5)]
    finally:
        await db.close()


async def test_increment_state_default_and_accumulation(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        assert await db.get_state("mention_count:2026-01-01") is None

        first = await db.increment_state("mention_count:2026-01-01")
        assert first == 1
        assert await db.get_state("mention_count:2026-01-01") == "1"

        second = await db.increment_state("mention_count:2026-01-01", by=4)
        assert second == 5
        assert await db.get_state("mention_count:2026-01-01") == "5"
    finally:
        await db.close()


async def test_increment_state_concurrent_50_times_gives_exactly_50(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        results = await asyncio.gather(
            *(db.increment_state("llm_calls:2026-01-01") for _ in range(50))
        )

        assert sorted(results) == list(range(1, 51))
        assert await db.get_state("llm_calls:2026-01-01") == "50"
    finally:
        await db.close()


async def test_add_state_float_accumulates_and_round_trips_parsing(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        assert await db.get_state("llm_spent_usd:2026-01-01") is None

        first = await db.add_state_float("llm_spent_usd:2026-01-01", 0.1)
        assert first == pytest.approx(0.1)

        second = await db.add_state_float("llm_spent_usd:2026-01-01", 0.2)
        assert second == pytest.approx(0.3)

        stored = await db.get_state("llm_spent_usd:2026-01-01")
        assert stored is not None
        # Накопленное значение должно оставаться парсибельным float() без исключений,
        # даже когда сумма даёт длинную дробь (0.1 + 0.2 != 0.3 в двоичной арифметике).
        assert float(stored) == pytest.approx(second)
    finally:
        await db.close()


async def test_insert_pending_update_and_mark_done(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        pending_id = await db.insert_pending(
            trigger_tg_message_id=10,
            user_id=1,
            trigger="mention",
            due_at=2000,
            created_at=1000,
        )
        assert pending_id > 0

        await db.update_pending_due(pending_id, 2500)

        conn = db._conn
        assert conn is not None
        cursor = await conn.execute(
            "SELECT due_at, done_at FROM pending_replies WHERE id = ?", (pending_id,)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["due_at"] == 2500
        assert row["done_at"] is None

        await db.mark_pending_done(pending_id, 3000)
        cursor = await conn.execute(
            "SELECT done_at FROM pending_replies WHERE id = ?", (pending_id,)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["done_at"] == 3000
    finally:
        await db.close()


async def test_load_pending_excludes_done_and_orders_by_due_at(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        done_id = await db.insert_pending(
            trigger_tg_message_id=1, user_id=1, trigger="mention", due_at=1500, created_at=1000
        )
        await db.mark_pending_done(done_id, 1600)

        later_id = await db.insert_pending(
            trigger_tg_message_id=2, user_id=1, trigger="reply", due_at=3000, created_at=1000
        )
        earlier_id = await db.insert_pending(
            trigger_tg_message_id=3, user_id=1, trigger="name", due_at=2000, created_at=1000
        )

        pending = await db.load_pending()
        assert [p.id for p in pending] == [earlier_id, later_id]
        assert all(p.done_at is None for p in pending)
        assert pending[0].trigger == "name"
        assert pending[0].due_at == 2000
    finally:
        await db.close()


async def test_night_unanswered_orders_by_created_at_and_excludes_answered(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        answered_id = await db.enqueue_night(
            tg_message_id=1, user_id=1, display_name="A", text="answered", created_at=900
        )
        await db.mark_night_answered([answered_id], 950)

        later_id = await db.enqueue_night(
            tg_message_id=2, user_id=2, display_name="B", text="later", created_at=2000
        )
        earlier_id = await db.enqueue_night(
            tg_message_id=3, user_id=3, display_name="C", text="earlier", created_at=1000
        )

        unanswered = await db.night_unanswered()
        assert [row.id for row in unanswered] == [earlier_id, later_id]
        assert all(row.answered_at is None for row in unanswered)
        assert unanswered[0].text == "earlier"
    finally:
        await db.close()


async def test_mark_night_answered_batch_update(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        id_a = await db.enqueue_night(
            tg_message_id=1, user_id=1, display_name="A", text="a", created_at=1000
        )
        id_b = await db.enqueue_night(
            tg_message_id=2, user_id=2, display_name="B", text="b", created_at=1001
        )
        id_c = await db.enqueue_night(
            tg_message_id=3, user_id=3, display_name="C", text="c", created_at=1002
        )

        await db.mark_night_answered([id_a, id_b], 2000)

        remaining = await db.night_unanswered()
        assert [row.id for row in remaining] == [id_c]

        conn = db._conn
        assert conn is not None
        cursor = await conn.execute("SELECT id, answered_at FROM night_queue ORDER BY id")
        rows = {row["id"]: row["answered_at"] for row in await cursor.fetchall()}
        assert rows == {id_a: 2000, id_b: 2000, id_c: None}
    finally:
        await db.close()


async def test_mark_night_answered_empty_list_is_noop(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        row_id = await db.enqueue_night(
            tg_message_id=1, user_id=1, display_name="A", text="a", created_at=1000
        )

        await db.mark_night_answered([], 2000)

        unanswered = await db.night_unanswered()
        assert [row.id for row in unanswered] == [row_id]
        assert unanswered[0].answered_at is None
    finally:
        await db.close()


async def test_messages_after_counts_only_after_id_and_excludes_bot(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await db.insert_message(
            tg_message_id=10,
            chat_id=1,
            user_id=1,
            display_name="A",
            text="before",
            reply_to_tg_message_id=None,
            is_bot=False,
            created_at=1000,
        )
        await db.insert_message(
            tg_message_id=20,
            chat_id=1,
            user_id=2,
            display_name="B",
            text="after-human",
            reply_to_tg_message_id=None,
            is_bot=False,
            created_at=1001,
        )
        await db.insert_message(
            tg_message_id=21,
            chat_id=1,
            user_id=99,
            display_name="Bot",
            text="after-bot",
            reply_to_tg_message_id=None,
            is_bot=True,
            created_at=1002,
        )
        # другой чат не должен считаться
        await db.insert_message(
            tg_message_id=30,
            chat_id=2,
            user_id=3,
            display_name="C",
            text="other chat",
            reply_to_tg_message_id=None,
            is_bot=False,
            created_at=1003,
        )

        assert await db.messages_after(1, 10) == 1
        assert await db.messages_after(1, 20) == 0
        assert await db.messages_after(1, 0) == 2
    finally:
        await db.close()


async def test_last_message_at_ignores_bot_and_other_chats(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        assert await db.last_message_at(1) is None

        await db.insert_message(
            tg_message_id=1,
            chat_id=1,
            user_id=1,
            display_name="A",
            text="first",
            reply_to_tg_message_id=None,
            is_bot=False,
            created_at=1000,
        )
        await db.insert_message(
            tg_message_id=2,
            chat_id=1,
            user_id=99,
            display_name="Bot",
            text="later-bot",
            reply_to_tg_message_id=None,
            is_bot=True,
            created_at=5000,
        )
        await db.insert_message(
            tg_message_id=3,
            chat_id=1,
            user_id=2,
            display_name="B",
            text="second",
            reply_to_tg_message_id=None,
            is_bot=False,
            created_at=2000,
        )
        await db.insert_message(
            tg_message_id=4,
            chat_id=2,
            user_id=3,
            display_name="C",
            text="other chat, later",
            reply_to_tg_message_id=None,
            is_bot=False,
            created_at=9000,
        )

        assert await db.last_message_at(1) == 2000
    finally:
        await db.close()


async def test_last_bot_reply_at_none_without_replies(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        assert await db.last_bot_reply_at() is None
    finally:
        await db.close()


async def test_last_bot_reply_at_returns_max_created_at(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await db.insert_bot_reply(
            tg_message_id=1,
            reply_to_tg_message_id=None,
            trigger="ambient",
            trigger_tg_message_id=None,
            text="раньше",
            prompt_version=1,
            few_shot_version=1,
            delay_sec=0,
            created_at=1000,
        )
        await db.insert_bot_reply(
            tg_message_id=2,
            reply_to_tg_message_id=None,
            trigger="mention",
            trigger_tg_message_id=None,
            text="позже",
            prompt_version=1,
            few_shot_version=1,
            delay_sec=0,
            created_at=5000,
        )

        assert await db.last_bot_reply_at() == 5000
    finally:
        await db.close()


async def test_messages_since_strictly_after_excludes_bot_and_other_chats(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await db.insert_message(
            tg_message_id=1,
            chat_id=1,
            user_id=1,
            display_name="A",
            text="at boundary",
            reply_to_tg_message_id=None,
            is_bot=False,
            created_at=1000,
        )
        await db.insert_message(
            tg_message_id=2,
            chat_id=1,
            user_id=2,
            display_name="B",
            text="after",
            reply_to_tg_message_id=None,
            is_bot=False,
            created_at=1001,
        )
        await db.insert_message(
            tg_message_id=3,
            chat_id=1,
            user_id=99,
            display_name="Bot",
            text="bot reply",
            reply_to_tg_message_id=None,
            is_bot=True,
            created_at=1002,
        )
        await db.insert_message(
            tg_message_id=4,
            chat_id=2,
            user_id=3,
            display_name="C",
            text="other chat",
            reply_to_tg_message_id=None,
            is_bot=False,
            created_at=1003,
        )

        rows = await db.messages_since(1, 1000)
        # created_at > since (строго): сообщение ровно на границе не входит.
        assert [row.text for row in rows] == ["after"]
    finally:
        await db.close()


async def test_messages_since_limit_keeps_last_n_chronological(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        for i in range(5):
            await db.insert_message(
                tg_message_id=10 + i,
                chat_id=1,
                user_id=1,
                display_name="A",
                text=f"msg-{i}",
                reply_to_tg_message_id=None,
                is_bot=False,
                created_at=1000 + i,
            )

        rows = await db.messages_since(1, 999, 2)
        assert [row.text for row in rows] == ["msg-3", "msg-4"]

        all_rows = await db.messages_since(1, 999, 100)
        assert [row.text for row in all_rows] == [f"msg-{i}" for i in range(5)]
    finally:
        await db.close()


# -- этап 6: управление из телеграма ------------------------------------------


async def test_set_override_writes_value_and_audit_row_returns_old_none(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        old = await db.set_override("behaviour.daily_cap", "5", changed_by=42, now=1000)
        assert old is None
        assert await db.get_overrides() == {"behaviour.daily_cap": "5"}

        conn = db._conn
        assert conn is not None
        cursor = await conn.execute(
            "SELECT key, old_value, new_value, changed_by, created_at FROM config_audit"
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        assert rows == [
            {
                "key": "behaviour.daily_cap",
                "old_value": None,
                "new_value": "5",
                "changed_by": 42,
                "created_at": 1000,
            }
        ]
    finally:
        await db.close()


async def test_set_override_twice_returns_previous_value_and_audits_both(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await db.set_override("behaviour.daily_cap", "5", changed_by=1, now=1000)
        old = await db.set_override("behaviour.daily_cap", "7", changed_by=1, now=1001)

        assert old == "5"
        assert await db.get_overrides() == {"behaviour.daily_cap": "7"}

        conn = db._conn
        assert conn is not None
        cursor = await conn.execute("SELECT old_value, new_value FROM config_audit ORDER BY id")
        rows = [dict(r) for r in await cursor.fetchall()]
        assert rows == [
            {"old_value": None, "new_value": "5"},
            {"old_value": "5", "new_value": "7"},
        ]
    finally:
        await db.close()


async def test_delete_override_removes_value_and_audits_new_value_null(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await db.set_override("behaviour.daily_cap", "5", changed_by=1, now=1000)

        old = await db.delete_override("behaviour.daily_cap", changed_by=2, now=2000)
        assert old == "5"
        assert await db.get_overrides() == {}

        conn = db._conn
        assert conn is not None
        cursor = await conn.execute(
            "SELECT key, old_value, new_value, changed_by, created_at FROM config_audit ORDER BY id"
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        assert rows[-1] == {
            "key": "behaviour.daily_cap",
            "old_value": "5",
            "new_value": None,
            "changed_by": 2,
            "created_at": 2000,
        }
    finally:
        await db.close()


async def test_delete_override_missing_key_returns_none_but_still_audits(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        old = await db.delete_override("behaviour.daily_cap", changed_by=1, now=1000)
        assert old is None

        conn = db._conn
        assert conn is not None
        cursor = await conn.execute("SELECT COUNT(*) AS cnt FROM config_audit")
        row = await cursor.fetchone()
        assert row is not None
        assert row["cnt"] == 1
    finally:
        await db.close()


async def test_audit_stop_writes_row_with_null_old_and_new(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await db.audit_stop("stop", changed_by=7, now=1234)

        conn = db._conn
        assert conn is not None
        cursor = await conn.execute(
            "SELECT key, old_value, new_value, changed_by, created_at FROM config_audit"
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        assert rows == [
            {
                "key": "stop",
                "old_value": None,
                "new_value": None,
                "changed_by": 7,
                "created_at": 1234,
            }
        ]
        # config_overrides никак не затрагивается
        assert await db.get_overrides() == {}
    finally:
        await db.close()


async def test_add_mute_and_remove_mute(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        assert await db.muted_user_ids() == frozenset()

        await db.add_mute(10, "Дима", muted_by=1, now=1000)
        assert await db.muted_user_ids() == frozenset({10})

        # повторный add_mute того же user_id обновляет строку, а не дублирует
        await db.add_mute(10, "Дмитрий", muted_by=2, now=2000)
        assert await db.muted_user_ids() == frozenset({10})

        removed = await db.remove_mute(10)
        assert removed is True
        assert await db.muted_user_ids() == frozenset()

        removed_again = await db.remove_mute(10)
        assert removed_again is False
    finally:
        await db.close()


async def test_last_bot_replies_order_and_limit(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        for i in range(3):
            await db.insert_bot_reply(
                tg_message_id=i,
                reply_to_tg_message_id=None,
                trigger="ambient",
                trigger_tg_message_id=None,
                text=f"reply-{i}",
                prompt_version=1,
                few_shot_version=1,
                delay_sec=i,
                created_at=1000 + i,
            )

        rows = await db.last_bot_replies(2)
        assert [r.text for r in rows] == ["reply-2", "reply-1"]  # новые первыми
        assert rows[0].trigger == "ambient"
        assert rows[0].tg_message_id == 2
        assert rows[0].trigger_tg_message_id is None
        assert rows[0].prompt_version == 1
        assert rows[0].few_shot_version == 1
        assert rows[0].delay_sec == 2
        assert rows[0].created_at == 1002

        all_rows = await db.last_bot_replies(100)
        assert [r.text for r in all_rows] == ["reply-2", "reply-1", "reply-0"]
    finally:
        await db.close()


async def test_prompt_versions_active_and_seed_from_empty(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        assert await db.active_prompt() is None
        assert await db.prompt_versions() == []

        v1 = await db.add_prompt_version("body v1", "seed from file", 1000)
        assert v1 == 1
        assert await db.active_prompt() == (1, "body v1")

        versions = await db.prompt_versions()
        assert [(v.version, v.active) for v in versions] == [(1, True)]
        assert versions[0].note == "seed from file"
        assert versions[0].created_at == 1000
    finally:
        await db.close()


async def test_add_prompt_version_increments_and_switches_active(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        v1 = await db.add_prompt_version("body v1", "seed", 1000)
        v2 = await db.add_prompt_version("body v2", "edited", 2000)

        assert v1 == 1
        assert v2 == 2
        assert await db.active_prompt() == (2, "body v2")

        versions = await db.prompt_versions()
        assert [(v.version, v.active) for v in versions] == [(1, False), (2, True)]
    finally:
        await db.close()


async def test_activate_prompt_switches_active_and_missing_version_returns_false(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await db.add_prompt_version("body v1", "seed", 1000)
        await db.add_prompt_version("body v2", "edited", 2000)

        assert await db.active_prompt() == (2, "body v2")

        ok = await db.activate_prompt(1)
        assert ok is True
        assert await db.active_prompt() == (1, "body v1")

        versions = await db.prompt_versions()
        assert [(v.version, v.active) for v in versions] == [(1, True), (2, False)]

        missing = await db.activate_prompt(99)
        assert missing is False
        # активная версия не изменилась
        assert await db.active_prompt() == (1, "body v1")
    finally:
        await db.close()


async def test_few_shot_versions_add_and_activate_independent_from_prompt(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        assert await db.active_few_shot() is None

        await db.add_prompt_version("prompt v1", "seed", 1000)
        fv1 = await db.add_few_shot_version("- a\n", "seed", 1000)
        fv2 = await db.add_few_shot_version("- a\n- b\n", "/ex add", 2000)

        assert fv1 == 1
        assert fv2 == 2
        assert await db.active_few_shot() == (2, "- a\n- b\n")
        # версии few_shot и prompt нумеруются независимо
        assert await db.active_prompt() == (1, "prompt v1")

        ok = await db.activate_few_shot(1)
        assert ok is True
        assert await db.active_few_shot() == (1, "- a\n")

        missing = await db.activate_few_shot(42)
        assert missing is False
        assert await db.active_few_shot() == (1, "- a\n")
    finally:
        await db.close()


async def test_prompt_version_bodies_returns_all_bodies(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        assert await db.prompt_version_bodies() == []

        await db.add_prompt_version("body v1", "seed", 1000)
        await db.add_prompt_version("body v2", "edited", 2000)

        bodies = await db.prompt_version_bodies()
        assert set(bodies) == {"body v1", "body v2"}
    finally:
        await db.close()


async def test_few_shot_version_bodies_returns_all_bodies(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        assert await db.few_shot_version_bodies() == []

        await db.add_few_shot_version("- a\n", "seed", 1000)
        await db.add_few_shot_version("- a\n- b\n", "/ex add", 2000)

        bodies = await db.few_shot_version_bodies()
        assert set(bodies) == {"- a\n", "- a\n- b\n"}
    finally:
        await db.close()


async def test_message_by_tg_id_found_and_missing(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        assert await db.message_by_tg_id(1, 100) is None

        await db.insert_message(
            tg_message_id=100,
            chat_id=1,
            user_id=1,
            display_name="A",
            text="привет",
            reply_to_tg_message_id=None,
            is_bot=False,
            created_at=1000,
        )
        # другой чат, тот же tg_message_id -> не находится
        assert await db.message_by_tg_id(2, 100) is None

        row = await db.message_by_tg_id(1, 100)
        assert row is not None
        assert row.text == "привет"
        assert row.chat_id == 1
        assert row.tg_message_id == 100
    finally:
        await db.close()


async def test_bot_reply_by_tg_id_found_and_missing(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        assert await db.bot_reply_by_tg_id(555) is None

        await db.insert_bot_reply(
            tg_message_id=555,
            reply_to_tg_message_id=None,
            trigger="mention",
            trigger_tg_message_id=42,
            text="привет",
            prompt_version=1,
            few_shot_version=1,
            delay_sec=5,
            created_at=1000,
        )

        row = await db.bot_reply_by_tg_id(555)
        assert row is not None
        assert row.text == "привет"
        assert row.trigger == "mention"
        assert row.tg_message_id == 555
        assert row.trigger_tg_message_id == 42
    finally:
        await db.close()


# --------------------------------------------------------------------------- #
# places (этап 5): upsert_place / places_all / places_names
# --------------------------------------------------------------------------- #


def _place_row(
    place_id: str,
    name: str,
    *,
    district: str = "Wilda",
    category: str = "craft",
    rating: float = 4.6,
    reviews: int = 100,
    price_level: int | None = 2,
    quiet: bool = False,
    fact: str = "тихо",
    operational: bool = True,
    refreshed_at: int = 1_700_000_000,
) -> PlaceRow:
    return PlaceRow(
        place_id=place_id,
        name=name,
        district=district,
        category=category,
        rating=rating,
        reviews=reviews,
        price_level=price_level,
        quiet=quiet,
        fact=fact,
        operational=operational,
        refreshed_at=refreshed_at,
    )


async def test_upsert_place_inserts_and_updates_by_place_id(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await db.upsert_place(_place_row("p1", "FARBY", rating=4.8, quiet=True))
        rows = await db.places_all()
        assert len(rows) == 1
        assert rows[0].place_id == "p1"
        assert rows[0].name == "FARBY"
        assert rows[0].rating == 4.8
        assert rows[0].quiet is True
        assert rows[0].operational is True

        # Тот же place_id -> UPDATE, не второй ряд.
        await db.upsert_place(_place_row("p1", "FARBY", rating=4.9, quiet=False, fact="дёшево"))
        rows = await db.places_all()
        assert len(rows) == 1
        assert rows[0].rating == 4.9
        assert rows[0].quiet is False
        assert rows[0].fact == "дёшево"
    finally:
        await db.close()


async def test_upsert_place_price_level_none(tmp_path: Path) -> None:
    """price_level ненадёжен у Google и иногда отсутствует (PLAN.md, этап 5, п.4)."""
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await db.upsert_place(_place_row("p1", "Piwnica", price_level=None))
        rows = await db.places_all()
        assert rows[0].price_level is None
    finally:
        await db.close()


async def test_places_all_operational_only_default_true(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await db.upsert_place(_place_row("p1", "Открыто", operational=True))
        await db.upsert_place(_place_row("p2", "Закрыто", operational=False))

        operational = await db.places_all()
        assert [r.name for r in operational] == ["Открыто"]

        everything = await db.places_all(operational_only=False)
        assert {r.name for r in everything} == {"Открыто", "Закрыто"}
    finally:
        await db.close()


async def test_places_all_ordered_by_name(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await db.upsert_place(_place_row("p1", "Wściekły Chmiel"))
        await db.upsert_place(_place_row("p2", "BRO"))
        await db.upsert_place(_place_row("p3", "Deja Vu"))

        rows = await db.places_all()
        assert [r.name for r in rows] == ["BRO", "Deja Vu", "Wściekły Chmiel"]
    finally:
        await db.close()


async def test_places_names_only_operational(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await db.upsert_place(_place_row("p1", "FARBY", operational=True))
        await db.upsert_place(_place_row("p2", "Закрытый бар", operational=False))

        assert await db.places_names() == ["FARBY"]
    finally:
        await db.close()


# --------------------------------------------------------------------------- #
# mark_places_not_seen (код-ревью, places_fill.py после прогона с Google)
# --------------------------------------------------------------------------- #


async def test_mark_places_not_seen_marks_missing_leaves_seen_and_manual(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await db.upsert_place(_place_row("p1", "Остался"))
        await db.upsert_place(_place_row("p2", "Пропал"))
        await db.upsert_place(_place_row("manual:0:ручное", "Ручное Место"))

        marked = await db.mark_places_not_seen(["p1"], now=2_000_000_000)

        assert marked == 1
        by_id = {row.place_id: row for row in await db.places_all(operational_only=False)}
        assert by_id["p1"].operational is True
        assert by_id["p2"].operational is False
        assert by_id["p2"].refreshed_at == 2_000_000_000
        assert by_id["manual:0:ручное"].operational is True
    finally:
        await db.close()


async def test_mark_places_not_seen_empty_seen_ids_marks_all_non_manual(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await db.upsert_place(_place_row("p1", "Пропал"))
        await db.upsert_place(_place_row("manual:0:ручное", "Ручное Место"))

        marked = await db.mark_places_not_seen([], now=2_000_000_000)

        assert marked == 1
        by_id = {row.place_id: row for row in await db.places_all(operational_only=False)}
        assert by_id["p1"].operational is False
        assert by_id["manual:0:ручное"].operational is True
    finally:
        await db.close()


async def test_mark_places_not_seen_already_not_operational_not_recounted(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await db.upsert_place(_place_row("p1", "Уже закрыто", operational=False))

        marked = await db.mark_places_not_seen([], now=2_000_000_000)

        assert marked == 0
    finally:
        await db.close()


# --- life_events (/life, CLAUDE.md "события жизни") --------------------------


def _schema_sql_v1() -> str:
    """schema.sql, но как будто ещё нет ни life_events, ни chat_memory (реальная
    БД на сервере, user_version == 1) — для теста миграции 1 -> 3."""
    schema_sql = importlib.resources.files("trolobot").joinpath("schema.sql").read_text("utf-8")
    life_events_block = (
        '-- События жизни персонажа (/life, CLAUDE.md "события жизни") — память, не\n'
        "-- переписка: retention.py её не трогает, чистит только /life rm.\n"
        "CREATE TABLE life_events (\n"
        "    id INTEGER PRIMARY KEY,\n"
        "    text TEXT NOT NULL,\n"
        "    created_at INTEGER NOT NULL,\n"
        "    announced_at INTEGER,\n"
        "    announced_tg_message_id INTEGER\n"
        ");\n\n"
    )
    assert life_events_block in schema_sql
    old_schema_sql = schema_sql.replace(life_events_block, "")
    old_schema_sql = _strip_chat_memory(old_schema_sql)
    old_schema_sql = old_schema_sql.replace("PRAGMA user_version = 3;", "PRAGMA user_version = 1;")
    assert "life_events" not in old_schema_sql
    return old_schema_sql


def _schema_sql_v2() -> str:
    """schema.sql, но как будто ещё нет chat_memory (user_version == 2) — для
    теста миграции 2 -> 3 (CLAUDE.md, "долгая память чата")."""
    schema_sql = importlib.resources.files("trolobot").joinpath("schema.sql").read_text("utf-8")
    old_schema_sql = _strip_chat_memory(schema_sql)
    old_schema_sql = old_schema_sql.replace("PRAGMA user_version = 3;", "PRAGMA user_version = 2;")
    assert "chat_memory" not in old_schema_sql
    return old_schema_sql


def _strip_chat_memory(schema_sql: str) -> str:
    chat_memory_block = (
        '-- Долгая память чата (CLAUDE.md, "долгая память чата") — пересказ прошедших\n'
        "-- разговоров по периодам. Живёт дольше самих сообщений: retention.py чистит её\n"
        "-- по своему сроку (behaviour.chat_memory.keep_days), а не по message_retention_days.\n"
        "CREATE TABLE chat_memory (\n"
        "    id INTEGER PRIMARY KEY,\n"
        "    period_start INTEGER NOT NULL,   -- unix, включительно\n"
        "    period_end INTEGER NOT NULL,     -- unix, исключительно\n"
        "    text TEXT NOT NULL,              -- пересказ, несколько строк\n"
        "    created_at INTEGER NOT NULL\n"
        ");\n\n"
    )
    index_line = "CREATE INDEX idx_chat_memory_period_end ON chat_memory (period_end);\n"
    assert chat_memory_block in schema_sql
    assert index_line in schema_sql
    return schema_sql.replace(chat_memory_block, "").replace(index_line, "")


async def test_migrate_v1_to_v3_adds_new_tables_and_keeps_existing_data(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bot.db"
    conn = await aiosqlite.connect(path)
    try:
        await conn.executescript(_schema_sql_v1())
        await conn.execute(
            "INSERT INTO messages (tg_message_id, chat_id, user_id, display_name, text, "
            "reply_to_tg_message_id, is_bot, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (1, 42, 1, "A", "привет со старой схемы", None, 0, 1000),
        )
        await conn.commit()
    finally:
        await conn.close()

    db = Database(path)
    await db.connect()
    try:
        raw_conn = db._conn
        assert raw_conn is not None

        cursor = await raw_conn.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 3

        cursor = await raw_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('life_events', 'chat_memory')"
        )
        assert {r["name"] for r in await cursor.fetchall()} == {"life_events", "chat_memory"}

        messages = await db.recent_messages(42, 10)
        assert len(messages) == 1
        assert messages[0].text == "привет со старой схемы"

        # Свежие таблицы рабочие, не просто существуют.
        event_id = await db.insert_life_event(text="продал Октавию", created_at=2000)
        assert await db.life_event(event_id) is not None
        memory_id = await db.insert_chat_memory(
            period_start=1000, period_end=2000, text="говорили о гараже", created_at=2000
        )
        assert await db.chat_memory(memory_id) is not None
    finally:
        await db.close()


async def test_migrate_v2_to_v3_adds_chat_memory_and_keeps_existing_data(
    tmp_path: Path,
) -> None:
    """Реальная БД на сервере стоит на user_version == 2 (life_events уже есть):
    миграция 3 должна добавить chat_memory и не тронуть данные."""
    path = tmp_path / "bot.db"
    conn = await aiosqlite.connect(path)
    try:
        await conn.executescript(_schema_sql_v2())
        await conn.execute(
            "INSERT INTO messages (tg_message_id, chat_id, user_id, display_name, text, "
            "reply_to_tg_message_id, is_bot, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (1, 42, 1, "A", "привет со схемы v2", None, 0, 1000),
        )
        await conn.commit()
    finally:
        await conn.close()

    db = Database(path)
    await db.connect()
    try:
        raw_conn = db._conn
        assert raw_conn is not None

        cursor = await raw_conn.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 3

        messages = await db.recent_messages(42, 10)
        assert [m.text for m in messages] == ["привет со схемы v2"]

        memory_id = await db.insert_chat_memory(
            period_start=1000, period_end=2000, text="говорили о гараже", created_at=2000
        )
        assert await db.chat_memory(memory_id) is not None
    finally:
        await db.close()


async def test_insert_and_get_life_event(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        event_id = await db.insert_life_event(text="продал Октавию, взял Кию Сид", created_at=1000)

        event = await db.life_event(event_id)
        assert event is not None
        assert event.id == event_id
        assert event.text == "продал Октавию, взял Кию Сид"
        assert event.created_at == 1000
        assert event.announced_at is None
        assert event.announced_tg_message_id is None

        assert await db.life_event(event_id + 1000) is None
    finally:
        await db.close()


async def test_life_events_ordered_by_created_at_then_id(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        id_b = await db.insert_life_event(text="B", created_at=1000)
        id_a = await db.insert_life_event(text="A", created_at=500)
        id_c = await db.insert_life_event(text="C", created_at=1000)

        rows = await db.life_events()

        # created_at asc, id asc: A (500) первой; из двух с created_at=1000 —
        # B раньше C, потому что вставлена раньше (меньший id).
        assert [row.id for row in rows] == [id_a, id_b, id_c]
        assert [row.text for row in rows] == ["A", "B", "C"]
    finally:
        await db.close()


async def test_delete_life_event_removes_row_and_reports_missing(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        event_id = await db.insert_life_event(text="продал Октавию", created_at=1000)

        assert await db.delete_life_event(event_id) is True
        assert await db.life_event(event_id) is None
        assert await db.delete_life_event(event_id) is False
    finally:
        await db.close()


async def test_mark_life_event_announced_sets_fields(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        event_id = await db.insert_life_event(text="продал Октавию", created_at=1000)
        before = await db.life_event(event_id)
        assert before is not None
        assert before.announced_at is None

        await db.mark_life_event_announced(event_id, tg_message_id=555, now=2000)

        event = await db.life_event(event_id)
        assert event is not None
        assert event.announced_at == 2000
        assert event.announced_tg_message_id == 555
    finally:
        await db.close()


# --- долгая память чата (CLAUDE.md, "долгая память чата") --------------------


async def test_chat_memory_crud(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        first = await db.insert_chat_memory(
            period_start=1000, period_end=2000, text="говорили о гараже", created_at=2000
        )
        second = await db.insert_chat_memory(
            period_start=2000, period_end=3000, text="ездили за грибами", created_at=3000
        )

        rows = await db.chat_memories(10)
        assert [row.id for row in rows] == [first, second]  # хронологически
        assert rows[0].text == "говорили о гараже"
        assert await db.chat_memory_count() == 2
        assert await db.last_chat_memory_end() == 3000

        one = await db.chat_memory(second)
        assert one is not None
        assert one.period_start == 2000

        assert await db.delete_chat_memory(second) is True
        assert await db.delete_chat_memory(second) is False
        assert await db.chat_memory(second) is None
        assert await db.last_chat_memory_end() == 2000
    finally:
        await db.close()


async def test_chat_memories_returns_last_limit_chronologically(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        for index in range(5):
            await db.insert_chat_memory(
                period_start=index * 100,
                period_end=(index + 1) * 100,
                text=f"неделя {index}",
                created_at=(index + 1) * 100,
            )

        rows = await db.chat_memories(2)
        assert [row.text for row in rows] == ["неделя 3", "неделя 4"]
        # in_prompt = 0 -> память в промпт не подмешивается, лишнего запроса нет
        assert await db.chat_memories(0) == []
    finally:
        await db.close()


async def test_last_chat_memory_end_is_none_when_empty(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        assert await db.last_chat_memory_end() is None
        assert await db.chat_memory_count() == 0
        assert await db.chat_memories(5) == []
    finally:
        await db.close()


async def test_purge_chat_memory_older_than_uses_period_end(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        old = await db.insert_chat_memory(
            period_start=0, period_end=1000, text="давнее", created_at=1000
        )
        fresh = await db.insert_chat_memory(
            period_start=1000, period_end=2000, text="свежее", created_at=2000
        )

        assert await db.purge_chat_memory_older_than(1500) == 1
        assert await db.chat_memory(old) is None
        assert await db.chat_memory(fresh) is not None
    finally:
        await db.close()


async def test_messages_between_is_half_open_and_skips_bots(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        for index, created_at in enumerate((900, 1000, 1500, 2000, 2100)):
            await db.insert_message(
                tg_message_id=index + 1,
                chat_id=42,
                user_id=1,
                display_name="Дима",
                text=str(created_at),
                reply_to_tg_message_id=None,
                is_bot=False,
                created_at=created_at,
            )
        await db.insert_message(
            tg_message_id=99,
            chat_id=42,
            user_id=999,
            display_name="Фёдор",
            text="реплика бота",
            reply_to_tg_message_id=None,
            is_bot=True,
            created_at=1200,
        )
        await db.insert_message(
            tg_message_id=100,
            chat_id=7,
            user_id=1,
            display_name="Чужой",
            text="другой чат",
            reply_to_tg_message_id=None,
            is_bot=False,
            created_at=1200,
        )

        rows = await db.messages_between(42, 1000, 2000)
        assert [row.text for row in rows] == ["1000", "1500"]  # start включён, end нет
    finally:
        await db.close()


async def test_messages_between_excludes_muted_user_ids(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        for user_id, name in ((1, "Дима"), (2, "Молчун")):
            await db.insert_message(
                tg_message_id=user_id,
                chat_id=42,
                user_id=user_id,
                display_name=name,
                text=name,
                reply_to_tg_message_id=None,
                is_bot=False,
                created_at=1000 + user_id,
            )

        rows = await db.messages_between(42, 0, 5000, exclude_user_ids=frozenset({2}))
        assert [row.display_name for row in rows] == ["Дима"]

        rows = await db.messages_between(42, 0, 5000)
        assert [row.display_name for row in rows] == ["Дима", "Молчун"]
    finally:
        await db.close()


async def test_bot_replies_between_is_half_open_and_chronological(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        for created_at in (900, 1000, 1500, 2000):
            await db.insert_bot_reply(
                tg_message_id=created_at,
                reply_to_tg_message_id=None,
                trigger="ambient",
                trigger_tg_message_id=None,
                text=str(created_at),
                prompt_version=1,
                few_shot_version=1,
                delay_sec=0,
                created_at=created_at,
            )

        rows = await db.bot_replies_between(1000, 2000)
        assert [row.text for row in rows] == ["1000", "1500"]
    finally:
        await db.close()


async def test_first_message_at_returns_earliest_of_chat(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        assert await db.first_message_at(42) is None

        for chat_id, created_at in ((42, 2000), (42, 1000), (7, 500)):
            await db.insert_message(
                tg_message_id=created_at,
                chat_id=chat_id,
                user_id=1,
                display_name="Дима",
                text="x",
                reply_to_tg_message_id=None,
                is_bot=False,
                created_at=created_at,
            )

        assert await db.first_message_at(42) == 1000
    finally:
        await db.close()
