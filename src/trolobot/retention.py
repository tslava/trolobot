"""Ретеншн: разовый прогон покупки (purge) и периодический фоновый таск.

`retention_loop` не завязан на config.py, чтобы не зависеть от модуля, который
пишет параллельно другой агент — конфиг передаётся через `cfg_getter`, типизированный
структурным Protocol: нужен только объект с `.behaviour.message_retention_days`
и `.persona.timezone`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Protocol

from trolobot.db import Database, PurgeStats

logger = logging.getLogger(__name__)


class _RetentionChatMemory(Protocol):
    @property
    def keep_days(self) -> int: ...


class _RetentionBehaviour(Protocol):
    # Свойства, а не атрибуты: mypy проверяет атрибуты Protocol инвариантно
    # и не принял бы BehaviourConfig на месте _RetentionBehaviour.
    @property
    def message_retention_days(self) -> int: ...
    @property
    def chat_memory(self) -> _RetentionChatMemory: ...


class _RetentionPersona(Protocol):
    @property
    def timezone(self) -> str: ...


class _RetentionConfig(Protocol):
    @property
    def behaviour(self) -> _RetentionBehaviour: ...
    @property
    def persona(self) -> _RetentionPersona: ...


async def run_retention(
    db: Database,
    retention_days: int,
    tz: str,
    now: int,
    chat_memory_keep_days: int | None = None,
) -> PurgeStats:
    """Удаляет из БД всё, что старше `retention_days` относительно `now`, и логирует итоги.

    `tz` — таймзона бота (persona.timezone): сутки, к которым привязаны датированные
    state-ключи, считаются локально, а не по UTC.

    `chat_memory_keep_days` — свой, гораздо больший срок для долгой памяти чата
    (CLAUDE.md, "долгая память чата"): пересказы живут дольше самих сообщений.
    None — память не трогаем вовсе (так зовут тесты и любой старый вызов с тремя
    аргументами).
    """
    cutoff = now - retention_days * 86400
    stats = await db.purge_older_than(cutoff, tz)
    if chat_memory_keep_days is not None:
        chat_memory_deleted = await db.purge_chat_memory_older_than(
            now - chat_memory_keep_days * 86400
        )
        stats = replace(stats, chat_memory_deleted=chat_memory_deleted)
    total = (
        stats.messages
        + stats.night_queue
        + stats.pending_replies
        + stats.filter_log_texts
        + stats.state_keys
        + stats.chat_memory_deleted
    )
    if total:
        logger.info(
            "retention purge: messages=%d night_queue=%d pending_replies=%d "
            "filter_log_texts=%d state_keys=%d chat_memory=%d",
            stats.messages,
            stats.night_queue,
            stats.pending_replies,
            stats.filter_log_texts,
            stats.state_keys,
            stats.chat_memory_deleted,
        )
    return stats


async def retention_loop(
    db: Database,
    cfg_getter: Callable[[], _RetentionConfig],
    interval_sec: int = 3600,
) -> None:
    """Бесконечный цикл ретеншна.

    Отменяется через asyncio-отмену таска; CancelledError пробрасывается наружу.
    """
    while True:
        try:
            cfg = cfg_getter()
            retention_days = cfg.behaviour.message_retention_days
            tz = cfg.persona.timezone
            keep_days = cfg.behaviour.chat_memory.keep_days
            await run_retention(db, retention_days, tz, int(time.time()), keep_days)
        except asyncio.CancelledError:
            logger.info("retention_loop: cancelled, stopping")
            raise
        except Exception:
            logger.exception("retention_loop: iteration failed")
        await asyncio.sleep(interval_sec)
