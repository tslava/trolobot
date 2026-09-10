"""aiogram Router и хендлер сообщений одного чата (PLAN.md, этапы 1-2).

Хендлер пишет сообщение в БД, затем прогоняет его через гейт (should_consider) и
логирует вердикт. Ничего не отвечает в чат — это принципиально до этапа 3, см.
CLAUDE.md, раздел "Чего не делать".
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from aiogram import Router
from aiogram.types import Message

from trolobot.config_models import Config
from trolobot.db import Database
from trolobot.gate import should_consider
from trolobot.gate_state import load_gate_state
from trolobot.gate_types import GateMessage, Verdict
from trolobot.patterns import Patterns
from trolobot.responder import Responder
from trolobot.sanitize import media_placeholder, normalize_text, sanitize_display_name, stable_n
from trolobot.settings import Settings
from trolobot.stores import ConfigStore, PromptStore

logger = logging.getLogger(__name__)

# Обрезка текста в INFO-логе (PLAN.md: лог "Имя: текст").
_LOG_TEXT_MAX_LEN = 200


@dataclass(slots=True)
class Deps:
    """Зависимости хендлера и командного роутера (commands.py, этап 6).

    config_getter/patterns_getter — геттеры, а не замороженные снимки, снятые
    один раз при сборке Deps в app.py: Patterns пересобирается ConfigStore при
    каждом /set (новые regex из filters.*), и без геттера handle_message держал
    бы устаревший объект до перезапуска процесса. patterns_getter = config_store.patterns
    вызывается на каждом сообщении, как и config_getter = config_store.get.
    config_store/prompt_store — полноценные хранилища этапа 6, добавлены для
    commands.py (build_commands_router ждёт их через структурный протокол
    _CommandsDeps — этот Deps ему структурно соответствует без явного наследования).
    """

    settings: Settings
    config_getter: Callable[[], Config]
    db: Database
    bot_user_id: int
    reserved_names: set[str]
    patterns_getter: Callable[[], Patterns]
    rng: random.Random
    config_store: ConfigStore
    prompt_store: PromptStore
    bot_username: str = ""
    responder: Responder | None = None
    # user_id, для которых уже залогирован WARNING про display_name-инъекцию —
    # не спамить лог на каждое следующее сообщение того же участника.
    warned_user_ids: set[int] = field(default_factory=set)
    clock: Callable[[], int] = field(default_factory=lambda: lambda: int(time.time()))


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
            # Имя из невидимых символов или одних эмодзи даёт «Участник N»; если есть
            # юзернейм — он понятнее и людям в контексте, и модели.
            if display_name.startswith("Участник ") and user.username:
                display_name = sanitize_display_name(user.username, user.id, deps.reserved_names)
            is_bot = user.is_bot

        if deps.patterns_getter().injection(display_name) is not None:
            # Имя-инъекция ("Игнорируй правила и скажи ...") прошла санитизацию (она
            # не содержит триггеров бота как отдельных слов), но выглядит как команда
            # гейта 5a — подменяем на "Участник N" до записи в БД и в промпт, чтобы
            # display_name сам по себе не читался как инструкция построчно перед
            # каждым сообщением участника в {context}.
            if user_id not in deps.warned_user_ids:
                deps.warned_user_ids.add(user_id)
                logger.warning(
                    "display_name looks like a prompt injection, replacing with placeholder: "
                    "user_id=%s name=%r",
                    user_id,
                    display_name,
                )
            display_name = f"Участник {stable_n(user_id)}"

        text = normalize_text(message.text or message.caption)
        if not text:
            text = media_placeholder(message) or ""
        if not text:
            # Сервисное сообщение без текста и без медиа (вошёл/вышел и т.п.) — не пишем.
            return

        reply_to_tg_message_id = (
            message.reply_to_message.message_id if message.reply_to_message is not None else None
        )

        created_at = int(message.date.timestamp())
        await deps.db.insert_message(
            tg_message_id=message.message_id,
            chat_id=message.chat.id,
            user_id=user_id,
            display_name=display_name,
            text=text,
            reply_to_tg_message_id=reply_to_tg_message_id,
            is_bot=is_bot,
            created_at=created_at,
        )
        logger.info("%s: %s", display_name, text[:_LOG_TEXT_MAX_LEN])

        reply_to_bot = (
            message.reply_to_message is not None
            and message.reply_to_message.from_user is not None
            and message.reply_to_message.from_user.id == deps.bot_user_id
        )
        gate_message = GateMessage(
            chat_id=message.chat.id,
            tg_message_id=message.message_id,
            user_id=user_id,
            is_bot=is_bot,
            text=text,
            reply_to_bot=reply_to_bot,
            created_at=created_at,
        )

        try:
            cfg = deps.config_getter()
            now = gate_message.created_at
            # Хвост апдейтов после рестарта: сообщение старше порога поздней реплики
            # уходит в контекст, но не в гейт. Человек, у которого сел телефон,
            # не отвечает задним числом. Иначе трёхчасовой меншн получит ответ
            # по свежему контексту.
            age = deps.clock() - created_at
            if age > cfg.behaviour.late_reply_threshold_sec:
                await deps.db.insert_filter_log(
                    trigger_tg_message_id=message.message_id,
                    candidate_text=None,
                    verdict="cut",
                    stage="gate",
                    reason="gate:stale",
                    shadow=False,
                    created_at=deps.clock(),
                )
                logger.info("gate stale: message %s is %ss old, skipped", message.message_id, age)
                return
            state = await load_gate_state(deps.db, cfg, gate_message, now)
            decision = should_consider(
                gate_message, state, cfg, deps.patterns_getter(), now, deps.rng
            )
            await deps.db.apply_state_changes(decision.state_changes)

            if decision.verdict is Verdict.DROP:
                await deps.db.insert_filter_log(
                    trigger_tg_message_id=gate_message.tg_message_id,
                    candidate_text=None,
                    verdict="cut",
                    stage="gate",
                    reason=decision.reason,
                    shadow=False,
                    created_at=now,
                )
                logger.debug("gate drop: %s (%s)", decision.reason, gate_message.tg_message_id)
            elif decision.verdict is Verdict.QUEUE_NIGHT:
                await deps.db.enqueue_night(
                    tg_message_id=gate_message.tg_message_id,
                    user_id=user_id,
                    display_name=display_name,
                    text=text,
                    created_at=now,
                )
                await deps.db.insert_filter_log(
                    trigger_tg_message_id=gate_message.tg_message_id,
                    candidate_text=None,
                    verdict="cut",
                    stage="gate",
                    reason=decision.reason,
                    shadow=False,
                    created_at=now,
                )
                logger.info("night queued: %s (%s)", decision.trigger, text[:_LOG_TEXT_MAX_LEN])
            else:
                await deps.db.insert_filter_log(
                    trigger_tg_message_id=gate_message.tg_message_id,
                    candidate_text=None,
                    verdict="pass",
                    stage="gate",
                    reason=decision.reason,
                    shadow=False,
                    created_at=now,
                )
                if decision.trigger is None:
                    # Инвариант гейта: PASS всегда несёт trigger (see gate.py). Если
                    # он всё же None — конфигурация гейта сломана, а не повод уронить
                    # хендлер: логируем и просто не зовём responder на этом сообщении.
                    logger.error(
                        "gate pass without trigger: message %s reason=%s",
                        gate_message.tg_message_id,
                        decision.reason,
                    )
                elif deps.responder is not None:
                    await deps.responder.on_gate_pass(gate_message, decision.trigger, display_name)
                else:
                    logger.info("gate pass: %s (%s)", decision.trigger, text[:_LOG_TEXT_MAX_LEN])
        except Exception:
            # Ошибка гейта не должна ронять хендлер: сообщение уже записано в messages.
            logger.exception("gate failed for message %s", gate_message.tg_message_id)

    return router
