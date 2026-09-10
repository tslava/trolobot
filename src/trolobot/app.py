"""Сборка процесса: настройки, конфиг, БД, бот, фоновые таски (PLAN.md, этап 1)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from aiogram import Bot, Dispatcher

from trolobot.bot import Deps, build_router
from trolobot.config import load_config
from trolobot.config_models import Config
from trolobot.db import Database
from trolobot.few_shot import load_few_shot
from trolobot.retention import retention_loop
from trolobot.settings import Settings

logger = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def setup_logging(level: str) -> None:
    """stdout, формат из CLAUDE.md. Секреты сюда никогда не попадают."""
    logging.basicConfig(stream=sys.stdout, level=level, format=_LOG_FORMAT)


class ConfigHolder:
    """Изменяемый контейнер текущего Config.

    На этапе 1 config_getter всегда возвращает один и тот же объект — горячая
    перезагрузка появится в этапе 6. Класс существует уже сейчас, чтобы Deps.config_getter
    можно было завязать на .get() и потом просто подменять объект внутри через .set(),
    не меняя контракт bot.py.
    """

    def __init__(self, config: Config) -> None:
        self._config = config

    def get(self) -> Config:
        return self._config

    def set(self, config: Config) -> None:
        self._config = config


async def main() -> None:
    settings = Settings()
    setup_logging(settings.log_level)

    db = Database(settings.db_path)
    await db.connect()

    bot: Bot | None = None
    retention_task: asyncio.Task[None] | None = None
    try:
        overrides = await db.get_overrides()
        config = load_config(settings.config_path, overrides)

        # Валидация на старте: файлы должны читаться, иначе падаем сразу, а не на первом сообщении.
        load_few_shot(settings.few_shot_path)
        settings.prompt_path.read_text(encoding="utf-8")

        holder = ConfigHolder(config)

        bot = Bot(token=settings.bot_token.get_secret_value())
        me = await bot.get_me()
        reserved_names = {
            config.persona.name,
            config.persona.display_name,
            *config.persona.name_triggers,
            me.username or "",
        } - {""}

        deps = Deps(
            settings=settings,
            config_getter=holder.get,
            db=db,
            bot_user_id=me.id,
            reserved_names=reserved_names,
        )

        dispatcher = Dispatcher()
        dispatcher.include_router(build_router(deps))

        retention_task = asyncio.create_task(retention_loop(db, holder.get))

        logger.info(
            "started as @%s, allowed_chat_id=%s, discovery=%s",
            me.username,
            settings.allowed_chat_id,
            settings.allowed_chat_id == 0,
        )

        await dispatcher.start_polling(bot, allowed_updates=["message", "edited_message"])
    finally:
        if retention_task is not None:
            retention_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await retention_task
        if bot is not None:
            # aiogram закрывает сессию сама при штатном выходе из start_polling; повторный
            # вызов идемпотентен и здесь нужен на случай исключения до start_polling.
            await bot.session.close()
        await db.close()
