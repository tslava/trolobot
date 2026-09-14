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

# Номерные миграции поверх исходной схемы (user_version == 1). Ключ — целевая
# версия, значение — SQL-скрипт, применяемый через executescript. Скрипт для
# версии 2 — тот же CREATE TABLE IF NOT EXISTS, что и в schema.sql (для свежих
# БД таблицу уже создаёт schema.sql, здесь IF NOT EXISTS на случай, если кто-то
# всё же дойдёт сюда с уже существующей таблицей).
MIGRATIONS: dict[int, str] = {
    2: """
    CREATE TABLE IF NOT EXISTS life_events (
        id INTEGER PRIMARY KEY,
        text TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        announced_at INTEGER,
        announced_tg_message_id INTEGER
    );
    """,
    3: """
    CREATE TABLE IF NOT EXISTS chat_memory (
        id INTEGER PRIMARY KEY,
        period_start INTEGER NOT NULL,
        period_end INTEGER NOT NULL,
        text TEXT NOT NULL,
        created_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_chat_memory_period_end ON chat_memory (period_end);
    """,
}


@dataclass(frozen=True, slots=True)
class LifeEventRow:
    """Событие жизни персонажа, заведённое владельцем через /life (CLAUDE.md, «события жизни»)."""

    id: int
    text: str
    created_at: int
    announced_at: int | None
    announced_tg_message_id: int | None


@dataclass(frozen=True, slots=True)
class ChatMemoryRow:
    """Пересказ одного периода чата (CLAUDE.md, "долгая память чата").

    ``period_start`` включительно, ``period_end`` исключительно — обе границы
    приходятся на локальную полночь (persona.timezone), см. chat_memory.py.
    """

    id: int
    period_start: int
    period_end: int
    text: str
    created_at: int


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
    # Пересказы долгой памяти чата живут по своему сроку (behaviour.chat_memory.keep_days),
    # а не по message_retention_days, поэтому чистятся отдельным вызовом из run_retention —
    # у поля есть дефолт, чтобы уже существующий код, собирающий PurgeStats позиционно,
    # не ломался (CLAUDE.md, "долгая память чата").
    chat_memory_deleted: int = 0


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


@dataclass(frozen=True, slots=True)
class BotReplyRow:
    id: int
    tg_message_id: int
    trigger: str
    trigger_tg_message_id: int | None
    text: str
    prompt_version: int
    few_shot_version: int
    delay_sec: int
    created_at: int


@dataclass(frozen=True, slots=True)
class VersionRow:
    version: int
    note: str
    active: bool
    created_at: int


@dataclass(frozen=True, slots=True)
class PlaceRow:
    """Одна строка таблицы places (schema.sql, PLAN.md этап 5).

    price_level — из Google, ненадёжен и иногда отсутствует (PLAN.md, п.4),
    поэтому int | None. quiet и fact проставляются руками/скриптом наполнения
    (places_fill.py), а не Google — quiet всегда bool.
    """

    place_id: str
    name: str
    district: str
    category: str
    rating: float
    reviews: int
    price_level: int | None
    quiet: bool
    fact: str
    operational: bool
    refreshed_at: int


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

    async def display_names(self, user_ids: Sequence[int]) -> dict[int, str]:
        """Последний (по created_at) display_name каждого user_id из messages.

        Пустой список -> {}. Один запрос для всех user_ids, отсортированный по
        свежести (created_at, id DESC) — первое вхождение каждого user_id в
        результате и есть самое свежее его имя.
        """
        if not user_ids:
            return {}
        conn = self._require_conn()
        unique_ids = list(dict.fromkeys(user_ids))
        placeholders = ",".join("?" for _ in unique_ids)
        cursor = await conn.execute(
            f"SELECT user_id, display_name FROM messages "
            f"WHERE user_id IN ({placeholders}) "
            f"ORDER BY created_at DESC, id DESC",
            unique_ids,
        )
        rows = await cursor.fetchall()
        result: dict[int, str] = {}
        for row in rows:
            user_id = int(row["user_id"])
            if user_id not in result:
                result[user_id] = str(row["display_name"])
        return result

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

    async def last_bot_reply_at(self) -> int | None:
        """created_at последней реплики бота (bot_replies) — момент, от которого
        responder._maybe_checkin (CLAUDE.md, "внимание как у живого человека:
        вернулся проверить") считает "тему умершей" и берёт messages_since."""
        conn = self._require_conn()
        cursor = await conn.execute("SELECT MAX(created_at) AS max_created_at FROM bot_replies")
        row = await cursor.fetchone()
        if row is None or row["max_created_at"] is None:
            return None
        return int(row["max_created_at"])

    # -- этап 6: управление из телеграма -----------------------------------

    async def set_override(self, key: str, value: str, changed_by: int, now: int) -> str | None:
        """UPSERT в config_overrides + строка в config_audit, одной транзакцией.

        Возвращает предыдущее значение (None, если ключ не был переопределён).
        """
        conn = self._require_conn()
        async with self._write_lock:
            cursor = await conn.execute("SELECT value FROM config_overrides WHERE key = ?", (key,))
            row = await cursor.fetchone()
            old_value = None if row is None else str(row["value"])
            await conn.execute(
                "INSERT INTO config_overrides (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, value, now),
            )
            await conn.execute(
                "INSERT INTO config_audit (key, old_value, new_value, changed_by, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (key, old_value, value, changed_by, now),
            )
            await conn.commit()
            return old_value

    async def delete_override(self, key: str, changed_by: int, now: int) -> str | None:
        """DELETE из config_overrides + строка в config_audit (new_value=NULL), одной транзакцией.

        Возвращает значение до удаления (None, если ключ и не был переопределён).
        """
        conn = self._require_conn()
        async with self._write_lock:
            cursor = await conn.execute("SELECT value FROM config_overrides WHERE key = ?", (key,))
            row = await cursor.fetchone()
            old_value = None if row is None else str(row["value"])
            await conn.execute("DELETE FROM config_overrides WHERE key = ?", (key,))
            await conn.execute(
                "INSERT INTO config_audit (key, old_value, new_value, changed_by, created_at) "
                "VALUES (?, ?, NULL, ?, ?)",
                (key, old_value, changed_by, now),
            )
            await conn.commit()
            return old_value

    async def audit_stop(
        self, key: str, changed_by: int, now: int, new_value: str | None = None
    ) -> None:
        """Строка в config_audit без изменения config_overrides — для /stop, /panic, /life, /say.

        new_value — текст события/реплики для /life и /say; у переключателей None.
        """
        conn = self._require_conn()
        async with self._write_lock:
            await conn.execute(
                "INSERT INTO config_audit (key, old_value, new_value, changed_by, created_at) "
                "VALUES (?, NULL, ?, ?, ?)",
                (key, new_value, changed_by, now),
            )
            await conn.commit()

    async def add_mute(self, user_id: int, display_name: str, muted_by: int, now: int) -> None:
        conn = self._require_conn()
        async with self._write_lock:
            await conn.execute(
                "INSERT INTO muted_users (user_id, display_name, muted_by, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET display_name = excluded.display_name, "
                "muted_by = excluded.muted_by, created_at = excluded.created_at",
                (user_id, display_name, muted_by, now),
            )
            await conn.commit()

    async def remove_mute(self, user_id: int) -> bool:
        conn = self._require_conn()
        async with self._write_lock:
            cursor = await conn.execute("DELETE FROM muted_users WHERE user_id = ?", (user_id,))
            await conn.commit()
            return cursor.rowcount > 0

    async def last_bot_replies(self, n: int) -> list[BotReplyRow]:
        """Последние n реплик бота, новые первыми."""
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT id, tg_message_id, trigger, trigger_tg_message_id, text, prompt_version, "
            "few_shot_version, delay_sec, created_at FROM bot_replies "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (n,),
        )
        rows = await cursor.fetchall()
        return [
            BotReplyRow(
                id=row["id"],
                tg_message_id=row["tg_message_id"],
                trigger=row["trigger"],
                trigger_tg_message_id=row["trigger_tg_message_id"],
                text=row["text"],
                prompt_version=row["prompt_version"],
                few_shot_version=row["few_shot_version"],
                delay_sec=row["delay_sec"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def prompt_versions(self) -> list[VersionRow]:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT version, note, active, created_at FROM prompt_versions ORDER BY version"
        )
        rows = await cursor.fetchall()
        return [
            VersionRow(
                version=row["version"],
                note=row["note"],
                active=bool(row["active"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def active_prompt(self) -> tuple[int, str] | None:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT version, body FROM prompt_versions WHERE active = 1 LIMIT 1"
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return (int(row["version"]), str(row["body"]))

    async def add_prompt_version(self, body: str, note: str, now: int) -> int:
        """Новая версия = max(version)+1, становится active, остальные active=0.

        Одна транзакция: без неё конкурентный вызов мог бы дважды прочитать
        один и тот же max(version) и оставить две активные версии.
        """
        conn = self._require_conn()
        async with self._write_lock:
            cursor = await conn.execute("SELECT MAX(version) AS max_version FROM prompt_versions")
            row = await cursor.fetchone()
            next_version = (
                1 if row is None or row["max_version"] is None else int(row["max_version"]) + 1
            )
            await conn.execute("UPDATE prompt_versions SET active = 0")
            await conn.execute(
                "INSERT INTO prompt_versions (version, body, note, active, created_at) "
                "VALUES (?, ?, ?, 1, ?)",
                (next_version, body, note, now),
            )
            await conn.commit()
            return next_version

    async def activate_prompt(self, version: int) -> bool:
        conn = self._require_conn()
        async with self._write_lock:
            cursor = await conn.execute(
                "SELECT 1 FROM prompt_versions WHERE version = ?", (version,)
            )
            exists = await cursor.fetchone()
            if exists is None:
                return False
            await conn.execute("UPDATE prompt_versions SET active = 0")
            await conn.execute(
                "UPDATE prompt_versions SET active = 1 WHERE version = ?", (version,)
            )
            await conn.commit()
            return True

    async def active_few_shot(self) -> tuple[int, str] | None:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT version, body_yaml FROM few_shot_versions WHERE active = 1 LIMIT 1"
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return (int(row["version"]), str(row["body_yaml"]))

    async def add_few_shot_version(self, body_yaml: str, note: str, now: int) -> int:
        """Как add_prompt_version, но для few_shot_versions — независимая нумерация."""
        conn = self._require_conn()
        async with self._write_lock:
            cursor = await conn.execute("SELECT MAX(version) AS max_version FROM few_shot_versions")
            row = await cursor.fetchone()
            next_version = (
                1 if row is None or row["max_version"] is None else int(row["max_version"]) + 1
            )
            await conn.execute("UPDATE few_shot_versions SET active = 0")
            await conn.execute(
                "INSERT INTO few_shot_versions (version, body_yaml, note, active, created_at) "
                "VALUES (?, ?, ?, 1, ?)",
                (next_version, body_yaml, note, now),
            )
            await conn.commit()
            return next_version

    async def activate_few_shot(self, version: int) -> bool:
        conn = self._require_conn()
        async with self._write_lock:
            cursor = await conn.execute(
                "SELECT 1 FROM few_shot_versions WHERE version = ?", (version,)
            )
            exists = await cursor.fetchone()
            if exists is None:
                return False
            await conn.execute("UPDATE few_shot_versions SET active = 0")
            await conn.execute(
                "UPDATE few_shot_versions SET active = 1 WHERE version = ?", (version,)
            )
            await conn.commit()
            return True

    async def message_by_tg_id(self, chat_id: int, tg_message_id: int) -> MessageRow | None:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT id, tg_message_id, chat_id, user_id, display_name, text, "
            "reply_to_tg_message_id, is_bot, created_at FROM messages "
            "WHERE chat_id = ? AND tg_message_id = ? ORDER BY id DESC LIMIT 1",
            (chat_id, tg_message_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return MessageRow(
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

    async def messages_since(
        self, chat_id: int, since: int, limit: int | None = None
    ) -> list[MessageRow]:
        """Человеческие сообщения чата с created_at > since, хронологически.

        ``limit`` не задан (по умолчанию) — используется
        ``responder._collect_addressed_items`` для восстановления накопленных
        обращений к боту после рестарта процесса (когда ``_pending_info``
        пуст), возвращает все подходящие строки. ``limit`` задан — используется
        ``responder._maybe_checkin`` (CLAUDE.md, "внимание как у живого
        человека: вернулся проверить"): последние ``limit`` строк, но всё
        равно в хронологическом порядке.
        """
        conn = self._require_conn()
        if limit is None:
            cursor = await conn.execute(
                "SELECT id, tg_message_id, chat_id, user_id, display_name, text, "
                "reply_to_tg_message_id, is_bot, created_at FROM messages "
                "WHERE chat_id = ? AND is_bot = 0 AND created_at > ? ORDER BY created_at, id",
                (chat_id, since),
            )
            rows = await cursor.fetchall()
        else:
            cursor = await conn.execute(
                "SELECT id, tg_message_id, chat_id, user_id, display_name, text, "
                "reply_to_tg_message_id, is_bot, created_at FROM messages "
                "WHERE chat_id = ? AND is_bot = 0 AND created_at > ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (chat_id, since, limit),
            )
            rows = list(await cursor.fetchall())
            rows.reverse()
        return [
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

    async def bot_reply_by_tg_id(self, tg_message_id: int) -> BotReplyRow | None:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT id, tg_message_id, trigger, trigger_tg_message_id, text, prompt_version, "
            "few_shot_version, delay_sec, created_at FROM bot_replies WHERE tg_message_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (tg_message_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return BotReplyRow(
            id=row["id"],
            tg_message_id=row["tg_message_id"],
            trigger=row["trigger"],
            trigger_tg_message_id=row["trigger_tg_message_id"],
            text=row["text"],
            prompt_version=row["prompt_version"],
            few_shot_version=row["few_shot_version"],
            delay_sec=row["delay_sec"],
            created_at=row["created_at"],
        )

    async def prompt_version_bodies(self) -> list[str]:
        """Тела всех сохранённых версий системного промпта (для сида PromptStore)."""
        conn = self._require_conn()
        cursor = await conn.execute("SELECT body FROM prompt_versions")
        rows = await cursor.fetchall()
        return [str(row["body"]) for row in rows]

    async def few_shot_version_bodies(self) -> list[str]:
        """Тела всех сохранённых версий few-shot (для сида PromptStore)."""
        conn = self._require_conn()
        cursor = await conn.execute("SELECT body_yaml FROM few_shot_versions")
        rows = await cursor.fetchall()
        return [str(row["body_yaml"]) for row in rows]

    # -- этап 5: заведения ---------------------------------------------------

    async def upsert_place(self, row: PlaceRow) -> None:
        """UPSERT по place_id (заполнение кэша, places_fill.py — другой агент)."""
        conn = self._require_conn()
        async with self._write_lock:
            await conn.execute(
                "INSERT INTO places "
                "(place_id, name, district, category, rating, reviews, price_level, quiet, "
                "fact, operational, refreshed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(place_id) DO UPDATE SET "
                "name = excluded.name, district = excluded.district, "
                "category = excluded.category, rating = excluded.rating, "
                "reviews = excluded.reviews, price_level = excluded.price_level, "
                "quiet = excluded.quiet, fact = excluded.fact, "
                "operational = excluded.operational, refreshed_at = excluded.refreshed_at",
                (
                    row.place_id,
                    row.name,
                    row.district,
                    row.category,
                    row.rating,
                    row.reviews,
                    row.price_level,
                    int(row.quiet),
                    row.fact,
                    int(row.operational),
                    row.refreshed_at,
                ),
            )
            await conn.commit()

    async def places_all(self, *, operational_only: bool = True) -> list[PlaceRow]:
        """Все заведения, по name. operational_only=True (дефолт) — только рабочие."""
        conn = self._require_conn()
        query = (
            "SELECT place_id, name, district, category, rating, reviews, price_level, "
            "quiet, fact, operational, refreshed_at FROM places"
        )
        params: tuple[object, ...] = ()
        if operational_only:
            query += " WHERE operational = ?"
            params = (1,)
        query += " ORDER BY name"
        cursor = await conn.execute(query, params)
        rows = await cursor.fetchall()
        return [
            PlaceRow(
                place_id=row["place_id"],
                name=row["name"],
                district=row["district"],
                category=row["category"],
                rating=row["rating"],
                reviews=row["reviews"],
                price_level=row["price_level"],
                quiet=bool(row["quiet"]),
                fact=row["fact"] or "",
                operational=bool(row["operational"]),
                refreshed_at=row["refreshed_at"],
            )
            for row in rows
        ]

    async def places_names(self) -> list[str]:
        """Названия только рабочих заведений — белый список для filters.py."""
        conn = self._require_conn()
        cursor = await conn.execute("SELECT name FROM places WHERE operational = 1 ORDER BY name")
        rows = await cursor.fetchall()
        return [str(row["name"]) for row in rows]

    async def mark_places_not_seen(self, seen_ids: Sequence[str], now: int) -> int:
        """После прогона places_fill.py с Google — гасит места, которые были
        operational, но в этом прогоне не встретились (закрылись, отфильтровались
        по rating/reviews или просто не попали ни в один запрос): operational=0,
        refreshed_at=now. Записи ``--manual-only`` (place_id с префиксом "manual:")
        Google не находит никогда, поэтому они не тронуты независимо от seen_ids.
        Возвращает число помеченных строк.
        """
        conn = self._require_conn()
        async with self._write_lock:
            if seen_ids:
                placeholders = ",".join("?" for _ in seen_ids)
                query = (
                    "UPDATE places SET operational = 0, refreshed_at = ? "
                    f"WHERE operational = 1 AND place_id NOT LIKE 'manual:%' "
                    f"AND place_id NOT IN ({placeholders})"
                )
                params: tuple[object, ...] = (now, *seen_ids)
            else:
                query = (
                    "UPDATE places SET operational = 0, refreshed_at = ? "
                    "WHERE operational = 1 AND place_id NOT LIKE 'manual:%'"
                )
                params = (now,)
            cursor = await conn.execute(query, params)
            await conn.commit()
            return cursor.rowcount

    # -- события жизни (/life, CLAUDE.md "события жизни") -------------------

    async def insert_life_event(self, *, text: str, created_at: int) -> int:
        conn = self._require_conn()
        async with self._write_lock:
            cursor = await conn.execute(
                "INSERT INTO life_events (text, created_at, announced_at, "
                "announced_tg_message_id) VALUES (?, ?, NULL, NULL)",
                (text, created_at),
            )
            await conn.commit()
            if cursor.lastrowid is None:
                raise RuntimeError("insert_life_event: INSERT did not return a rowid")
            return cursor.lastrowid

    async def life_events(self) -> list[LifeEventRow]:
        """Все события, по created_at asc, id asc (старые сначала — как в промпте)."""
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT id, text, created_at, announced_at, announced_tg_message_id "
            "FROM life_events ORDER BY created_at, id"
        )
        rows = await cursor.fetchall()
        return [
            LifeEventRow(
                id=row["id"],
                text=row["text"],
                created_at=row["created_at"],
                announced_at=row["announced_at"],
                announced_tg_message_id=row["announced_tg_message_id"],
            )
            for row in rows
        ]

    async def life_event(self, event_id: int) -> LifeEventRow | None:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT id, text, created_at, announced_at, announced_tg_message_id "
            "FROM life_events WHERE id = ?",
            (event_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return LifeEventRow(
            id=row["id"],
            text=row["text"],
            created_at=row["created_at"],
            announced_at=row["announced_at"],
            announced_tg_message_id=row["announced_tg_message_id"],
        )

    async def delete_life_event(self, event_id: int) -> bool:
        conn = self._require_conn()
        async with self._write_lock:
            cursor = await conn.execute("DELETE FROM life_events WHERE id = ?", (event_id,))
            await conn.commit()
            return cursor.rowcount > 0

    async def mark_life_event_announced(
        self, event_id: int, *, tg_message_id: int, now: int
    ) -> None:
        conn = self._require_conn()
        async with self._write_lock:
            await conn.execute(
                "UPDATE life_events SET announced_at = ?, announced_tg_message_id = ? WHERE id = ?",
                (now, tg_message_id, event_id),
            )
            await conn.commit()

    # -- долгая память чата (CLAUDE.md, "долгая память чата") ----------------

    async def insert_chat_memory(
        self, *, period_start: int, period_end: int, text: str, created_at: int
    ) -> int:
        conn = self._require_conn()
        async with self._write_lock:
            cursor = await conn.execute(
                "INSERT INTO chat_memory (period_start, period_end, text, created_at) "
                "VALUES (?, ?, ?, ?)",
                (period_start, period_end, text, created_at),
            )
            await conn.commit()
            if cursor.lastrowid is None:
                raise RuntimeError("insert_chat_memory: INSERT did not return a rowid")
            return cursor.lastrowid

    async def chat_memories(self, limit: int) -> list[ChatMemoryRow]:
        """Последние ``limit`` пересказов, возвращаются хронологически (старые сначала).

        ``limit <= 0`` -> пустой список: ``behaviour.chat_memory.in_prompt = 0``
        означает «память в промпт не подмешивать», и лишнего запроса тут не нужно.
        """
        if limit <= 0:
            return []
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT id, period_start, period_end, text, created_at FROM chat_memory "
            "ORDER BY period_end DESC, id DESC LIMIT ?",
            (limit,),
        )
        rows = list(await cursor.fetchall())
        rows.reverse()
        return [
            ChatMemoryRow(
                id=row["id"],
                period_start=row["period_start"],
                period_end=row["period_end"],
                text=row["text"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def chat_memory(self, memory_id: int) -> ChatMemoryRow | None:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT id, period_start, period_end, text, created_at FROM chat_memory WHERE id = ?",
            (memory_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return ChatMemoryRow(
            id=row["id"],
            period_start=row["period_start"],
            period_end=row["period_end"],
            text=row["text"],
            created_at=row["created_at"],
        )

    async def chat_memory_count(self) -> int:
        """Сколько пересказов лежит в БД всего — для строки /status; списком их
        тянуть незачем (за год их набирается полсотни и больше)."""
        conn = self._require_conn()
        cursor = await conn.execute("SELECT COUNT(*) AS n FROM chat_memory")
        row = await cursor.fetchone()
        return 0 if row is None else int(row["n"])

    async def delete_chat_memory(self, memory_id: int) -> bool:
        conn = self._require_conn()
        async with self._write_lock:
            cursor = await conn.execute("DELETE FROM chat_memory WHERE id = ?", (memory_id,))
            await conn.commit()
            return cursor.rowcount > 0

    async def last_chat_memory_end(self) -> int | None:
        """max(period_end) — с этого момента начинается следующий период пересказа."""
        conn = self._require_conn()
        cursor = await conn.execute("SELECT MAX(period_end) AS end FROM chat_memory")
        row = await cursor.fetchone()
        if row is None or row["end"] is None:
            return None
        return int(row["end"])

    async def purge_chat_memory_older_than(self, cutoff: int) -> int:
        """Удаляет пересказы, чей период закончился раньше ``cutoff`` (по period_end)."""
        conn = self._require_conn()
        async with self._write_lock:
            cursor = await conn.execute("DELETE FROM chat_memory WHERE period_end < ?", (cutoff,))
            await conn.commit()
            return int(cursor.rowcount)

    async def first_message_at(self, chat_id: int) -> int | None:
        """created_at самого раннего сообщения чата — точка отсчёта для догоняющего
        прогона долгой памяти (chat_memory.run_due), когда пересказов ещё нет."""
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT MIN(created_at) AS first FROM messages WHERE chat_id = ?", (chat_id,)
        )
        row = await cursor.fetchone()
        if row is None or row["first"] is None:
            return None
        return int(row["first"])

    async def messages_between(
        self,
        chat_id: int,
        start: int,
        end: int,
        *,
        exclude_user_ids: Iterable[int] = (),
    ) -> list[MessageRow]:
        """Человеческие сообщения чата за ``[start, end)``, хронологически.

        ``exclude_user_ids`` — замьюченные участники: их сообщения в пересказ не
        попадают вовсе (осознанное решение по приватности, CLAUDE.md, "долгая
        память чата").
        """
        conn = self._require_conn()
        excluded = sorted({int(user_id) for user_id in exclude_user_ids})
        sql = (
            "SELECT id, tg_message_id, chat_id, user_id, display_name, text, "
            "reply_to_tg_message_id, is_bot, created_at FROM messages "
            "WHERE chat_id = ? AND is_bot = 0 AND created_at >= ? AND created_at < ?"
        )
        params: list[int] = [chat_id, start, end]
        if excluded:
            placeholders = ",".join("?" for _ in excluded)
            sql += f" AND (user_id IS NULL OR user_id NOT IN ({placeholders}))"
            params.extend(excluded)
        sql += " ORDER BY created_at, id"
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [
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

    async def bot_replies_between(self, start: int, end: int) -> list[BotReplyRow]:
        """Реплики бота за ``[start, end)``, хронологически — вторая половина
        материала для пересказа периода."""
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT id, tg_message_id, trigger, trigger_tg_message_id, text, prompt_version, "
            "few_shot_version, delay_sec, created_at FROM bot_replies "
            "WHERE created_at >= ? AND created_at < ? ORDER BY created_at, id",
            (start, end),
        )
        rows = await cursor.fetchall()
        return [
            BotReplyRow(
                id=row["id"],
                tg_message_id=row["tg_message_id"],
                trigger=row["trigger"],
                trigger_tg_message_id=row["trigger_tg_message_id"],
                text=row["text"],
                prompt_version=row["prompt_version"],
                few_shot_version=row["few_shot_version"],
                delay_sec=row["delay_sec"],
                created_at=row["created_at"],
            )
            for row in rows
        ]
