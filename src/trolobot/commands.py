"""Router с командами управления ботом из телеграма (PLAN.md, этап 6).

`build_commands_router` возвращает Router с одним хендлером на все сообщения,
начинающиеся с "/". Интегратор (app.py) обязан включить этот роутер в Dispatcher
ПЕРВЫМ, перед основным роутером сообщений (trolobot.bot.build_router) — иначе
команды в разрешённом чате попадут в гейт и запишутся в messages как обычный текст.
aiogram останавливает propagation на первом хендлере, вернувшем не-None/не
skip_this_handler, так что второй раз то же сообщение до основного роутера не дойдёт.

Хендлер сам решает, кому что можно — гейт "чужой чат" из bot.py сюда не
распространяется, роутер команд стоит раньше него.

stores.py (ConfigStore/PromptStore) пишет параллельно другой агент по контракту
CLAUDE.md ("Интерфейсы этапа 6"). Чтобы не зависеть от его файла до готовности,
здесь определены структурные Protocol'ы (`_ConfigStoreLike`, `_PromptStoreLike`,
`_DbLike` и вспомогательные row-протоколы) с ровно теми методами, которые нужны
командам. Реальные классы (ConfigStore/PromptStore/Database) подойдут под них
структурно, без явного наследования.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

from aiogram import F, Router
from aiogram.types import Message, User

from trolobot.chat_memory import format_period
from trolobot.config import KeyInfo
from trolobot.config_models import Config
from trolobot.db import ChatMemoryRow, LifeEventRow
from trolobot.few_shot import FewShot
from trolobot.sanitize import normalize_text, sanitize_display_name
from trolobot.settings import Settings
from trolobot.timeutil import day_key, local_dt

if TYPE_CHECKING:
    # Только для аннотаций: "from __future__ import annotations" делает их строками,
    # так что этот импорт не исполняется в рантайме. Прямой (не TYPE_CHECKING) импорт
    # trolobot.responder сюда нежелателен — тянет aiogram-цепочку в commands.py и
    # рискует циклическим импортом (bot.py и так импортирует и responder, и commands).
    from trolobot.responder import SendOutcome

logger = logging.getLogger(__name__)

# Ограничение длины ответа владельцу (PLAN.md, этап 6: "не длиннее 3500 символов").
_MAX_REPLY_LEN = 3500
# Превью тела промпта в /prompt без "full" (CLAUDE.md, "Интерфейсы этапа 6").
_PROMPT_PREVIEW_LEN = 1500
# Подсказка в конце /get — список (не карточка одного ключа).
_GET_LIST_HINT = "* — переопределено через /set. Описание ключа: /get <ключ>"

_HELP_TEXT = (
    "Команды:\n"
    "\n"
    "В чате (всем участникам):\n"
    "/stop — тишина на 24 часа для всего чата\n"
    "/mute — без реплая: замьютить себя; реплаем на другого — только владелец\n"
    "/unmute — без реплая: снять мьют с себя; реплаем на другого — только владелец\n"
    "/ex add — реплаем на ответ бота, только владелец: добавить пару в few-shot\n"
    "\n"
    "В личке (только владелец):\n"
    "/panic — стоп навсегда, до /resume\n"
    "/resume — снять панику и /stop\n"
    "/status — краткое состояние бота\n"
    "/last [n] — последние реплики бота (по умолчанию 5, максимум 20)\n"
    "/why [hours] — сводка причин молчания за N часов (по умолчанию 1)\n"
    "/get [ключ|префикс] — параметры конфига; точный ключ — описание, тип и диапазон\n"
    "/set <ключ> <значение> — изменить параметр без рестарта; списки — в YAML: [a, b]\n"
    "/unset <ключ> — сбросить параметр к дефолту из yaml\n"
    "/prompt [full] — текущий системный промпт\n"
    "/rollback <версия> — откат промпта на версию\n"
    "/ex last [n] — последние примеры few-shot\n"
    "/ex rm [n] — удалить n-й пример few-shot с конца\n"
    "/life <текст> — новость о себе: запомнить и сразу рассказать в чате\n"
    "/life list | rm N | post N — события: список, удалить, повторить\n"
    "/say <текст> — сказать в чат дословно\n"
    "/memory [list|rm N|run] — долгая память чата: пересказы по неделям\n"
    "/help — эта справка"
)

# Общий текст использования — и для голого /life, и для /life rm|post без валидного N.
_LIFE_USAGE = "Использование: /life <текст> | list | rm N | post N"

# /memory без подкоманды = /memory list; остальное — подсказка при кривом вводе.
_MEMORY_USAGE = "Использование: /memory [list | rm N | run]"
# Сколько пересказов показывать в /memory list (ответ всё равно режется до 3500 символов).
_MEMORY_LIST_LIMIT = 20
# Сколько символов свежесозданных пересказов показать в ответе на /memory run.
_MEMORY_RUN_PREVIEW_LEN = 1500


class _MessageRowLike(Protocol):
    """Подмножество db.MessageRow, нужное /ex add.

    Члены объявлены через ``@property`` (а не как обычные атрибуты), потому что
    db.MessageRow — frozen dataclass: его поля read-only, а обычный атрибут в
    Protocol требует settable-совместимости (инвариантно в обе стороны) и не
    матчится на read-only поле. ``@property`` в Protocol — это ровно read-only
    член, который frozen dataclass удовлетворяет структурно.
    """

    @property
    def display_name(self) -> str | None: ...
    @property
    def text(self) -> str | None: ...


class _BotReplyRowLike(Protocol):
    """Подмножество db.BotReplyRow, нужное /ex add и /last. См. _MessageRowLike про @property."""

    @property
    def trigger(self) -> str: ...
    @property
    def trigger_tg_message_id(self) -> int | None: ...
    @property
    def text(self) -> str: ...
    @property
    def prompt_version(self) -> int: ...
    @property
    def few_shot_version(self) -> int: ...
    @property
    def delay_sec(self) -> int: ...
    @property
    def created_at(self) -> int: ...


class _VersionRowLike(Protocol):
    """Подмножество db.VersionRow, нужное /prompt (посчитать "всего M"). См. _MessageRowLike."""

    @property
    def version(self) -> int: ...


class _DbLike(Protocol):
    """Методы Database, которые использует commands.py.

    Часть из них (add_mute/remove_mute/last_bot_replies/audit_stop/
    message_by_tg_id/bot_reply_by_tg_id и версии промпта) пишет параллельно
    другой агент — здесь только сигнатуры из контракта CLAUDE.md, без импорта db.py.
    """

    async def get_state(self, key: str) -> str | None: ...
    async def set_state(self, key: str, value: str) -> None: ...
    async def delete_state(self, key: str) -> None: ...
    async def audit_stop(
        self, key: str, changed_by: int, now: int, new_value: str | None = None
    ) -> None: ...
    async def add_mute(self, user_id: int, display_name: str, muted_by: int, now: int) -> None: ...
    async def remove_mute(self, user_id: int) -> bool: ...
    # Sequence (не list) в возвращаемом типе: list инвариантен по своему параметру,
    # так что list[_BotReplyRowLike] не принял бы Database.last_bot_replies(), чей
    # реальный тип — list[BotReplyRow] (BotReplyRow — подтип _BotReplyRowLike, но
    # List[BotReplyRow] всё равно не подтип List[_BotReplyRowLike]). Sequence
    # объявлен ковариантным по элементу — этой проблемы не создаёт.
    async def last_bot_replies(self, n: int) -> Sequence[_BotReplyRowLike]: ...
    async def message_by_tg_id(
        self, chat_id: int, tg_message_id: int
    ) -> _MessageRowLike | None: ...
    async def bot_reply_by_tg_id(self, tg_message_id: int) -> _BotReplyRowLike | None: ...
    async def filter_log_summary(self, since: int) -> list[tuple[str, int]]: ...
    async def load_pending(self) -> Sequence[object]: ...
    async def night_unanswered(self) -> Sequence[object]: ...
    async def prompt_versions(self) -> Sequence[_VersionRowLike]: ...
    # -- события жизни (/life, CLAUDE.md "события жизни и /say") -------------
    async def insert_life_event(self, *, text: str, created_at: int) -> int: ...
    async def life_events(self) -> Sequence[LifeEventRow]: ...
    async def life_event(self, event_id: int) -> LifeEventRow | None: ...
    async def delete_life_event(self, event_id: int) -> bool: ...
    # -- долгая память чата (/memory, CLAUDE.md "долгая память чата") --------
    async def chat_memories(self, limit: int) -> Sequence[ChatMemoryRow]: ...
    async def chat_memory(self, memory_id: int) -> ChatMemoryRow | None: ...
    async def chat_memory_count(self) -> int: ...
    async def delete_chat_memory(self, memory_id: int) -> bool: ...
    async def last_chat_memory_end(self) -> int | None: ...


class _ResponderLike(Protocol):
    """Подмножество Responder, нужное /life и /say (CLAUDE.md, «события жизни»).

    Только эти два метода — не полный Responder. SendOutcome в аннотациях
    приходит из TYPE_CHECKING-импорта выше (см. его комментарий про то, почему
    commands.py не импортирует trolobot.responder напрямую)."""

    async def announce_life(self, event: LifeEventRow) -> SendOutcome: ...
    async def say(self, text: str) -> SendOutcome: ...


class _ChatMemorizerLike(Protocol):
    """Подмножество chat_memory.ChatMemorizer, нужное /memory run.

    Только ``run_due`` — список и удаление идут прямо через БД. None означает,
    что LLM-часть выключена (нет ключа): тогда /memory run отвечает об этом и
    ничего не запускает."""

    async def run_due(self, *, now: int) -> Sequence[ChatMemoryRow]: ...


class _ConfigStoreLike(Protocol):
    """Методы stores.ConfigStore, которые использует commands.py."""

    def get(self) -> Config: ...
    async def set(
        self, key: str, raw_value: str, changed_by: int, now: int
    ) -> tuple[str | None, str]: ...
    async def unset(self, key: str, changed_by: int, now: int) -> str | None: ...
    def flat(self) -> list[tuple[str, str, bool]]: ...
    def describe(self, key: str) -> KeyInfo | None: ...


class _PromptStoreLike(Protocol):
    """Методы stores.PromptStore, которые использует commands.py."""

    def system_prompt(self) -> str: ...
    def prompt_version(self) -> int: ...
    def few_shot_version(self) -> int: ...
    async def rollback_prompt(self, version: int) -> bool: ...
    async def add_example(self, name: str, user: str, text: str, now: int) -> int: ...
    async def remove_example(self, index: int, now: int) -> int: ...
    def examples(self) -> list[FewShot]: ...


class _CommandsDeps(Protocol):
    """Зависимости build_commands_router. bot.Deps потом дополнят этими полями.

    responder используется /life и /say (announce_life/say) через _ResponderLike —
    структурный протокол, а не прямой тип Responder, чтобы commands.py не тянул
    responder.py (и вместе с ним aiogram-цепочку) в рантайме. responder может быть
    None (LLM-часть выключена, CLAUDE.md "Settings.openrouter_api_key None").

    Члены объявлены через ``@property``, а не как обычные атрибуты: commands.py
    только читает deps.* и никогда не присваивает — по PEP 544 обычный (settable)
    атрибут Protocol требует инвариантного совпадения типа в обе стороны, а
    ``@property`` — только совместимости на чтение (ковариантно). bot.Deps —
    обычный (не frozen) dataclass с более конкретными типами полей (ConfigStore,
    PromptStore, Database, Responder | None), и без ``@property`` он не матчился
    бы структурно на эти протокольные типы.
    """

    @property
    def settings(self) -> Settings: ...
    @property
    def config_store(self) -> _ConfigStoreLike: ...
    @property
    def prompt_store(self) -> _PromptStoreLike: ...
    @property
    def db(self) -> _DbLike: ...
    @property
    def responder(self) -> _ResponderLike | None: ...
    @property
    def bot_user_id(self) -> int: ...
    @property
    def bot_username(self) -> str: ...
    @property
    def sticker_catalog_enabled(self) -> int: ...
    @property
    def memorizer(self) -> _ChatMemorizerLike | None: ...


def _truncate(text: str) -> str:
    if len(text) <= _MAX_REPLY_LEN:
        return text
    return text[: _MAX_REPLY_LEN - 1] + "…"


async def _reply(message: Message, text: str) -> None:
    await message.answer(_truncate(text), parse_mode=None)


def _is_owner(user: User | None, deps: _CommandsDeps) -> bool:
    return (
        user is not None
        and deps.settings.admin_user_id != 0
        and user.id == deps.settings.admin_user_id
    )


def _resolve_mention_target(message: Message) -> tuple[int, str] | None:
    """Цель /mute или /unmute: реплай или text_mention-упоминание в entities.

    Обычный @username (entity type "mention") не поддерживается — username
    в БД не хранится, найти по нему пользователя нечем, игнорируем молча.
    """
    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None:
        return reply.from_user.id, reply.from_user.full_name
    for entity in message.entities or ():
        if entity.type == "text_mention" and entity.user is not None:
            return entity.user.id, entity.user.full_name
    return None


async def _cmd_stop(message: Message, deps: _CommandsDeps, now: int, user: User | None) -> None:
    changed_by = user.id if user is not None else 0
    await deps.db.set_state("stop_until", str(now + 86400))
    await deps.db.audit_stop("stop", changed_by, now)
    logger.info("stop: silenced until %s by user_id=%s", now + 86400, changed_by)


async def _cmd_mute(message: Message, deps: _CommandsDeps, now: int, user: User | None) -> None:
    """Без реплая/упоминания — мьютит самого отправителя, доступно всем. Реплаем
    или упоминанием на ЧУЖОЕ сообщение — только владелец (иначе это готовый
    инструмент травли: «тебя теперь бот не видит»). Цель-владелец или цель-бот —
    молча ничего, кем бы ни была вызвана команда."""
    if user is None:
        return
    target = _resolve_mention_target(message)
    if target is None:
        target_id, target_name = user.id, user.full_name
    else:
        target_id, target_name = target
        if target_id != user.id and not _is_owner(user, deps):
            return
    if target_id == deps.settings.admin_user_id or target_id == deps.bot_user_id:
        return
    display_name = sanitize_display_name(target_name, target_id, set())
    await deps.db.add_mute(target_id, display_name, user.id, now)
    await deps.db.audit_stop(f"mute:{target_id}", user.id, now)
    logger.info("mute: user_id=%s by user_id=%s", target_id, user.id)


async def _cmd_unmute(message: Message, deps: _CommandsDeps, now: int, user: User | None) -> None:
    """Без реплая/упоминания — снимает мьют с самого отправителя, доступно всем.
    Реплаем или упоминанием на чужого — только владелец."""
    if user is None:
        return
    target = _resolve_mention_target(message)
    target_id = user.id if target is None else target[0]
    if target is not None and target_id != user.id and not _is_owner(user, deps):
        return
    await deps.db.remove_mute(target_id)
    await deps.db.audit_stop(f"unmute:{target_id}", user.id, now)
    logger.info("unmute: user_id=%s by user_id=%s", target_id, user.id)


async def _cmd_ex_add(message: Message, deps: _CommandsDeps, now: int) -> None:
    reply = message.reply_to_message
    if reply is None or reply.from_user is None or reply.from_user.id != deps.bot_user_id:
        return
    bot_reply = await deps.db.bot_reply_by_tg_id(reply.message_id)
    if bot_reply is None:
        return

    trigger_msg = None
    if bot_reply.trigger_tg_message_id is not None:
        trigger_msg = await deps.db.message_by_tg_id(
            message.chat.id, bot_reply.trigger_tg_message_id
        )

    if trigger_msg is not None and trigger_msg.display_name and trigger_msg.text:
        name, user_text = trigger_msg.display_name, trigger_msg.text
    else:
        name, user_text = "Чат", "(без обращения)"

    await deps.prompt_store.add_example(name=name, user=user_text, text=bot_reply.text, now=now)
    logger.info("ex add: %r -> %r", user_text, bot_reply.text)


async def _cmd_panic(message: Message, deps: _CommandsDeps, now: int, user: User) -> None:
    await deps.db.set_state("panic", "1")
    await deps.db.audit_stop("panic", user.id, now)
    logger.info("panic set")
    await _reply(message, "Паника. Бот молчит до /resume.")


async def _cmd_resume(message: Message, deps: _CommandsDeps, now: int, user: User) -> None:
    """Снимает panic, stop_until и предохранитель LLM (llm_circuit_until/
    llm_error_streak). Действия безусловны (как раньше для panic/stop_until), но
    в ответе перечисляется только то, что реально было выставлено — иначе
    владелец решит, что бот стоял на паузе, которой не было."""
    panic_was_set = await deps.db.get_state("panic") is not None
    stop_was_set = await deps.db.get_state("stop_until") is not None
    streak_before = await deps.db.get_state("llm_error_streak")
    circuit_was_open = await deps.db.get_state("llm_circuit_until") is not None

    await deps.db.delete_state("panic")
    await deps.db.delete_state("stop_until")
    await deps.db.delete_state("llm_circuit_until")
    await deps.db.set_state("llm_error_streak", "0")
    await deps.db.audit_stop("resume", user.id, now)
    logger.info(
        "resume: panic=%s stop=%s circuit=%s", panic_was_set, stop_was_set, circuit_was_open
    )

    cleared: list[str] = []
    if panic_was_set:
        cleared.append("panic")
    if stop_was_set:
        cleared.append("stop")
    if circuit_was_open:
        streak = streak_before if streak_before is not None else "?"
        cleared.append(f"предохранитель LLM (было {streak} ошибок подряд)")

    text = "Продолжаем."
    if cleared:
        text += f" Снято: {', '.join(cleared)}."
    await _reply(message, text)


async def _cmd_status(message: Message, deps: _CommandsDeps, now: int) -> None:
    cfg = deps.config_store.get()
    tz = cfg.persona.timezone

    panic = await deps.db.get_state("panic") == "1"
    stop_until_raw = await deps.db.get_state("stop_until")
    stop_until = (
        local_dt(int(stop_until_raw), tz).strftime("%H:%M %d.%m") if stop_until_raw else "нет"
    )

    circuit_until_raw = await deps.db.get_state("llm_circuit_until")
    circuit_state = "закрыт"
    if circuit_until_raw is not None:
        circuit_until = int(circuit_until_raw)
        if circuit_until > now:
            circuit_state = f"открыт до {local_dt(circuit_until, tz).strftime('%H:%M')}"
    llm_error_streak = await deps.db.get_state("llm_error_streak") or "0"

    ambient = int(await deps.db.get_state(day_key("ambient_count", now, tz)) or "0")
    mention = int(await deps.db.get_state(day_key("mention_count", now, tz)) or "0")
    llm_calls = int(await deps.db.get_state(day_key("llm_calls", now, tz)) or "0")
    llm_spent = float(await deps.db.get_state(day_key("llm_spent_usd", now, tz)) or "0")
    reactions = int(await deps.db.get_state(day_key("reaction_count", now, tz)) or "0")
    stickers = int(await deps.db.get_state(day_key("sticker_count", now, tz)) or "0")
    followup_calls = int(await deps.db.get_state(day_key("followup_calls", now, tz)) or "0")
    vision_count = int(await deps.db.get_state(day_key("vision_count", now, tz)) or "0")

    pending = len(await deps.db.load_pending())
    night_queue = len(await deps.db.night_unanswered())

    life_events = await deps.db.life_events()
    life_unsent = sum(1 for event in life_events if event.announced_at is None)

    memory_total = await deps.db.chat_memory_count()
    memory_end = await deps.db.last_chat_memory_end()
    memory_line = (
        f"chat memory: {memory_total}, последний до {local_dt(memory_end, tz).strftime('%d.%m.%Y')}"
        if memory_end is not None
        else "chat memory: нет"
    )

    hot_until_raw = await deps.db.get_state("hot_until")
    try:
        hot_until = int(hot_until_raw) if hot_until_raw is not None else None
    except ValueError:
        hot_until = None
    if hot_until is not None and hot_until > now:
        hot_ambient_count = int(await deps.db.get_state("hot_ambient_count") or "0")
        hot_line = (
            f"hot window: до {local_dt(hot_until, tz).strftime('%H:%M')} "
            f"({hot_ambient_count}/{cfg.behaviour.hot_window.ambient_cap})"
        )
    else:
        hot_line = "hot window: нет"

    checkin_due_raw = await deps.db.get_state("checkin_due")
    try:
        checkin_due = int(checkin_due_raw) if checkin_due_raw is not None else None
    except ValueError:
        checkin_due = None
    checkin_line = (
        f"checkin: due {local_dt(checkin_due, tz).strftime('%H:%M')}"
        if checkin_due is not None
        else "checkin: нет"
    )

    lines = [
        f"Паника: {'да' if panic else 'нет'}",
        f"Стоп до: {stop_until}",
        f"Предохранитель LLM: {circuit_state}, ошибок подряд: {llm_error_streak}",
        f"Промпт: v{deps.prompt_store.prompt_version()}, "
        f"few-shot: v{deps.prompt_store.few_shot_version()}",
        f"Модели: main={cfg.llm.main_model or '-'}, judge={cfg.llm.judge_model or '-'}",
        f"Shadow: {'да' if cfg.filters.shadow else 'нет'}",
        f"Сегодня: ambient={ambient}, mention={mention}, "
        f"llm_calls={llm_calls}, llm_spent=${llm_spent:.2f}, "
        f"reactions={reactions}/{cfg.behaviour.reactions.daily_cap}, "
        f"stickers={stickers}/{cfg.behaviour.stickers.daily_cap} "
        f"({deps.sticker_catalog_enabled} в каталоге)",
        f"Pending: {pending}",
        f"Night queue: {night_queue}",
        f"life events: {len(life_events)} ({life_unsent})",
        memory_line,
        hot_line,
        f"followup calls: {followup_calls}/{cfg.behaviour.followup.daily_cap}",
        f"vision: {vision_count}/{cfg.behaviour.vision.daily_cap}",
        checkin_line,
    ]
    await _reply(message, "\n".join(lines))


async def _cmd_last(message: Message, deps: _CommandsDeps, args: list[str]) -> None:
    n = 5
    if args:
        try:
            n = int(args[0])
        except ValueError:
            n = 5
    n = max(1, min(n, 20))

    tz = deps.config_store.get().persona.timezone
    rows = await deps.db.last_bot_replies(n)
    lines = [
        f"{local_dt(row.created_at, tz).strftime('%H:%M %d.%m')} | {row.trigger} | "
        f"p{row.prompt_version}/f{row.few_shot_version} | +{row.delay_sec}s | {row.text}"
        for row in rows
    ]
    await _reply(message, "\n".join(lines) if lines else "Пусто.")


async def _cmd_why(message: Message, deps: _CommandsDeps, now: int, args: list[str]) -> None:
    hours = 1
    if args:
        try:
            hours = int(args[0])
        except ValueError:
            hours = 1
    hours = max(1, min(hours, 168))

    summary = await deps.db.filter_log_summary(now - hours * 3600)
    lines = [f"{reason} {count}" for reason, count in summary]
    await _reply(message, "\n".join(lines) if lines else "Пусто.")


def _render_key_card(info: KeyInfo) -> str:
    lines = [f"{info.key}: {info.value}"]
    type_line = f"тип: {info.type_name}, {info.bounds}" if info.bounds else f"тип: {info.type_name}"
    lines.append(type_line)
    if info.description:
        lines.append(info.description)
    if info.overridden:
        lines.append(f"переопределён, в yaml: {info.default}")
    if not info.settable:
        lines.append("меняется только в config.yaml")
    return "\n".join(lines)


async def _cmd_get(message: Message, deps: _CommandsDeps, args: list[str]) -> None:
    arg = args[0] if args else ""
    if arg:
        info = deps.config_store.describe(arg)
        if info is not None:
            await _reply(message, _render_key_card(info))
            return

    lines = [
        f"{key}{'*' if overridden else ''}: {value}"
        for key, value, overridden in deps.config_store.flat()
        if key.startswith(arg)
    ]
    if not lines:
        await _reply(message, "Пусто.")
        return
    lines.append(_GET_LIST_HINT)
    await _reply(message, "\n".join(lines))


async def _cmd_set(
    message: Message, deps: _CommandsDeps, now: int, admin_user_id: int, args: list[str]
) -> None:
    if len(args) < 2:
        await _reply(
            message, "Использование: /set <ключ> <значение>. Ключи: /get, описание: /get <ключ>"
        )
        return
    key, value = args[0], " ".join(args[1:])
    old, new = await deps.config_store.set(key, value, admin_user_id, now)
    await _reply(message, f"{key}: {old} → {new}")


async def _cmd_unset(
    message: Message, deps: _CommandsDeps, now: int, admin_user_id: int, args: list[str]
) -> None:
    if not args:
        await _reply(message, "Использование: /unset <ключ>")
        return
    key = args[0]
    default = await deps.config_store.unset(key, admin_user_id, now)
    if default is None:
        await _reply(message, f"{key}: не был переопределён")
    else:
        await _reply(message, f"{key}: сброшен к {default}")


async def _cmd_prompt(message: Message, deps: _CommandsDeps, args: list[str]) -> None:
    full = bool(args) and args[0].lower() == "full"
    body = deps.prompt_store.system_prompt()
    total = len(await deps.db.prompt_versions())
    header = f"версия {deps.prompt_store.prompt_version()} (активная), всего {total}"

    if full:
        text = f"{header}\n\n{body}"
    else:
        preview = body[:_PROMPT_PREVIEW_LEN]
        if len(body) > _PROMPT_PREVIEW_LEN:
            preview += "…"
        text = f"{header}\n\n{preview}"
    await _reply(message, text)


async def _cmd_rollback(message: Message, deps: _CommandsDeps, args: list[str]) -> None:
    if not args:
        await _reply(message, "Использование: /rollback <версия>")
        return
    try:
        version = int(args[0])
    except ValueError:
        await _reply(message, f"Ошибка: {args[0]!r} не похоже на номер версии")
        return
    if await deps.prompt_store.rollback_prompt(version):
        await _reply(message, f"промпт: версия {version}")
    else:
        await _reply(message, f"Ошибка: версия {version} не найдена")


async def _cmd_ex_last(message: Message, deps: _CommandsDeps, args: list[str]) -> None:
    n = 5
    if len(args) > 1:
        try:
            n = int(args[1])
        except ValueError:
            n = 5
    n = max(1, n)

    lines = []
    for example in deps.prompt_store.examples()[-n:]:
        payload = json.dumps(
            {"speak": example.speak, "text": example.text},
            ensure_ascii=False,
            separators=(", ", ": "),
        )
        lines.append(f"{example.name}: {example.user} → {payload}")
    await _reply(message, "\n".join(lines) if lines else "Пусто.")


async def _cmd_ex_rm(message: Message, deps: _CommandsDeps, now: int, args: list[str]) -> None:
    n = 1
    if len(args) > 1:
        try:
            n = int(args[1])
        except ValueError:
            n = 1
    n = max(1, n)
    version = await deps.prompt_store.remove_example(n, now)
    await _reply(message, f"few-shot: версия {version} (удалён пример {n} с конца)")


def _parse_int(raw: str) -> int | None:
    try:
        return int(raw)
    except ValueError:
        return None


def _command_arg_text(raw_text: str, tokens: list[str]) -> str:
    """Текст после команды, с исходными пробелами/переносами внутри (для /say —
    "дословно"), только внешние пробелы срезаны. args (tokens.split()) для этого
    не годится — схлопывает внутренние пробелы."""
    if not tokens:
        return ""
    return raw_text[len(tokens[0]) :].strip()


def _is_blocked(outcome: SendOutcome) -> bool:
    return outcome.reason in ("blocked:panic", "blocked:stop")


def _life_outcome_text(outcome: SendOutcome) -> str:
    """Общая часть ответа /life <текст> и /life post N после вызова announce_life."""
    if outcome.sent:
        return f"Отправлено: {outcome.text}"
    if _is_blocked(outcome):
        return "Бот молчит (panic/stop) — /resume."
    text = f"Не отправлено: {outcome.reason}"
    if outcome.text:
        text += f"\nКандидат: {outcome.text}"
    return text


async def _cmd_life(
    message: Message,
    deps: _CommandsDeps,
    now: int,
    admin_user_id: int,
    args: list[str],
    raw_text: str,
    tokens: list[str],
) -> None:
    sub = args[0].lower() if args else ""
    if sub == "list":
        await _cmd_life_list(message, deps)
        return
    if sub == "rm":
        await _cmd_life_rm(message, deps, now, admin_user_id, args)
        return
    if sub == "post":
        await _cmd_life_post(message, deps, now, admin_user_id, args)
        return

    text = normalize_text(_command_arg_text(raw_text, tokens))
    if not text:
        await _reply(message, _LIFE_USAGE)
        return

    event_id = await deps.db.insert_life_event(text=text, created_at=now)
    await deps.db.audit_stop("life:add", admin_user_id, now, text)
    logger.info("life add: #%s %r", event_id, text)

    if deps.responder is None:
        await _reply(message, f"Записал #{event_id}. LLM не настроен, в чат не отправлено.")
        return

    event = LifeEventRow(
        id=event_id, text=text, created_at=now, announced_at=None, announced_tg_message_id=None
    )
    outcome = await deps.responder.announce_life(event)
    await _reply(message, f"Записал #{event_id}. {_life_outcome_text(outcome)}")


async def _cmd_life_list(message: Message, deps: _CommandsDeps) -> None:
    tz = deps.config_store.get().persona.timezone
    events = await deps.db.life_events()
    if not events:
        await _reply(message, "Событий нет.")
        return
    lines = [
        f"#{event.id} {local_dt(event.created_at, tz).strftime('%d.%m.%Y')} "
        f"{'✓' if event.announced_at is not None else '—'} {event.text}"
        for event in events
    ]
    await _reply(message, "\n".join(lines))


async def _cmd_life_rm(
    message: Message, deps: _CommandsDeps, now: int, admin_user_id: int, args: list[str]
) -> None:
    event_id = _parse_int(args[1]) if len(args) > 1 else None
    if event_id is None:
        await _reply(message, _LIFE_USAGE)
        return
    event = await deps.db.life_event(event_id)
    if event is None:
        await _reply(message, f"Нет события #{event_id}.")
        return
    await deps.db.delete_life_event(event_id)
    await deps.db.audit_stop("life:rm", admin_user_id, now, event.text)
    logger.info("life rm: #%s %r", event_id, event.text)
    await _reply(message, f"Событие #{event_id} удалено.")


async def _cmd_life_post(
    message: Message, deps: _CommandsDeps, now: int, admin_user_id: int, args: list[str]
) -> None:
    event_id = _parse_int(args[1]) if len(args) > 1 else None
    if event_id is None:
        await _reply(message, _LIFE_USAGE)
        return
    event = await deps.db.life_event(event_id)
    if event is None:
        await _reply(message, f"Нет события #{event_id}.")
        return
    await deps.db.audit_stop("life:post", admin_user_id, now, event.text)
    logger.info("life post: #%s %r", event_id, event.text)

    if deps.responder is None:
        await _reply(message, "LLM не настроен, в чат не отправлено.")
        return

    outcome = await deps.responder.announce_life(event)
    await _reply(message, _life_outcome_text(outcome))


async def _cmd_memory(
    message: Message,
    deps: _CommandsDeps,
    now: int,
    admin_user_id: int,
    args: list[str],
) -> None:
    """/memory [list|rm N|run] — долгая память чата (CLAUDE.md, "долгая память чата").

    Голое /memory — это /memory list: смотреть, что бот о вас помнит, владелец
    будет чаще, чем что-то менять.
    """
    sub = args[0].lower() if args else "list"
    if sub == "list":
        await _cmd_memory_list(message, deps)
        return
    if sub == "rm":
        await _cmd_memory_rm(message, deps, now, admin_user_id, args)
        return
    if sub == "run":
        await _cmd_memory_run(message, deps, now, admin_user_id)
        return
    await _reply(message, _MEMORY_USAGE)


async def _cmd_memory_list(message: Message, deps: _CommandsDeps) -> None:
    tz = deps.config_store.get().persona.timezone
    rows = await deps.db.chat_memories(_MEMORY_LIST_LIMIT)
    if not rows:
        await _reply(message, "Памяти пока нет.")
        return
    blocks = [
        f"#{row.id} {format_period(row.period_start, row.period_end, tz)}\n{row.text}"
        for row in rows
    ]
    await _reply(message, "\n\n".join(blocks))


async def _cmd_memory_rm(
    message: Message, deps: _CommandsDeps, now: int, admin_user_id: int, args: list[str]
) -> None:
    memory_id = _parse_int(args[1]) if len(args) > 1 else None
    if memory_id is None:
        await _reply(message, _MEMORY_USAGE)
        return
    row = await deps.db.chat_memory(memory_id)
    if row is None:
        await _reply(message, f"Нет пересказа #{memory_id}.")
        return
    await deps.db.delete_chat_memory(memory_id)
    await deps.db.audit_stop("memory:rm", admin_user_id, now, row.text)
    logger.info("memory rm: #%s, символов %d", memory_id, len(row.text))
    await _reply(message, f"Пересказ #{memory_id} удалён.")


async def _cmd_memory_run(
    message: Message, deps: _CommandsDeps, now: int, admin_user_id: int
) -> None:
    """Принудительный прогон, вне run_window — чтобы не ждать до ночи."""
    if deps.memorizer is None:
        await _reply(message, "LLM-часть выключена, пересказы недоступны.")
        return
    await deps.db.audit_stop("memory:run", admin_user_id, now)
    tz = deps.config_store.get().persona.timezone
    created = await deps.memorizer.run_due(now=now)
    logger.info("memory run: добавлено %d", len(created))
    if not created:
        await _reply(message, "Нечего пересказывать.")
        return
    blocks = [
        f"#{row.id} {format_period(row.period_start, row.period_end, tz)}\n{row.text}"
        for row in created
    ]
    preview = "\n\n".join(blocks)[:_MEMORY_RUN_PREVIEW_LEN]
    await _reply(message, f"Добавлено пересказов: {len(created)}\n\n{preview}")


async def _cmd_say(
    message: Message,
    deps: _CommandsDeps,
    now: int,
    admin_user_id: int,
    raw_text: str,
    tokens: list[str],
) -> None:
    text = _command_arg_text(raw_text, tokens)
    if not text:
        await _reply(message, "Использование: /say <текст>")
        return

    await deps.db.audit_stop("say", admin_user_id, now, text)
    logger.info("say: %r", text)

    if deps.responder is None:
        await _reply(message, "LLM-часть выключена, /say недоступен.")
        return

    outcome = await deps.responder.say(text)
    if outcome.sent:
        await _reply(message, "Отправлено.")
    else:
        await _reply(message, "Бот молчит (panic/stop) — /resume.")


def build_commands_router(deps: _CommandsDeps) -> Router:
    """Собирает Router с одним хендлером-диспетчером команд.

    Должен быть включён в Dispatcher ПЕРВЫМ, перед trolobot.bot.build_router —
    команды не должны попадать в гейт и в лог/БД основного хендлера. aiogram
    сам останавливает распространение сообщения после первого хендлера, чья
    функция вернулась без исключения, так что явного "cancel propagation" не
    требуется — важен только порядок регистрации роутеров у интегратора.
    """
    router = Router(name=__name__)

    @router.message((F.text & F.text.startswith("/")) | (F.caption & F.caption.startswith("/")))
    async def handle_command(message: Message) -> None:
        text = message.text or message.caption or ""
        tokens = text.split()
        if not tokens:
            return
        cmd_token = tokens[0][1:]
        if "@" in cmd_token:
            cmd_part, _, suffix = cmd_token.partition("@")
            # Суффикс "@другой_бот" в групповых чатах Telegram рассылает команду
            # всем ботам сразу — исполняем только адресованную нам (без учёта
            # регистра); иначе, например, /stop@weatherbot остановил бы и нас.
            if suffix.lower() != deps.bot_username.lower():
                return
            cmd = cmd_part.lower()
        else:
            cmd = cmd_token.lower()
        args = tokens[1:]

        user = message.from_user
        now = int(message.date.timestamp())

        is_chat = message.chat.id == deps.settings.allowed_chat_id
        is_owner_private = (
            message.chat.type == "private"
            and deps.settings.admin_user_id != 0
            and user is not None
            and user.id == deps.settings.admin_user_id
        )
        if not is_chat and not is_owner_private:
            return

        try:
            if is_chat:
                if cmd == "stop":
                    await _cmd_stop(message, deps, now, user)
                elif cmd == "mute":
                    await _cmd_mute(message, deps, now, user)
                elif cmd == "unmute":
                    await _cmd_unmute(message, deps, now, user)
                elif cmd == "ex" and args and args[0].lower() == "add" and _is_owner(user, deps):
                    await _cmd_ex_add(message, deps, now)
                # Неизвестная/чужая команда в чате — молчание, без лога и без ответа.
            else:
                assert user is not None  # гарантировано is_owner_private выше
                admin_user_id = deps.settings.admin_user_id
                if cmd == "panic":
                    await _cmd_panic(message, deps, now, user)
                elif cmd == "resume":
                    await _cmd_resume(message, deps, now, user)
                elif cmd == "status":
                    await _cmd_status(message, deps, now)
                elif cmd == "last":
                    await _cmd_last(message, deps, args)
                elif cmd == "why":
                    await _cmd_why(message, deps, now, args)
                elif cmd == "get":
                    await _cmd_get(message, deps, args)
                elif cmd == "set":
                    await _cmd_set(message, deps, now, admin_user_id, args)
                elif cmd == "unset":
                    await _cmd_unset(message, deps, now, admin_user_id, args)
                elif cmd == "prompt":
                    await _cmd_prompt(message, deps, args)
                elif cmd == "rollback":
                    await _cmd_rollback(message, deps, args)
                elif cmd == "ex" and args and args[0].lower() == "last":
                    await _cmd_ex_last(message, deps, args)
                elif cmd == "ex" and args and args[0].lower() == "rm":
                    await _cmd_ex_rm(message, deps, now, args)
                elif cmd == "life":
                    await _cmd_life(message, deps, now, admin_user_id, args, text, tokens)
                elif cmd == "say":
                    await _cmd_say(message, deps, now, admin_user_id, text, tokens)
                elif cmd == "memory":
                    await _cmd_memory(message, deps, now, admin_user_id, args)
                elif cmd == "help":
                    await _reply(message, _HELP_TEXT)
                else:
                    await _reply(message, _HELP_TEXT)
        except Exception as exc:
            logger.exception("command /%s failed", cmd)
            if is_owner_private:
                await _reply(message, f"Ошибка: {exc}")

    return router
