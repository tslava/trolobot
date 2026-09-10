"""Сборка процесса: настройки, конфиг, БД, бот, фоновые таски (PLAN.md, этап 1)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import sys

import httpx
from aiogram import Bot, Dispatcher

from trolobot.bot import Deps, build_router
from trolobot.config import load_config
from trolobot.config_models import Config
from trolobot.db import Database
from trolobot.few_shot import load_few_shot, render_few_shot
from trolobot.judge import Judge
from trolobot.llm import LLMClient
from trolobot.patterns import Patterns
from trolobot.responder import Responder
from trolobot.retention import retention_loop
from trolobot.settings import Settings

logger = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# Версии промпта и few-shot пока константы: таблицы версий (prompt_versions,
# few_shot_versions) появляются на этапе 6.
_PROMPT_VERSION = 1
_FEW_SHOT_VERSION = 1


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
    morning_task: asyncio.Task[None] | None = None
    spontaneous_task: asyncio.Task[None] | None = None
    llm: LLMClient | None = None
    responder: Responder | None = None
    try:
        overrides = await db.get_overrides()
        config = load_config(settings.config_path, overrides)

        # Валидация на старте: файлы должны читаться, иначе падаем сразу, а не на первом сообщении.
        few_shot_items = load_few_shot(settings.few_shot_path)
        prompt_template = settings.prompt_path.read_text(encoding="utf-8")

        holder = ConfigHolder(config)

        bot = Bot(token=settings.bot_token.get_secret_value())
        me = await bot.get_me()
        reserved_names = {
            config.persona.name,
            config.persona.display_name,
            *config.persona.name_triggers,
            me.username or "",
        } - {""}

        # TODO(этап 6): Patterns зависит от config (filters, name_triggers) и от username
        # бота — при горячей перезагрузке конфига (config_overrides) его нужно пересобирать
        # вместе с ConfigHolder.set(), иначе гейт продолжит работать по старым паттернам.
        patterns = Patterns(config.filters, config.persona.name_triggers, me.username or "")

        rng = random.Random()

        api_key = settings.openrouter_api_key
        if api_key is None:
            logger.warning("LLM отключён, ответы не генерируются: openrouter_api_key не задан")
        elif not holder.get().llm.main_model:
            logger.warning("LLM отключён, ответы не генерируются: llm.main_model не задан")
        else:
            http = httpx.AsyncClient(timeout=holder.get().llm.timeout_sec)
            llm = LLMClient(
                api_key=api_key.get_secret_value(), cfg_getter=holder.get, db=db, http=http
            )

            judge: Judge | None = None
            if holder.get().llm.judge_model:
                judge_prompt = settings.judge_prompt_path.read_text(encoding="utf-8")
                judge = Judge(llm, holder.get, judge_prompt)
            else:
                logger.warning("судья отключён: llm.judge_model не задан")

            responder = Responder(
                bot=bot,
                db=db,
                cfg_getter=holder.get,
                llm=llm,
                judge=judge,
                patterns_getter=lambda: patterns,
                prompt_template=prompt_template,
                few_shot_getter=lambda: render_few_shot(few_shot_items),
                prompt_version=_PROMPT_VERSION,
                few_shot_version=_FEW_SHOT_VERSION,
                rng=rng,
                chat_id=settings.allowed_chat_id,
                bot_user_id=me.id,
            )

        deps = Deps(
            settings=settings,
            config_getter=holder.get,
            db=db,
            bot_user_id=me.id,
            reserved_names=reserved_names,
            patterns=patterns,
            rng=rng,
            responder=responder,
        )

        dispatcher = Dispatcher()
        dispatcher.include_router(build_router(deps))

        retention_task = asyncio.create_task(retention_loop(db, holder.get))
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
