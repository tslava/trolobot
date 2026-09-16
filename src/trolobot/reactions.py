"""Реакции-эмодзи на чужие сообщения, срезанные гейтом по кубику (CLAUDE.md,
"Интерфейсы: реакции").

Решение владельца: когда гейт срезает сообщение по недетерминированной причине
(``gate:dice`` или ``gate:ambient_cooldown``) — вместо полного молчания бот иногда
ставит реакцию через Bot API ``setMessageReaction``. Дёшево: без вызова модели,
без нового сообщения, эффект присутствия. Решение о реакции живёт снаружи гейта
(``gate.py`` не меняется) — этот модуль вызывается из ``bot.py`` уже после того,
как гейт вернул ``Verdict.DROP``.

``pick_reaction`` — чистая функция, как и ``should_consider`` в ``gate.py``: на входе
причина дропа, автор, снимок состояния, конфиг, ``rng`` и ``now``, на выходе — эмодзи
или ``None``. Состояние не меняет, изменения делает вызывающий (``react``) после
успешной отправки реакции.

Решение владельца 16.09.2026 (CLAUDE.md, "Интерфейсы: реакции с задержкой и смыслом"):
реакция за 0,3 секунды после сообщения выглядит механически, а выбор по кубику ставит
💩 на что угодно. Поэтому поверх того же предфильтра появились два слоя:

* ``ReactionScheduler`` — пауза ``delay_sec`` перед реакцией (человек сначала читает),
  после паузы — перепроверка тех же детерминированных условий (кулдаун, потолок, тот же
  автор), потому что за время паузы в чате могло случиться что угодно;
* ``ReactionChooser`` — дешёвый вызов модели (тот же приём, что ``judge.Judge`` и
  ``followup.FollowupChecker``): короткий промпт с контекстом чата и сообщением, ответ
  строго JSON ``{"emoji": "<один из списка>"|null}``. При ``semantic`` кубик не бросается
  вовсе — фильтром служит модель, а ``pick_reaction`` возвращает заглушку.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ReactionTypeEmoji, ReactionTypeUnion

from trolobot.config_models import Config, ReactionsConfig
from trolobot.db import Database, MessageRow
from trolobot.llm import LLMClient, LLMError
from trolobot.prompt import render_context
from trolobot.timeutil import day_key

logger = logging.getLogger(__name__)

# Свой счётчик вызовов модели на реакции: дешёвая проверка не должна съедать
# потолок вызовов основной модели (тот же приём, что followup_calls/vision_count).
_COUNTER_KEY = "reaction_calls"

# Обрезка текста сообщения в filter_log (react:declined) — как _LOG_TEXT_MAX_LEN в bot.py.
_CANDIDATE_TEXT_MAX_LEN = 200

# Обрезка обоснования модели в логе: оно может пересказывать сообщение участника.
_REASON_LOG_MAX_LEN = 100

_CONTEXT_EMPTY = "(пока не было)"

# Дублирует вырезание разделителей из sanitize.normalize_text/prompt.py/judge.py —
# на всякий случай ещё раз чистим то, что уходит в промпт выбора реакции.
_LT_RUN_RE = re.compile(r"<{3,}")
_GT_RUN_RE = re.compile(r">{3,}")

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL | re.IGNORECASE)

_SLOT_RE = re.compile(r"\{(context|name|text|emoji)\}")


def _strip_fake_delimiters(text: str) -> str:
    return _GT_RUN_RE.sub(" ", _LT_RUN_RE.sub(" ", text))


def _strip_code_fence(text: str) -> str:
    match = _CODE_FENCE_RE.match(text)
    if match is not None:
        return match.group(1).strip()
    return text


# Причины DROP гейта, на которые вместо молчания иногда ставится реакция —
# обе недетерминированные ("кости" п.11 и кулдаун ambient п.10). Все прочие причины
# (panic/stop/muted/topic/night/logistics/not_live/injection/mention_cap/ambient_cap...) —
# там решено молчать полностью, реакция не ставится никогда.
REACT_REASONS: frozenset[str] = frozenset({"gate:dice", "gate:ambient_cooldown"})


class ReactionBotLike(Protocol):
    """Узкий протокол вместо ``aiogram.Bot`` — тесты подделывают его без aiogram
    (по образцу ``responder._BotLike``).

    ``reaction`` типизирован как ``list[ReactionTypeUnion] | None`` (тип реального
    ``Bot.set_message_reaction``, не только ``ReactionTypeEmoji``): ``list`` в Python
    инвариантен по параметру, так что более узкий тип здесь не прошёл бы структурную
    проверку mypy при передаче настоящего ``aiogram.Bot`` в ``Deps.bot``.
    """

    async def set_message_reaction(
        self,
        chat_id: int,
        message_id: int,
        reaction: list[ReactionTypeUnion] | None = None,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class ReactionState:
    """Снимок состояния реакций на момент решения (по образцу ``GateState``)."""

    last_reaction_at: int | None
    last_reaction_user_id: int | None
    count_today: int


def _parse_int_state(key: str, raw: str | None) -> int | None:
    """int(raw) с дефолтом None; мусор в значении -> WARNING в лог, не падать
    (дублирует gate_state._parse_int_state — тот приватный к своему модулю)."""
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("reactions: garbage value %r for state key %r, using default", raw, key)
        return None


def pick_reaction(
    *,
    drop_reason: str,
    user_id: int,
    state: ReactionState,
    cfg: ReactionsConfig,
    rng: random.Random,
    now: int,
) -> str | None:
    """Решить, ставить ли реакцию на срезанное сообщение, и какую.

    Порядок проверок (CLAUDE.md, "Интерфейсы: реакции"): enabled -> причина ->
    потолок -> кулдаун -> тот же пользователь -> кубик. Кубик — последним, чтобы
    ``rng`` тратился только когда реакция вообще возможна: тесты с фиксированным
    seed так стабильнее (ровно как одиннадцатый шаг гейта — последний).

    При ``cfg.semantic`` (CLAUDE.md, "Интерфейсы: реакции с задержкой и смыслом")
    шаг «кубик» пропускается и возвращается заглушка ``cfg.emoji[0]``: фильтром
    служит модель (``ReactionChooser``), а не вероятность, и конкретное эмодзи
    выбирает тоже она — здесь важно лишь «реакция в принципе возможна».
    """
    if not cfg.enabled:
        return None
    if drop_reason not in REACT_REASONS:
        return None
    if state.count_today >= cfg.daily_cap:
        return None
    if state.last_reaction_at is not None and state.last_reaction_at + cfg.cooldown_min * 60 > now:
        return None
    if state.last_reaction_user_id is not None and state.last_reaction_user_id == user_id:
        return None
    if cfg.semantic:
        return cfg.emoji[0]
    if rng.random() >= cfg.probability:
        return None
    return rng.choice(cfg.emoji)


async def load_reaction_state(db: Database, tz: str, now: int) -> ReactionState:
    """Снимок state для pick_reaction. Сутки счётчика — по persona.timezone,
    как у mention_count/ambient_count (timeutil.day_key)."""
    last_reaction_at_raw = await db.get_state("last_reaction_at")
    last_reaction_user_id_raw = await db.get_state("last_reaction_user_id")
    count_key = day_key("reaction_count", now, tz)
    count_raw = await db.get_state(count_key)
    return ReactionState(
        last_reaction_at=_parse_int_state("last_reaction_at", last_reaction_at_raw),
        last_reaction_user_id=_parse_int_state("last_reaction_user_id", last_reaction_user_id_raw),
        count_today=_parse_int_state("reaction_count", count_raw) or 0,
    )


async def react(
    bot: ReactionBotLike,
    db: Database,
    *,
    chat_id: int,
    tg_message_id: int,
    user_id: int,
    emoji: str,
    tz: str,
    now: int,
) -> bool:
    """Поставить реакцию и обновить состояние. Провал (реакции запрещены в чате,
    сообщение уже удалено) -> WARNING + filter_log react:error, состояние не трогать.

    Другие исключения не ловятся — их ловит общий except хендлера в bot.py.
    """
    try:
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=tg_message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.warning("reaction failed on message %s: %s", tg_message_id, exc)
        await db.insert_filter_log(
            trigger_tg_message_id=tg_message_id,
            candidate_text=emoji,
            verdict="cut",
            stage="react",
            reason="react:error",
            shadow=False,
            created_at=now,
        )
        return False

    await db.set_state("last_reaction_at", str(now))
    await db.set_state("last_reaction_user_id", str(user_id))
    await db.increment_state(day_key("reaction_count", now, tz))
    await db.insert_filter_log(
        trigger_tg_message_id=tg_message_id,
        candidate_text=emoji,
        verdict="pass",
        stage="react",
        reason="react:sent",
        shadow=False,
        created_at=now,
    )
    logger.info("reaction %s on %s", emoji, tg_message_id)
    return True


@dataclass(frozen=True, slots=True)
class _ChoiceVerdict:
    """Разобранный ответ модели выбора реакции: ``emoji`` None — «реакция не нужна»."""

    emoji: str | None
    reason: str


def _parse_choice(raw: str) -> _ChoiceVerdict | None:
    """Разбирает ответ так же строго и терпимо к обёрткам, как ``judge._parse_verdict``:
    срез ```json``` обёрток, поиск первой "{" и ``json.JSONDecoder.raw_decode`` от неё.
    Любой сбой или неверный тип ключа -> None (значит "реакцию не ставим")."""
    try:
        candidate = _strip_code_fence(raw.strip())
        start = candidate.find("{")
        if start == -1:
            return None
        data, _end = json.JSONDecoder().raw_decode(candidate, start)
        if not isinstance(data, dict):
            return None

        emoji_value = data.get("emoji")
        if emoji_value is not None and not isinstance(emoji_value, str):
            return None

        reason_value = data.get("reason", "")
        if reason_value is None:
            reason_value = ""
        if not isinstance(reason_value, str):
            return None

        return _ChoiceVerdict(emoji=emoji_value or None, reason=reason_value)
    except Exception:
        # Ответ модели — недоверенный внешний текст: любой сбой разбора означает
        # "реакции нет", а не падение.
        return None


class ReactionChooser:
    """Дешёвый вызов модели: уместна ли реакция на это сообщение и какая.

    Тот же приём, что ``judge.Judge``/``followup.FollowupChecker``: короткий промпт
    из файла (``prompts/reaction.txt``) со слотами, данные внутри ``<<<CHAT ... >>>``,
    ответ строго JSON. Считается своим счётчиком ``reaction_calls`` с собственным
    потолком ``reactions.semantic_daily_cap`` — реакции не должны съедать бюджет
    вызовов основной модели.

    Провал (пустая модель, ``LLMError``, невалидный JSON, эмодзи не из списка) —
    ``None``: реакции просто не будет, как до этой фичи.
    """

    def __init__(
        self,
        llm: LLMClient,
        cfg_getter: Callable[[], Config],
        prompt_template: str,
    ) -> None:
        self._llm = llm
        self._cfg_getter = cfg_getter
        self._prompt_template = prompt_template

    async def choose(
        self,
        *,
        text: str,
        display_name: str,
        context_rows: Sequence[MessageRow],
        allowed: Sequence[str],
        now: int,
    ) -> str | None:
        cfg = self._cfg_getter()
        reactions_cfg = cfg.behaviour.reactions
        model = reactions_cfg.model or cfg.llm.judge_model
        if not model:
            return None
        if not allowed:
            return None

        context = _strip_fake_delimiters(render_context(list(context_rows))).strip()
        slot_values = {
            "context": context or _CONTEXT_EMPTY,
            "name": _strip_fake_delimiters(display_name).strip(),
            "text": _strip_fake_delimiters(text).strip(),
            "emoji": " ".join(allowed),
        }
        system = _SLOT_RE.sub(lambda m: slot_values[m.group(1)], self._prompt_template)
        messages = [{"role": "system", "content": system}]

        try:
            result = await self._llm.call(
                messages,
                model=model,
                max_tokens=reactions_cfg.max_tokens,
                now=now,
                counter_key=_COUNTER_KEY,
                calls_cap=reactions_cfg.semantic_daily_cap,
            )
        except LLMError as exc:
            logger.warning("reaction chooser llm error: reason=%s", exc.reason)
            return None

        verdict = _parse_choice(result.text)
        if verdict is None:
            logger.warning("reaction chooser: invalid answer")
            return None
        if verdict.emoji is None:
            logger.info("reaction chooser: none (%s)", verdict.reason[:_REASON_LOG_MAX_LEN])
            return None
        if verdict.emoji not in set(allowed):
            logger.warning("reaction chooser: emoji outside allowed list")
            return None

        # Обоснование модели может пересказывать сообщение — в лог только его начало.
        logger.info(
            "reaction chooser: %s (%s)", verdict.emoji, verdict.reason[:_REASON_LOG_MAX_LEN]
        )
        return verdict.emoji


class ReactionScheduler:
    """Ставит реакцию не сразу, а после паузы — и перепроверяет условия после неё.

    ``schedule`` не ждёт: она заводит фоновую задачу (по образцу дебаунса в
    ``responder.Responder``) и сразу возвращается, чтобы хендлер сообщения не
    задерживал обработку чата на минуты. Задача спит ``delay_sec``, перечитывает
    состояние (за паузу бот мог ответить, поставить другую реакцию или выбрать
    потолок) и только потом решает окончательно.

    Незавершённые задачи при остановке процесса просто теряются: реакция —
    необязательное украшение, восстанавливать её после рестарта незачем.
    """

    def __init__(
        self,
        *,
        bot: ReactionBotLike,
        db: Database,
        cfg_getter: Callable[[], Config],
        chooser: ReactionChooser | None,
        rng: random.Random,
        clock: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        self._bot = bot
        self._db = db
        self._cfg_getter = cfg_getter
        self._chooser = chooser
        self._rng = rng
        self._clock = clock
        self._tasks: set[asyncio.Task[None]] = set()

    def schedule(
        self,
        *,
        chat_id: int,
        tg_message_id: int,
        user_id: int,
        text: str,
        display_name: str,
    ) -> None:
        task = asyncio.ensure_future(
            self._run(
                chat_id=chat_id,
                tg_message_id=tg_message_id,
                user_id=user_id,
                text=text,
                display_name=display_name,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(
        self,
        *,
        chat_id: int,
        tg_message_id: int,
        user_id: int,
        text: str,
        display_name: str,
    ) -> None:
        try:
            await self._run_inner(
                chat_id=chat_id,
                tg_message_id=tg_message_id,
                user_id=user_id,
                text=text,
                display_name=display_name,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Реакция — украшение: её падение не должно ронять ни хендлер (он уже
            # вернулся), ни соседние задачи.
            logger.exception("reaction task failed on message %s", tg_message_id)

    async def _run_inner(
        self,
        *,
        chat_id: int,
        tg_message_id: int,
        user_id: int,
        text: str,
        display_name: str,
    ) -> None:
        lo, hi = self._cfg_getter().behaviour.reactions.delay_sec
        await asyncio.sleep(self._rng.uniform(lo, hi))

        # Конфиг перечитывается после паузы: за минуту-другую владелец мог поменять
        # что угодно через /set, а действуют текущие значения, не снятые до сна.
        cfg = self._cfg_getter()
        reactions_cfg = cfg.behaviour.reactions
        tz = cfg.persona.timezone
        now = self._clock()

        state = await load_reaction_state(self._db, tz, now)
        if not self._recheck(state, reactions_cfg, user_id=user_id, now=now):
            await self._db.insert_filter_log(
                trigger_tg_message_id=tg_message_id,
                candidate_text=None,
                verdict="cut",
                stage="react",
                reason="react:recheck",
                shadow=False,
                created_at=now,
            )
            return

        if reactions_cfg.semantic and self._chooser is not None:
            rows = await self._db.recent_messages(chat_id, reactions_cfg.context_messages + 1)
            context_rows = [row for row in rows if row.tg_message_id != tg_message_id]
            emoji = await self._chooser.choose(
                text=text,
                display_name=display_name,
                context_rows=context_rows,
                allowed=reactions_cfg.emoji,
                now=now,
            )
            if emoji is None:
                await self._db.insert_filter_log(
                    trigger_tg_message_id=tg_message_id,
                    candidate_text=text[:_CANDIDATE_TEXT_MAX_LEN],
                    verdict="cut",
                    stage="react",
                    reason="react:declined",
                    shadow=False,
                    created_at=now,
                )
                return
        else:
            fallback = self._fallback_emoji(reactions_cfg)
            if fallback is None:
                return
            emoji = fallback

        await react(
            self._bot,
            self._db,
            chat_id=chat_id,
            tg_message_id=tg_message_id,
            user_id=user_id,
            emoji=emoji,
            tz=tz,
            now=now,
        )

    @staticmethod
    def _recheck(state: ReactionState, cfg: ReactionsConfig, *, user_id: int, now: int) -> bool:
        """Те же детерминированные проверки, что в ``pick_reaction``, но уже после
        паузы и без кубика: за время сна бот мог поставить реакцию этому же человеку,
        выбрать суточный потолок или начать кулдаун."""
        if state.count_today >= cfg.daily_cap:
            return False
        if (
            state.last_reaction_at is not None
            and state.last_reaction_at + cfg.cooldown_min * 60 > now
        ):
            return False
        # Тот же человек подряд — реакция выглядела бы навязчивой.
        return state.last_reaction_user_id != user_id

    def _fallback_emoji(self, cfg: ReactionsConfig) -> str | None:
        """Эмодзи без модели. При ``semantic: false`` кубик уже бросил
        ``pick_reaction`` — остаётся только выбрать эмодзи. При ``semantic: true``
        без чузера (LLM не настроен) ``pick_reaction`` кубик пропустил, поэтому
        бросаем его здесь: иначе реакция ставилась бы на каждый подходящий срез."""
        if cfg.semantic and self._rng.random() >= cfg.probability:
            return None
        return self._rng.choice(cfg.emoji)

    async def shutdown(self) -> None:
        """Отменяет все ждущие задачи. Незавершённые реакции теряются — это нормально."""
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
