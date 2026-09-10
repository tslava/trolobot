"""Парсер экспорта чата из Telegram Desktop (result.json) для реплея гейта.

Формат экспорта: корень — объект с "name", "type", "id", "messages": [...].
Сообщение может быть "message" (обычное) или "service" (служебное, например смена
названия чата) — служебные пропускаются. Порядок и детали разбора полей — см.
CLAUDE.md, раздел "Интерфейсы этапа 2".

Чистая функция, без сети и без БД. Битые записи (нет id или date) пропускаются
с WARNING в лог, а не роняют весь парсинг — экспорт за месяц почти наверняка
содержит хотя бы одну аномалию (стёртое сообщение, старый формат и т.п.).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trolobot.sanitize import normalize_text, sanitize_display_name

logger = logging.getLogger(__name__)

# from_id в экспорте — "user221675896" или "channel123": буквенный префикс + число.
_FROM_ID_RE = re.compile(r"(\d+)$")


@dataclass(frozen=True, slots=True)
class ExportMessage:
    tg_message_id: int
    user_id: int
    display_name: str
    text: str
    reply_to_tg_message_id: int | None
    created_at: int  # unix seconds


def _extract_user_id(from_id: object) -> int:
    """ "userNNN"/"channelNNN" -> NNN; при отсутствии или незнакомом формате -> 0."""
    if not isinstance(from_id, str):
        return 0
    match = _FROM_ID_RE.search(from_id)
    return int(match.group(1)) if match else 0


def _extract_text(raw_text: object) -> str:
    """text — либо строка, либо список строк/{"type":..., "text":...}; склеить подряд."""
    if isinstance(raw_text, str):
        return raw_text
    if isinstance(raw_text, list):
        parts: list[str] = []
        for item in raw_text:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text_value = item.get("text")
                if isinstance(text_value, str):
                    parts.append(text_value)
        return "".join(parts)
    return ""


def _media_placeholder(raw: dict[str, Any]) -> str | None:
    """Плейсхолдер для медиа без подписи, иначе None. Порядок — как в CLAUDE.md."""
    if isinstance(raw.get("photo"), str):
        return "[фото]"
    media_type = raw.get("media_type")
    if media_type == "sticker":
        return "[стикер]"
    if media_type == "voice_message":
        return "[голосовое]"
    if media_type in ("video_message", "video_file", "animation"):
        return "[видео]"
    if media_type == "audio_file":
        return "[голосовое]"
    if raw.get("file") is not None:
        return "[файл]"
    return None


def _parse_created_at(raw: dict[str, Any], tz: str, tg_message_id: int) -> int | None:
    """unix seconds: приоритет у "date_unixtime", иначе "date" (наивный ISO в tz)."""
    unixtime_raw = raw.get("date_unixtime")
    if isinstance(unixtime_raw, str):
        try:
            return int(unixtime_raw)
        except ValueError:
            logger.warning(
                "export_parser: message %d has invalid date_unixtime %r, falling back to date",
                tg_message_id,
                unixtime_raw,
            )

    date_raw = raw.get("date")
    if not isinstance(date_raw, str):
        return None
    try:
        naive = datetime.fromisoformat(date_raw)
    except ValueError:
        logger.warning("export_parser: message %d has unparsable date %r", tg_message_id, date_raw)
        return None
    return int(naive.replace(tzinfo=ZoneInfo(tz)).timestamp())


def _parse_message(raw: dict[str, Any], tz: str) -> ExportMessage | None:
    raw_id = raw.get("id")
    if not isinstance(raw_id, int):
        logger.warning("export_parser: skipping message without a valid id: %r", raw)
        return None

    if "date" not in raw and "date_unixtime" not in raw:
        logger.warning("export_parser: skipping message %d without a date", raw_id)
        return None

    created_at = _parse_created_at(raw, tz, raw_id)
    if created_at is None:
        logger.warning("export_parser: skipping message %d with unparsable date", raw_id)
        return None

    text = normalize_text(_extract_text(raw.get("text")))
    if not text:
        placeholder = _media_placeholder(raw)
        if placeholder is None:
            return None
        text = placeholder

    user_id = _extract_user_id(raw.get("from_id"))
    # reserved пустой: парсер не знает имя бота, вызывающий код (replay.py) пере-санитизирует.
    display_name = sanitize_display_name(raw.get("from"), user_id, reserved=set())

    reply_to_raw = raw.get("reply_to_message_id")
    reply_to = reply_to_raw if isinstance(reply_to_raw, int) else None

    return ExportMessage(
        tg_message_id=raw_id,
        user_id=user_id,
        display_name=display_name,
        text=text,
        reply_to_tg_message_id=reply_to,
        created_at=created_at,
    )


def parse_export(path: Path, tz: str) -> list[ExportMessage]:
    """Читает result.json Telegram Desktop и возвращает сообщения, отсортированные
    по (created_at, id). Служебные сообщения и битые записи пропускаются."""
    data: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object at top level")

    raw_messages = data.get("messages")
    if not isinstance(raw_messages, list):
        raise ValueError(f"{path}: 'messages' must be a list")

    result: list[ExportMessage] = []
    for raw in raw_messages:
        if not isinstance(raw, dict):
            logger.warning("export_parser: skipping non-object message entry: %r", raw)
            continue
        if raw.get("type") != "message":
            continue  # "service" и прочее — служебные, пропускаются молча
        parsed = _parse_message(raw, tz)
        if parsed is not None:
            result.append(parsed)

    result.sort(key=lambda m: (m.created_at, m.tg_message_id))
    return result
