"""Database: подключение, миграции и методы-репозиторий поверх SQLite.

SQL пишется руками, без ORM. Схема живёт в schema.sql и применяется целиком,
когда PRAGMA user_version == 0. Дальнейшие миграции — пронумерованные скрипты
в MIGRATIONS, применяются по возрастанию номера версии.
"""

from __future__ import annotations

import asyncio
import importlib.resources
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite

from trolobot.gate_types import RecentActivity, StateChange

# Номерные миграции поверх исходной схемы (user_version == 1). Пока их нет —
# schema.sql уже описывает всю схему этапов 1 и 6. Ключ — целевая версия,
# значение — SQL-скрипт, применяемый через executescript.
MIGRATIONS: dict[int, str] = {}


@dataclass(frozen=True, slots=True)
class MessageRow:
    id: int
    tg_message_id: int | None
    chat_id: int | None
    user_id: int | None
    display_name: str | None
    text: str | None
    reply_to_tg_message_id: int | None
    is_bot: bool
    created_at: int | None


@dataclass(frozen=True, slots=True)
class PurgeStats:
    messages: int
    night_queue: int
    pending_replies: int
    filter_log_texts: int
    state_keys: int


@dataclass(frozen=True, slots=True)
class PendingRow:
    id: int
    trigger_tg_message_id: int
    user_id: int
    trigger: str
    due_at: int
    created_at: int
    done_at: int | None


@dataclass(frozen=True, slots=True)
class NightRow:
    id: int
    tg_message_id: int
    user_id: int
    display_name: str
    text: str
    created_at: int
    answered_at: int | None


def _state_key_date_suffix(key: str) -> str | None:
    """Суффикс после последнего ':' или None, если двоеточия в ключе нет."""
    if ":" not in key:
        return None
    return key.rsplit(":", 1)[-1]


def _parse_state_key_date(suffix: str) -> date | None:
    """Дата, за которую отвечает суффикс ключа: YYYY-MM-DD или YYYY-Www (понедельник).

    Возвращает None, если суффикс не похож ни на одно из этих написаний —
    такие ключи (например ":<user_id>") ретеншн не трогает.
    """
    try:
        return date.fromisoformat(suffix)
    except ValueError:
        pass
    match = re.fullmatch(r"(\d{4})-W(\d{2})", suffix)
    if match is None:
        return None
    year, week = int(match.group(1)), int(match.group(2))
    try:
        return date.fromisocalendar(year, week, 1)
    except ValueError:
        return None


def _is_expired_state_key(key: str, cutoff_date: date) -> bool:
    suffix = _state_key_date_suffix(key)
    if suffix is None:
        return False
    key_date = _parse_state_key_date(suffix)
    if key_date is None:
        return False
    return key_date < cutoff_date


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        # Сериализует все пишущие методы (включая commit) — один aiosqlite-connection
        # на процесс, конкурентные записи иначе гонятся друг за другом на уровне SQLite
        # и могут перемежать многостейтментные операции вроде purge_older_than.
        # Вызывающий код о сериализации не думает.
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self._path)
        conn.row_factory = aiosqlite.Row
        self._conn = conn
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await self.migrate()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected: call connect() first")
        return self._conn

    async def migrate(self) -> None:
        conn = self._require_conn()
        async with self._write_lock:
            cursor = await conn.execute("PRAGMA user_version")
            row = await cursor.fetchone()
            version = int(row[0]) if row is not None else 0

            if version == 0:
                schema_sql = (
                    importlib.resources.files("trolobot").joinpath("schema.sql").read_text("utf-8")
                )
                await conn.executescript(schema_sql)
                cursor = await conn.execute("PRAGMA user_version")
                row = await cursor.fetchone()
                version = int(row[0]) if row is not None else 0

            for target_version in sorted(MIGRATIONS):
                if target_version <= version:
                    continue
                await conn.executescript(MIGRATIONS[target_version])
                await conn.execute(f"PRAGMA user_version = {target_version}")
                await conn.commit()
                version = target_version

    async def insert_message(
        self,
        *,
        tg_message_id: int,
        chat_id: int,
        user_id: int,
        display_name: str,
        text: str,
        reply_to_tg_message_id: int | None,
        is_bot: bool,
        created_at: int,
    ) -> int:
        conn = self._require_conn()
        async with self._write_lock:
            cursor = await conn.execute(
                "INSERT INTO messages "
                "(tg_message_id, chat_id, user_id, display_name, text, "
                "reply_to_tg_message_id, is_bot, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tg_message_id,
                    chat_id,
                    user_id,
                    display_name,
                    text,
                    reply_to_tg_message_id,
                    int(is_bot),
                    created_at,
                ),
            )
            await conn.commit()
            if cursor.lastrowid is None:
                raise RuntimeError("insert_message: INSERT did not return a rowid")
            return cursor.lastrowid

    async def recent_messages(self, chat_id: int, limit: int) -> list[MessageRow]:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT id, tg_message_id, chat_id, user_id, display_name, text, "
            "reply_to_tg_message_id, is_bot, created_at "
            "FROM messages WHERE chat_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (chat_id, limit),
        )
        rows = await cursor.fetchall()
        messages = [
            MessageRow(
                id=row["id"],
                tg_message_id=row["tg_message_id"],
                chat_id=row["chat_id"],
                user_id=row["user_id"],
                display_name=row["display_name"],
                text=row["text"],
                reply_to_tg_message_id=row["reply_to_tg_message_id"],
                is_bot=bool(row["is_bot"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]
        messages.reverse()
        return messages

    async def get_state(self, key: str) -> str | None:
        conn = self._require_conn()
        cursor = await conn.execute("SELECT value FROM state WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return None if row is None else str(row["value"])

    async def set_state(self, key: str, value: str) -> None:
        conn = self._require_conn()
        async with self._write_lock:
            await conn.execute(
                "INSERT INTO state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await conn.commit()

    async def delete_state(self, key: str) -> None:
        conn = self._require_conn()
        async with self._write_lock:
            await conn.execute("DELETE FROM state WHERE key = ?", (key,))
            await conn.commit()

    async def get_overrides(self) -> dict[str, str]:
        conn = self._require_conn()
        cursor = await conn.execute("SELECT key, value FROM config_overrides")
        rows = await cursor.fetchall()
        return {row["key"]: row["value"] for row in rows}

    async def purge_older_than(self, cutoff: int, tz: str = "UTC") -> PurgeStats:
        conn = self._require_conn()
        async with self._write_lock:
            cursor = await conn.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
            messages_deleted = cursor.rowcount

            cursor = await conn.execute(
                "DELETE FROM night_queue WHERE answered_at IS NOT NULL AND answered_at < ?",
                (cutoff,),
            )
            night_queue_deleted = cursor.rowcount

            cursor = await conn.execute(
                "DELETE FROM pending_replies WHERE done_at IS NOT NULL AND done_at < ?",
                (cutoff,),
            )
            pending_replies_deleted = cursor.rowcount

            cursor = await conn.execute(
                "UPDATE filter_log SET candidate_text = NULL "
                "WHERE created_at < ? AND candidate_text IS NOT NULL",
                (cutoff,),
            )
            filter_log_texts_cleared = cursor.rowcount

            # Сутки бота локальные (persona.timezone), а не UTC: суффикс :YYYY-MM-DD/:YYYY-Www
            # сравнивается с датой cutoff в tz, иначе ключ "сегодняшнего" дня по Варшаве может
            # удалиться на несколько часов раньше/позже, чем реально наступили эти сутки у бота.
            cutoff_date = datetime.fromtimestamp(cutoff, ZoneInfo(tz)).date()
            cursor = await conn.execute("SELECT key FROM state")
            state_rows = await cursor.fetchall()
            expired_keys = [
                str(row["key"])
                for row in state_rows
                if _is_expired_state_key(row["key"], cutoff_date)
            ]
            state_keys_deleted = 0
            if expired_keys:
                placeholders = ",".join("?" for _ in expired_keys)
                cursor = await conn.execute(
                    f"DELETE FROM state WHERE key IN ({placeholders})", expired_keys
                )
                state_keys_deleted = cursor.rowcount

            await conn.commit()

            return PurgeStats(
                messages=messages_deleted,
                night_queue=night_queue_deleted,
                pending_replies=pending_replies_deleted,
                filter_log_texts=filter_log_texts_cleared,
                state_keys=state_keys_deleted,
            )

    async def insert_filter_log(
        self,
        *,
        trigger_tg_message_id: int | None,
        candidate_text: str | None,
        verdict: str,
        stage: str,
        reason: str,
        shadow: bool,
        created_at: int,
    ) -> int:
        conn = self._require_conn()
        async with self._write_lock:
            cursor = await conn.execute(
                "INSERT INTO filter_log "
                "(trigger_tg_message_id, candidate_text, verdict, stage, reason, shadow, "
                "created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    trigger_tg_message_id,
                    candidate_text,
                    verdict,
                    stage,
                    reason,
                    int(shadow),
                    created_at,
                ),
            )
            await conn.commit()
            if cursor.lastrowid is None:
                raise RuntimeError("insert_filter_log: INSERT did not return a rowid")
            return cursor.lastrowid

    async def muted_user_ids(self) -> frozenset[int]:
        conn = self._require_conn()
        cursor = await conn.execute("SELECT user_id FROM muted_users")
        rows = await cursor.fetchall()
        return frozenset(int(row["user_id"]) for row in rows)

    async def recent_activity(self, chat_id: int, since: int) -> list[RecentActivity]:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT user_id, created_at FROM messages "
            "WHERE chat_id = ? AND is_bot = 0 AND created_at >= ? "
            "ORDER BY created_at, id",
            (chat_id, since),
        )
        rows = await cursor.fetchall()
        return [
            RecentActivity(user_id=row["user_id"], created_at=row["created_at"]) for row in rows
        ]

    async def enqueue_night(
        self,
        *,
        tg_message_id: int,
        user_id: int,
        display_name: str,
        text: str,
        created_at: int,
    ) -> int:
        conn = self._require_conn()
        async with self._write_lock:
            cursor = await conn.execute(
                "INSERT INTO night_queue "
                "(tg_message_id, user_id, display_name, text, created_at, answered_at) "
                "VALUES (?, ?, ?, ?, ?, NULL)",
                (tg_message_id, user_id, display_name, text, created_at),
            )
            await conn.commit()
            if cursor.lastrowid is None:
                raise RuntimeError("enqueue_night: INSERT did not return a rowid")
            return cursor.lastrowid

    async def apply_state_changes(self, changes: Iterable[StateChange]) -> None:
        conn = self._require_conn()
        async with self._write_lock:
            applied = False
            for change in changes:
                applied = True
                if change.value is None:
                    await conn.execute("DELETE FROM state WHERE key = ?", (change.key,))
                else:
                    await conn.execute(
                        "INSERT INTO state (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (change.key, change.value),
                    )
            if applied:
                await conn.commit()

    async def filter_log_summary(self, since: int) -> list[tuple[str, int]]:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT reason, COUNT(*) AS cnt FROM filter_log WHERE created_at >= ? "
            "GROUP BY reason ORDER BY cnt DESC, reason",
            (since,),
        )
        rows = await cursor.fetchall()
        return [(str(row["reason"]), int(row["cnt"])) for row in rows]

    async def recent_bot_replies(self, limit: int) -> list[str]:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT text FROM bot_replies ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        texts = [str(row["text"]) for row in rows]
        texts.reverse()
        return texts

    async def insert_bot_reply(
        self,
        *,
        tg_message_id: int,
        reply_to_tg_message_id: int | None,
        trigger: str,
        trigger_tg_message_id: int | None,
        text: str,
        prompt_version: int,
        few_shot_version: int,
        delay_sec: int,
        created_at: int,
    ) -> int:
        conn = self._require_conn()
        async with self._write_lock:
            cursor = await conn.execute(
                "INSERT INTO bot_replies "
                "(tg_message_id, reply_to_tg_message_id, trigger, trigger_tg_message_id, text, "
                "prompt_version, few_shot_version, delay_sec, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tg_message_id,
                    reply_to_tg_message_id,
                    trigger,
                    trigger_tg_message_id,
                    text,
                    prompt_version,
                    few_shot_version,
                    delay_sec,
                    created_at,
                ),
            )
            await conn.commit()
            if cursor.lastrowid is None:
                raise RuntimeError("insert_bot_reply: INSERT did not return a rowid")
            return cursor.lastrowid

    async def increment_state(self, key: str, by: int = 1) -> int:
        """Атомарный read-modify-write int-счётчика в state, весь под write_lock.

        Не переиспользует get_state/set_state: их собственный захват write_lock
        (asyncio.Lock не реентерабелен) привёл бы к дедлоку внутри уже взятого лока.
        """
        conn = self._require_conn()
        async with self._write_lock:
            cursor = await conn.execute("SELECT value FROM state WHERE key = ?", (key,))
            row = await cursor.fetchone()
            current = int(row["value"]) if row is not None else 0
            new_value = current + by
            await conn.execute(
                "INSERT INTO state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(new_value)),
            )
            await conn.commit()
            return new_value

    async def add_state_float(self, key: str, by: float) -> float:
        """Атомарный read-modify-write float-счётчика в state, хранится как str(value)."""
        conn = self._require_conn()
        async with self._write_lock:
            cursor = await conn.execute("SELECT value FROM state WHERE key = ?", (key,))
            row = await cursor.fetchone()
            current = float(row["value"]) if row is not None else 0.0
            new_value = current + by
            await conn.execute(
                "INSERT INTO state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(new_value)),
            )
            await conn.commit()
            return new_value

    async def insert_pending(
        self,
        *,
        trigger_tg_message_id: int,
        user_id: int,
        trigger: str,
        due_at: int,
        created_at: int,
    ) -> int:
        conn = self._require_conn()
        async with self._write_lock:
            cursor = await conn.execute(
                "INSERT INTO pending_replies "
                "(trigger_tg_message_id, user_id, trigger, due_at, created_at, done_at) "
                "VALUES (?, ?, ?, ?, ?, NULL)",
                (trigger_tg_message_id, user_id, trigger, due_at, created_at),
            )
            await conn.commit()
            if cursor.lastrowid is None:
                raise RuntimeError("insert_pending: INSERT did not return a rowid")
            return cursor.lastrowid

    async def update_pending_due(self, pending_id: int, due_at: int) -> None:
        conn = self._require_conn()
        async with self._write_lock:
            await conn.execute(
                "UPDATE pending_replies SET due_at = ? WHERE id = ?", (due_at, pending_id)
            )
            await conn.commit()

    async def mark_pending_done(self, pending_id: int, done_at: int) -> None:
        conn = self._require_conn()
        async with self._write_lock:
            await conn.execute(
                "UPDATE pending_replies SET done_at = ? WHERE id = ?", (done_at, pending_id)
            )
            await conn.commit()

    async def load_pending(self) -> list[PendingRow]:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT id, trigger_tg_message_id, user_id, trigger, due_at, created_at, done_at "
            "FROM pending_replies WHERE done_at IS NULL ORDER BY due_at, id"
        )
        rows = await cursor.fetchall()
        return [
            PendingRow(
                id=row["id"],
                trigger_tg_message_id=row["trigger_tg_message_id"],
                user_id=row["user_id"],
                trigger=row["trigger"],
                due_at=row["due_at"],
                created_at=row["created_at"],
                done_at=row["done_at"],
            )
            for row in rows
        ]

    async def night_unanswered(self) -> list[NightRow]:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT id, tg_message_id, user_id, display_name, text, created_at, answered_at "
            "FROM night_queue WHERE answered_at IS NULL ORDER BY created_at, id"
        )
        rows = await cursor.fetchall()
        return [
            NightRow(
                id=row["id"],
                tg_message_id=row["tg_message_id"],
                user_id=row["user_id"],
                display_name=row["display_name"],
                text=row["text"],
                created_at=row["created_at"],
                answered_at=row["answered_at"],
            )
            for row in rows
        ]

    async def mark_night_answered(self, ids: Sequence[int], answered_at: int) -> None:
        if not ids:
            return
        conn = self._require_conn()
        async with self._write_lock:
            placeholders = ",".join("?" for _ in ids)
            await conn.execute(
                f"UPDATE night_queue SET answered_at = ? WHERE id IN ({placeholders})",
                (answered_at, *ids),
            )
            await conn.commit()

    async def messages_after(self, chat_id: int, tg_message_id: int) -> int:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT COUNT(*) AS cnt FROM messages "
            "WHERE chat_id = ? AND tg_message_id > ? AND is_bot = 0",
            (chat_id, tg_message_id),
        )
        row = await cursor.fetchone()
        return int(row["cnt"]) if row is not None else 0

    async def last_message_at(self, chat_id: int) -> int | None:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT MAX(created_at) AS max_created_at FROM messages "
            "WHERE chat_id = ? AND is_bot = 0",
            (chat_id,),
        )
        row = await cursor.fetchone()
        if row is None or row["max_created_at"] is None:
            return None
        return int(row["max_created_at"])
