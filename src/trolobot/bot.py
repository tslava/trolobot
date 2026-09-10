"""aiogram Router и хендлер сообщений одного чата (PLAN.md, этап 1).

Хендлер только логирует и пишет в БД. Ничего не отвечает, команды не обрабатывает —
это принципиально для этапа 1, см. CLAUDE.md, раздел "Чего не делать".
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from aiogram import Router
from aiogram.types import Message

from trolobot.config_models import Config
from trolobot.db import Database
from trolobot.sanitize import media_placeholder, normalize_text, sanitize_display_name
from trolobot.settings import Settings

logger = logging.getLogger(__name__)

# Обрезка текста в INFO-логе (PLAN.md: лог "Имя: текст").
_LOG_TEXT_MAX_LEN = 200


@dataclass(slots=True)
class Deps:
    """Зависимости хендлера. config_getter — под горячую перезагрузку конфига (этап 6)."""

    settings: Settings
    config_getter: Callable[[], Config]
    db: Database
    bot_user_id: int
    reserved_names: set[str]


def build_router(deps: Deps) -> Router:
    """Собирает Router с единственным хендлером на сообщения чужого/своего чата.

    Отредактированные сообщения игнорируются целиком (отдельный обработчик, просто return) —
    правки истории чата не должны переписывать то, что уже попало в лог/БД.
    """
    router = Router(name=__name__)

    @router.edited_message()
    async def handle_edited_message(message: Message) -> None:
        return

    @router.message()
    async def handle_message(message: Message) -> None:
        allowed_chat_id = deps.settings.allowed_chat_id
        user = message.from_user

        if allowed_chat_id == 0:
            # Discovery mode: помогаем владельцу найти chat_id для .env. В БД не пишем.
            logger.warning(
                "discovery: chat_id=%s title=%r user_id=%s name=%r",
                message.chat.id,
                message.chat.title,
                user.id if user is not None else 0,
                user.full_name if user is not None else None,
            )
            return

        if message.chat.id != allowed_chat_id:
            # Чужой чат: ни лога, ни БД, чтобы не жечь ключ и не хранить чужие данные.
            return

        if user is None:
            # Анонимный админ или сообщение от имени канала.
            user_id = 0
            display_name = sanitize_display_name(None, 0, deps.reserved_names)
            is_bot = False
        else:
            user_id = user.id
            display_name = sanitize_display_name(user.full_name, user.id, deps.reserved_names)
            is_bot = user.is_bot

        text = normalize_text(message.text or message.caption)
        if not text:
            text = media_placeholder(message) or ""
        if not text:
            # Сервисное сообщение без текста и без медиа (вошёл/вышел и т.п.) — не пишем.
            return

        reply_to_tg_message_id = (
            message.reply_to_message.message_id if message.reply_to_message is not None else None
        )

        await deps.db.insert_message(
            tg_message_id=message.message_id,
            chat_id=message.chat.id,
            user_id=user_id,
            display_name=display_name,
            text=text,
            reply_to_tg_message_id=reply_to_tg_message_id,
            is_bot=is_bot,
            created_at=int(message.date.timestamp()),
        )
        logger.info("%s: %s", display_name, text[:_LOG_TEXT_MAX_LEN])

    return router
