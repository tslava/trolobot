"""aiogram Router и хендлер сообщений одного чата (PLAN.md, этапы 1-2).

Хендлер пишет сообщение в БД, затем прогоняет его через гейт (should_consider) и
логирует вердикт. Ничего не отвечает в чат — это принципиально до этапа 3, см.
CLAUDE.md, раздел "Чего не делать".
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import BinaryIO, Protocol

from aiogram import Router
from aiogram.exceptions import AiogramError
from aiogram.types import Message

from trolobot.config_models import Config
from trolobot.db import Database
from trolobot.followup import FollowupChecker
from trolobot.gate import should_consider
from trolobot.gate_state import load_gate_state
from trolobot.gate_types import GateMessage, Trigger, Verdict
from trolobot.patterns import Patterns
from trolobot.reactions import (
    REACT_REASONS,
    ReactionBotLike,
    load_reaction_state,
    pick_reaction,
    react,
)
from trolobot.responder import Responder
from trolobot.sanitize import media_placeholder, normalize_text, sanitize_display_name, stable_n
from trolobot.settings import Settings
from trolobot.stores import ConfigStore, PromptStore
from trolobot.timeutil import day_key
from trolobot.vision import (
    PhotoSizeLike,
    VisionDescriber,
    photo_text,
    pick_photo_size,
    should_describe,
)

logger = logging.getLogger(__name__)

# Обрезка текста в INFO-логе (PLAN.md: лог "Имя: текст").
_LOG_TEXT_MAX_LEN = 200

# Причины DROP, на которых имеет смысл дешёвая проверка followup (CLAUDE.md,
# "внимание как у живого человека") — все они означают "сообщение без обращения,
# которое обычный гейт не признал ambient-репликой". gate:dice здесь — тот же
# кубик, что и без горячего окна (шаг 11/10h), не оставлять без шанса на followup
# только потому, что кубик выпал не в пользу ambient-ответа.
FOLLOWUP_REASONS: frozenset[str] = frozenset(
    {"gate:dice", "gate:not_live", "gate:ambient_cap", "gate:ambient_cooldown", "gate:hot_cap"}
)


class BotLike(ReactionBotLike, Protocol):
    """Узкий протокол вместо ``aiogram.Bot`` для всего, что хендлер делает с ботом:
    реакции (``set_message_reaction``, унаследован от ``ReactionBotLike``) и скачивание
    фото для зрения (``download``). Тесты подделывают его без aiogram.

    Сигнатура ``download`` — подмножество ``aiogram.Bot.download``
    (``file: str | Downloadable``, ``destination: BinaryIO | Path | str | None``,
    плюс ``timeout``/``chunk_size``/``seek`` со значениями по умолчанию): протоколу
    достаточно того, что нужно здесь (file_id строкой, ответ в память), а настоящий
    ``Bot`` принимает больше и потому структурно подходит.
    """

    async def download(self, file: str, destination: BinaryIO | None = None) -> BinaryIO | None: ...


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
    # Дешёвая проверка «это мне?» для сообщений без обращения в горячем окне
    # (CLAUDE.md, "внимание как у живого человека"). None — LLM не настроен, PASS
    # по FOLLOWUP_REASONS не проверяется вовсе, поведение как без followup.
    followup: FollowupChecker | None = None
    # Узкий протокол (set_message_reaction + download) вместо aiogram.Bot — для
    # реакций (reactions.py) и для скачивания фото зрением (vision.py). None в
    # discovery/тестах без реального бота — тогда реакция просто не ставится, фото
    # не описывается, а сообщение всё равно пишется и гейтится как обычно.
    bot: BotLike | None = None
    # Описание фото моделью со зрением (CLAUDE.md, "зрение на фото"). None — LLM не
    # настроен, фото остаётся плейсхолдером "[фото]", как до этой фичи.
    vision: VisionDescriber | None = None
    # Сколько enabled-стикеров в каталоге (stickers.yaml) — считается один раз в
    # app.py при старте, только для /status ("(N в каталоге)"). Каталог не меняется
    # на горячую (в отличие от config_overrides), поэтому фиксированное число, а не
    # геттер, достаточно.
    sticker_catalog_enabled: int = 0
    # user_id, для которых уже залогирован WARNING про display_name-инъекцию —
    # не спамить лог на каждое следующее сообщение того же участника.
    warned_user_ids: set[int] = field(default_factory=set)
    clock: Callable[[], int] = field(default_factory=lambda: lambda: int(time.time()))


def _int_state(raw: str | None) -> int | None:
    """int(raw) с дефолтом None; мусор в значении -> WARNING, не падать."""
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("bot: garbage value %r in state, using default", raw)
        return None


async def _describe_photo(
    deps: Deps,
    describer: VisionDescriber,
    bot: BotLike,
    *,
    sizes: Sequence[PhotoSizeLike],
    caption: str,
    reply_to_bot: bool,
    tg_message_id: int,
    now: int,
) -> str:
    """Текст, который ляжет в ``messages.text`` вместо фото (CLAUDE.md, "зрение на фото").

    Зовётся ДО ``insert_message``, чтобы описание попало и в БД, и в контекст, и в
    гейт — дальше фото ничем не отличается от обычного текста. Любой отказ (кубик,
    потолок, ошибка скачивания, ошибка модели) заканчивается привычным «[фото]»:
    фото не должно ломать обработку сообщения.
    """
    cfg = deps.config_getter()
    vision_cfg = cfg.behaviour.vision
    patterns = deps.patterns_getter()

    addressed = (
        reply_to_bot or patterns.mentions_bot(caption) or patterns.name_trigger(caption) is not None
    )
    hot_until = _int_state(await deps.db.get_state("hot_until"))
    hot = hot_until is not None and hot_until > now

    count_key = day_key("vision_count", now, cfg.persona.timezone)
    count_today = _int_state(await deps.db.get_state(count_key)) or 0

    if not should_describe(
        addressed=addressed, hot=hot, count_today=count_today, cfg=vision_cfg, rng=deps.rng
    ):
        await deps.db.insert_filter_log(
            trigger_tg_message_id=tg_message_id,
            candidate_text=None,
            verdict="cut",
            stage="vision",
            reason="vision:skipped",
            shadow=False,
            created_at=now,
        )
        return photo_text(None, caption)

    description: str | None = None
    size = pick_photo_size(sizes, vision_cfg.max_width)
    if size is not None:
        image: bytes | None = None
        try:
            downloaded = await bot.download(size.file_id)
            if downloaded is None:
                logger.warning("vision: bot.download вернул пусто для сообщения %s", tg_message_id)
            else:
                image = downloaded.read()
        except (AiogramError, OSError) as exc:
            # Фото удалено, файл слишком большой, сеть отвалилась — не повод ронять
            # хендлер: сообщение всё равно запишется как "[фото]".
            logger.warning("vision: не удалось скачать фото сообщения %s: %s", tg_message_id, exc)
        if image is not None:
            # mime фиксирован: телеграм отдаёт превью message.photo всегда как JPEG.
            description = await describer.describe(
                image, mime="image/jpeg", caption=caption, now=now
            )

    if description is None:
        await deps.db.insert_filter_log(
            trigger_tg_message_id=tg_message_id,
            candidate_text=None,
            verdict="cut",
            stage="vision",
            reason="vision:failed",
            shadow=False,
            created_at=now,
        )
        return photo_text(None, caption)

    # Счётчик /status (статистика описаний). Потолок вызовов держит LLMClient по
    # своему счётчику vision_calls — неудачная попытка тратит его, но не эту
    # статистику, небольшое расхождение допустимо и заложено контрактом.
    await deps.db.increment_state(count_key)
    await deps.db.insert_filter_log(
        trigger_tg_message_id=tg_message_id,
        candidate_text=description,
        verdict="pass",
        stage="vision",
        reason="vision:described",
        shadow=False,
        created_at=now,
    )
    logger.info("vision: %s", description)
    return photo_text(description, caption)


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

        reply_to_tg_message_id = (
            message.reply_to_message.message_id if message.reply_to_message is not None else None
        )
        reply_to_bot = (
            message.reply_to_message is not None
            and message.reply_to_message.from_user is not None
            and message.reply_to_message.from_user.id == deps.bot_user_id
        )
        created_at = int(message.date.timestamp())

        text = normalize_text(message.text or message.caption)
        photo_sizes = list(message.photo or ())
        if (
            photo_sizes
            and deps.vision is not None
            and deps.bot is not None
            and deps.config_getter().behaviour.vision.enabled
        ):
            # Зрение (CLAUDE.md, "зрение на фото"): описание заменяет плейсхолдер и
            # уходит в БД вместе с подписью — строго до insert_message.
            text = await _describe_photo(
                deps,
                deps.vision,
                deps.bot,
                sizes=photo_sizes,
                caption=text,
                reply_to_bot=reply_to_bot,
                tg_message_id=message.message_id,
                now=created_at,
            )
        if not text:
            text = media_placeholder(message) or ""
        if not text:
            # Сервисное сообщение без текста и без медиа (вошёл/вышел и т.п.) — не пишем.
            return

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
                if (
                    decision.reason in FOLLOWUP_REASONS
                    and deps.followup is not None
                    and cfg.behaviour.followup.enabled
                    and state.hot_until is not None
                    and now < state.hot_until
                ):
                    # Телефон в руках (CLAUDE.md, "внимание как у живого человека"):
                    # горячее окно ещё открыто, а сообщение без обращения гейт не
                    # признал ambient-репликой — дешёвая проверка решает, не
                    # адресовано ли оно боту всё же. context_rows без текущего
                    # сообщения (оно уже записано insert_message выше).
                    followup_cfg = cfg.behaviour.followup
                    context_rows = await deps.db.recent_messages(
                        gate_message.chat_id, followup_cfg.context_messages + 1
                    )
                    context_rows = [
                        row
                        for row in context_rows
                        if row.tg_message_id != gate_message.tg_message_id
                    ]
                    recent_replies = await deps.db.recent_bot_replies(followup_cfg.recent_replies)
                    addressed = await deps.followup.check(
                        text=text,
                        display_name=display_name,
                        context_rows=context_rows,
                        recent_replies=recent_replies,
                        now=now,
                    )
                    if addressed:
                        await deps.db.insert_filter_log(
                            trigger_tg_message_id=gate_message.tg_message_id,
                            candidate_text=text,
                            verdict="pass",
                            stage="followup",
                            reason="followup:yes",
                            shadow=False,
                            created_at=now,
                        )
                        logger.info("followup pass: %s", text[:_LOG_TEXT_MAX_LEN])
                        if deps.responder is not None:
                            await deps.responder.on_gate_pass(
                                gate_message, Trigger.FOLLOWUP, display_name
                            )
                        return
                    await deps.db.insert_filter_log(
                        trigger_tg_message_id=gate_message.tg_message_id,
                        candidate_text=None,
                        verdict="cut",
                        stage="followup",
                        reason="followup:no",
                        shadow=False,
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
                logger.debug("gate drop: %s (%s)", decision.reason, gate_message.tg_message_id)
                if decision.reason in REACT_REASONS and deps.bot is not None:
                    # Реакция вместо полного молчания — только на недетерминированные
                    # причины (gate:dice/gate:ambient_cooldown), только в разрешённом
                    # чате (эта ветка недостижима в discovery mode и для чужого чата —
                    # см. проверки выше). Решение живёт снаружи гейта, gate.py не меняется.
                    tz = cfg.persona.timezone
                    rstate = await load_reaction_state(deps.db, tz, now)
                    emoji = pick_reaction(
                        drop_reason=decision.reason,
                        user_id=user_id,
                        state=rstate,
                        cfg=cfg.behaviour.reactions,
                        rng=deps.rng,
                        now=now,
                    )
                    if emoji is not None:
                        await react(
                            deps.bot,
                            deps.db,
                            chat_id=gate_message.chat_id,
                            tg_message_id=gate_message.tg_message_id,
                            user_id=user_id,
                            emoji=emoji,
                            tz=tz,
                            now=now,
                        )
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
