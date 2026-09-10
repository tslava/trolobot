"""Сборка процесса: настройки, БД, конфиг/промпт-хранилища, бот, фоновые таски.

PLAN.md, этапы 1 и 6.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import sys

import httpx
from aiogram import Bot, Dispatcher

from trolobot.bot import Deps, build_router
from trolobot.commands import build_commands_router
from trolobot.db import Database
from trolobot.judge import Judge
from trolobot.llm import LLMClient
from trolobot.responder import Responder
from trolobot.retention import retention_loop
from trolobot.settings import Settings
from trolobot.stores import ConfigStore, PromptStore

logger = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def setup_logging(level: str) -> None:
    """stdout, формат из CLAUDE.md. Секреты сюда никогда не попадают."""
    logging.basicConfig(stream=sys.stdout, level=level, format=_LOG_FORMAT)


async def main() -> None:
    settings = Settings()
    setup_logging(settings.log_level)

    db = Database(settings.db_path)
    await db.connect()

    bot: Bot | None = None
    retention_task: asyncio.Task[None] | None = None
    morning_task: asyncio.Task[None] | None = None
    spontaneous_task: asyncio.Task[None] | None = None
    llm: LLMClient | None = None
    responder: Responder | None = None
    try:
        config_store = ConfigStore(settings.config_path, db)
        await config_store.load()

        prompt_store = PromptStore(db, settings.prompt_path, settings.few_shot_path)
        await prompt_store.load()

        bot = Bot(token=settings.bot_token.get_secret_value())
        me = await bot.get_me()
        config_store.set_bot_username(me.username or "")

        cfg = config_store.get()
        reserved_names = {
            cfg.persona.name,
            cfg.persona.display_name,
            *cfg.persona.name_triggers,
            me.username or "",
        } - {""}

        rng = random.Random()

        if settings.admin_user_id == 0:
            logger.warning("команды владельца отключены: admin_user_id не задан")

        deps = Deps(
            settings=settings,
            config_getter=config_store.get,
            db=db,
            bot_user_id=me.id,
            reserved_names=reserved_names,
            patterns_getter=config_store.patterns,
            rng=rng,
            config_store=config_store,
            prompt_store=prompt_store,
            bot_username=me.username or "",
        )

        dispatcher = Dispatcher()
        # Роутер команд ПЕРВЫМ: иначе команды в разрешённом чате попали бы в гейт
        # основного роутера и записались бы в messages как обычный текст.
        dispatcher.include_router(build_commands_router(deps))
        dispatcher.include_router(build_router(deps))

        api_key = settings.openrouter_api_key
        if api_key is None:
            logger.warning("LLM отключён, ответы не генерируются: openrouter_api_key не задан")
        else:
            # LLMClient/Judge/Responder создаются, как только есть ключ, независимо
            # от того, заданы ли llm.main_model/llm.judge_model сейчас — оба меняются
            # на горячую через /set, и Responder/Judge сами проверяют пустую модель
            # на каждом вызове (llm:no_model, judge.check -> []), а не один раз при
            # старте процесса.
            if not cfg.llm.main_model:
                logger.warning("llm.main_model не задан: ответы не генерируются до /set")
            if not cfg.llm.judge_model:
                logger.warning("судья отключён: llm.judge_model не задан")

            http = httpx.AsyncClient(timeout=cfg.llm.timeout_sec)
            llm = LLMClient(
                api_key=api_key.get_secret_value(), cfg_getter=config_store.get, db=db, http=http
            )

            judge_prompt = settings.judge_prompt_path.read_text(encoding="utf-8")
            judge: Judge | None = Judge(llm, config_store.get, judge_prompt)

            responder = Responder(
                bot=bot,
                db=db,
                cfg_getter=config_store.get,
                llm=llm,
                judge=judge,
                patterns_getter=config_store.patterns,
                prompt_store=prompt_store,
                rng=rng,
                chat_id=settings.allowed_chat_id,
                bot_user_id=me.id,
            )
            deps.responder = responder

        retention_task = asyncio.create_task(retention_loop(db, config_store.get))
        if responder is not None:
            await responder.restore_pending()
            morning_task = asyncio.create_task(responder.morning_job())
            spontaneous_task = asyncio.create_task(responder.spontaneous_job())

        logger.info(
            "started as @%s, allowed_chat_id=%s, discovery=%s",
            me.username,
            settings.allowed_chat_id,
            settings.allowed_chat_id == 0,
        )

        await dispatcher.start_polling(bot, allowed_updates=["message", "edited_message"])
    finally:
        for task in (spontaneous_task, morning_task, retention_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if responder is not None:
            await responder.shutdown()
        if llm is not None:
            await llm.aclose()
        if bot is not None:
            # aiogram закрывает сессию сама при штатном выходе из start_polling; повторный
            # вызов идемпотентен и здесь нужен на случай исключения до start_polling.
            await bot.session.close()
        await db.close()
