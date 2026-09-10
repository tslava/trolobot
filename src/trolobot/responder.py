"""Responder — оркестратор генерации и отправки (PLAN.md, этап 3).

Единственный модуль, который зовёт ``bot.send_message``/``bot.send_chat_action``.
Всё остальное (гейт, БД, LLM, промпт, фильтр) уже готово — этот модуль их склеивает:

- дебаунс входящих PASS (один ``asyncio.Task`` на чат, таймер сбрасывается каждым
  новым PASS в окне ``debounce_sec``);
- отложенный ответ на обращения через ``pending_replies`` (таймер — только
  будильник в памяти, данные переживают рестарт);
- перегенерация текста строго после задержки, на свежем контексте;
- утренний джоб и джоб «просто так».

``Bot`` из aiogram инжектируется как объект, реализующий ``_BotLike`` — узкий
протокол с ``send_message``/``send_chat_action`` — чтобы тесты этого модуля не
тянули aiogram и подделывали бота простым классом. По той же причине ``clock``
и ``sleep`` инжектируются (по умолчанию — реальные время и ``asyncio.sleep``):
тесты подменяют их на управляемые фейки и не ждут реальных секунд.

Особый случай: при рестарте процесса ``restore_pending`` восстанавливает
отложенные ответы только из ``PendingRow`` (без текста и без display_name —
их в БД для pending_replies нет). Если такое восстановленное ожидание попадает
в ночное окно, в ``night_queue`` уходит запись с ``text=""`` и
``display_name="Участник"`` — заглушка, а не настоящее сообщение. Для pending,
поставленных в течение текущего процесса (не после рестарта), текст и имя
есть в памяти (``_pending_info``) и используются как обычно.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

from trolobot import filters
from trolobot.config_models import Config
from trolobot.db import Database, PendingRow
from trolobot.delays import debounce_seconds, fast_delay, pick_delay
from trolobot.filters import FilterContext
from trolobot.gate_state import load_gate_state
from trolobot.gate_types import GateMessage, Trigger
from trolobot.llm import LLMClient, LLMError
from trolobot.patterns import Patterns
from trolobot.prompt import (
    SITUATION_LATE,
    SITUATION_MORNING,
    SITUATION_SPONTANEOUS,
    build_messages,
    parse_reply,
    render_context,
)
from trolobot.timeutil import day_key, in_window, local_date, parse_hhmm, seconds_until, week_key

logger = logging.getLogger(__name__)

# Приоритет триггера обращения при схлопывании дебаунс-буфера: reply > mention > name > ambient
# (CLAUDE.md, "Интерфейсы этапа 3").
_TRIGGER_PRIORITY: dict[Trigger, int] = {
    Trigger.REPLY: 3,
    Trigger.MENTION: 2,
    Trigger.NAME: 1,
    Trigger.AMBIENT: 0,
}

_ADDRESS_TRIGGER_VALUES = (Trigger.MENTION.value, Trigger.REPLY.value, Trigger.NAME.value)

_TYPING_ACTION = "typing"
_TYPING_STEP_SEC = 4.0
_CHARS_PER_SEC = 15.0
_SECONDS_PER_HOUR = 3600

# Ambient и spontaneous делят один и тот же дневной бюджет (ambient_count/last_ambient_at,
# CLAUDE.md, GateState.ambient_count_today) и поэтому перепроверяются одинаково в _respond_inner.
_AMBIENT_LIKE_TRIGGER_VALUES = (Trigger.AMBIENT.value, "spontaneous")

# Пауза после упавшей итерации фонового цикла (morning_job/spontaneous_job), чтобы не уйти
# в busy-loop, если ошибка повторяется на каждом заходе (по образцу retention_loop).
_ERROR_RETRY_SEC = 60.0


class _SentMessageLike(Protocol):
    @property
    def message_id(self) -> int: ...


class _BotLike(Protocol):
    """Узкий протокол вместо ``aiogram.Bot`` — тесты подделывают его без aiogram."""

    async def send_message(
        self, chat_id: int, text: str, *, reply_to_message_id: int | None = None
    ) -> _SentMessageLike: ...

    async def send_chat_action(self, chat_id: int, action: str) -> object: ...


def _trigger_value(trigger: Trigger | str) -> str:
    return trigger.value if isinstance(trigger, Trigger) else str(trigger)


def _window_hours(window: tuple[str, str]) -> float:
    start = parse_hhmm(window[0])
    end = parse_hhmm(window[1])
    start_sec = start.hour * _SECONDS_PER_HOUR + start.minute * 60
    end_sec = end.hour * _SECONDS_PER_HOUR + end.minute * 60
    if end_sec <= start_sec:
        end_sec += 24 * _SECONDS_PER_HOUR
    return (end_sec - start_sec) / _SECONDS_PER_HOUR


class Responder:
    def __init__(
        self,
        *,
        bot: _BotLike,
        db: Database,
        cfg_getter: Callable[[], Config],
        llm: LLMClient,
        patterns_getter: Callable[[], Patterns],
        prompt_template: str,
        few_shot_getter: Callable[[], str],
        prompt_version: int,
        few_shot_version: int,
        rng: random.Random,
        chat_id: int,
        bot_user_id: int,
        clock: Callable[[], int] = lambda: int(time.time()),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.bot = bot
        self.db = db
        self.cfg_getter = cfg_getter
        self.llm = llm
        self.patterns_getter = patterns_getter
        self.prompt_template = prompt_template
        self.few_shot_getter = few_shot_getter
        self.prompt_version = prompt_version
        self.few_shot_version = few_shot_version
        self.rng = rng
        self.chat_id = chat_id
        self.bot_user_id = bot_user_id
        self._clock = clock
        self.sleep = sleep

        self._debounce_buffer: list[tuple[Trigger, GateMessage, str]] = []
        self._debounce_task: asyncio.Task[None] | None = None
        self._pending_tasks: dict[int, asyncio.Task[None]] = {}
        # pending_id -> (text, display_name); только для pending, поставленных в этом
        # процессе. После рестарта заполняется по мере схлопывания новых обращений.
        self._pending_info: dict[int, tuple[str, str]] = {}
        # Сериализует генерацию+отправку+счётчики одного Responder: без этого два PASS,
        # ждущих LLM параллельно, могли бы оба проскочить одну и ту же проверку бюджета.
        self._respond_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Приём PASS от гейта: дебаунс, схлопывание, постановка pending.
    # ------------------------------------------------------------------ #

    async def on_gate_pass(self, msg: GateMessage, trigger: Trigger, display_name: str) -> None:
        self._debounce_buffer.append((trigger, msg, display_name))

        if self._debounce_task is not None:
            if not self._debounce_task.done():
                self._debounce_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._debounce_task
            else:
                # Таск уже завершён (обычным путём, отмена в _debounce_fire уже
                # проглочена) — на всякий случай забираем исключение, если оно всё же
                # есть, чтобы asyncio не пожаловался на "Task exception was never
                # retrieved".
                with contextlib.suppress(asyncio.CancelledError):
                    self._debounce_task.exception()

        cfg = self.cfg_getter()
        wait = debounce_seconds(cfg.behaviour, self.rng)
        self._debounce_task = asyncio.ensure_future(self._debounce_fire(wait))

    async def _debounce_fire(self, wait: float) -> None:
        try:
            await self.sleep(wait)
            buffer = self._debounce_buffer
            self._debounce_buffer = []
            if not buffer:
                return
            trigger, msg, display_name = self._pick_strongest(buffer)
            await self._handle_debounced(trigger, msg, display_name)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("debounce fire failed")

    def _pick_strongest(
        self, buffer: list[tuple[Trigger, GateMessage, str]]
    ) -> tuple[Trigger, GateMessage, str]:
        best = max(_TRIGGER_PRIORITY[item[0]] for item in buffer)
        candidates = [item for item in buffer if _TRIGGER_PRIORITY[item[0]] == best]
        return candidates[-1]

    async def _handle_debounced(
        self, trigger: Trigger, msg: GateMessage, display_name: str
    ) -> None:
        try:
            await self._handle_debounced_inner(trigger, msg, display_name)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "handle debounced failed: trigger=%s trigger_msg_id=%s", trigger, msg.tg_message_id
            )

    async def _handle_debounced_inner(
        self, trigger: Trigger, msg: GateMessage, display_name: str
    ) -> None:
        if trigger is Trigger.AMBIENT:
            await self._respond(
                trigger=Trigger.AMBIENT,
                trigger_msg_id=msg.tg_message_id,
                user_id=msg.user_id,
                situation="",
                delay_sec=0,
            )
            return

        cfg = self.cfg_getter()
        now = self._clock()
        pending_rows = await self.db.load_pending()
        if pending_rows:
            pending = pending_rows[0]
            new_due = now + fast_delay(cfg.behaviour, self.rng)
            await self.db.update_pending_due(pending.id, new_due)
            self._pending_info[pending.id] = (msg.text, display_name)
            row = PendingRow(
                id=pending.id,
                trigger_tg_message_id=pending.trigger_tg_message_id,
                user_id=pending.user_id,
                trigger=pending.trigger,
                due_at=new_due,
                created_at=pending.created_at,
                done_at=None,
            )
            self._schedule_pending_timer(row, new_due)
        else:
            urgent = self.patterns_getter().urgent(msg.text)
            due = now + pick_delay(cfg.behaviour, self.rng, urgent=urgent)
            pending_id = await self.db.insert_pending(
                trigger_tg_message_id=msg.tg_message_id,
                user_id=msg.user_id,
                trigger=trigger.value,
                due_at=due,
                created_at=now,
            )
            self._pending_info[pending_id] = (msg.text, display_name)
            row = PendingRow(
                id=pending_id,
                trigger_tg_message_id=msg.tg_message_id,
                user_id=msg.user_id,
                trigger=trigger.value,
                due_at=due,
                created_at=now,
                done_at=None,
            )
            self._schedule_pending_timer(row, due)

    # ------------------------------------------------------------------ #
    # Таймер и срабатывание pending.
    # ------------------------------------------------------------------ #

    def _schedule_pending_timer(self, row: PendingRow, due_at: int) -> None:
        existing = self._pending_tasks.get(row.id)
        if existing is not None and not existing.done():
            existing.cancel()
        self._pending_tasks[row.id] = asyncio.ensure_future(
            self._pending_wait_and_fire(row, due_at)
        )

    async def _pending_wait_and_fire(self, row: PendingRow, due_at: int) -> None:
        try:
            wait = max(0.0, due_at - self._clock())
            await self.sleep(wait)
            fresh = PendingRow(
                id=row.id,
                trigger_tg_message_id=row.trigger_tg_message_id,
                user_id=row.user_id,
                trigger=row.trigger,
                due_at=due_at,
                created_at=row.created_at,
                done_at=None,
            )
            await self._fire_pending(fresh)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("pending wait and fire failed: pending_id=%s", row.id)

    async def _fire_pending(self, row: PendingRow) -> None:
        try:
            await self._fire_pending_inner(row)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("fire pending failed: pending_id=%s", row.id)

    async def _fire_pending_inner(self, row: PendingRow) -> None:
        self._pending_tasks.pop(row.id, None)
        cfg = self.cfg_getter()
        tz = cfg.persona.timezone
        now = self._clock()

        if in_window(now, tz, cfg.behaviour.quiet_window):
            text, display_name = self._pending_info.pop(row.id, ("", "Участник"))
            if not text:
                logger.warning(
                    "night queue: pending %s restored without original text (restart), "
                    "writing placeholder for display_name=%r",
                    row.id,
                    display_name,
                )
            await self.db.enqueue_night(
                tg_message_id=row.trigger_tg_message_id,
                user_id=row.user_id,
                display_name=display_name,
                text=text,
                created_at=row.created_at,
            )
            await self.db.mark_pending_done(row.id, now)
            await self.db.insert_filter_log(
                trigger_tg_message_id=row.trigger_tg_message_id,
                candidate_text=None,
                verdict="cut",
                stage="send",
                reason="send:night",
                shadow=False,
                created_at=now,
            )
            return

        recheck_reason = await self._recheck(row, now, cfg)
        if recheck_reason is not None:
            self._pending_info.pop(row.id, None)
            await self.db.mark_pending_done(row.id, now)
            await self.db.insert_filter_log(
                trigger_tg_message_id=row.trigger_tg_message_id,
                candidate_text=None,
                verdict="cut",
                stage="send",
                reason=recheck_reason,
                shadow=False,
                created_at=now,
            )
            return

        self._pending_info.pop(row.id, None)
        await self.db.mark_pending_done(row.id, now)
        delay_sec = now - row.created_at
        situation = SITUATION_LATE if delay_sec > cfg.behaviour.late_reply_threshold_sec else ""
        await self._respond(
            trigger=row.trigger,
            trigger_msg_id=row.trigger_tg_message_id,
            user_id=row.user_id,
            situation=situation,
            delay_sec=delay_sec,
        )

    async def _recheck(self, row: PendingRow, now: int, cfg: Config) -> str | None:
        """Только детерминированные шаги гейта (п.1-5, п.7 и лимиты обращений).

        Кубик (п.11) и живой разговор (п.9) намеренно не перепроверяются — иначе
        перепроверка срезала бы большинство уже одобренных ambient-реплик.

        mention_chat_cooldown/mention_user_cooldown (гейт, шаг 6) тоже намеренно не
        перепроверяются здесь: схлопывание в _handle_debounced гарантирует, что на
        чат в любой момент живёт не более одного pending-обращения (второй PASS
        сдвигает due_at того же pending, а не создаёт второй), а last_mention_reply_at
        меняет только этот же _respond после отправки — то есть пока этот pending
        не сработал, ни last_mention_reply_at, ни last_mention_reply_at:<user> измениться
        не могли.
        """
        synthetic = GateMessage(
            chat_id=self.chat_id,
            tg_message_id=row.trigger_tg_message_id,
            user_id=row.user_id,
            is_bot=False,
            text="",
            reply_to_bot=False,
            created_at=now,
        )
        state = await load_gate_state(self.db, cfg, synthetic, now)
        if state.panic:
            return "send:recheck_panic"
        if state.stop_until is not None and state.stop_until > now:
            return "send:recheck_stop"
        if row.user_id in state.muted_user_ids:
            return "send:recheck_muted"
        if state.topic_cooldown_until is not None and state.topic_cooldown_until > now:
            return "send:recheck_topic"
        if state.mention_count_today >= cfg.behaviour.mention_daily_cap:
            return "send:recheck_mention_cap"
        return None

    # ------------------------------------------------------------------ #
    # Генерация и отправка.
    # ------------------------------------------------------------------ #

    async def _respond(
        self,
        *,
        trigger: Trigger | str,
        trigger_msg_id: int | None,
        user_id: int | None,
        situation: str,
        delay_sec: int,
    ) -> None:
        try:
            await self._respond_inner(
                trigger=trigger,
                trigger_msg_id=trigger_msg_id,
                user_id=user_id,
                situation=situation,
                delay_sec=delay_sec,
            )
        except LLMError as exc:
            await self.db.insert_filter_log(
                trigger_tg_message_id=trigger_msg_id,
                candidate_text=None,
                verdict="cut",
                stage="llm",
                reason=exc.reason,
                shadow=False,
                created_at=self._clock(),
            )
        except Exception:
            logger.exception(
                "responder failed: trigger=%s trigger_msg_id=%s", trigger, trigger_msg_id
            )

    async def _respond_inner(
        self,
        *,
        trigger: Trigger | str,
        trigger_msg_id: int | None,
        user_id: int | None,
        situation: str,
        delay_sec: int,
    ) -> None:
        # Один Responder генерирует и отправляет строго по одному ответу за раз: без
        # этого лока два PASS, ждущих LLM параллельно, могли бы оба проскочить одну и
        # ту же проверку бюджета (ambient_count/last_ambient_at), пока оба ждут ответа
        # модели, и оба отправиться.
        async with self._respond_lock:
            cfg = self.cfg_getter()
            tz = cfg.persona.timezone
            trigger_value = _trigger_value(trigger)
            now = self._clock()

            if trigger_value in _AMBIENT_LIKE_TRIGGER_VALUES:
                # Пока этот ответ ждал лок, другой мог уже уйти и обновить
                # ambient_count/last_ambient_at — перечитываем их с нуля вместо того,
                # чтобы полагаться на состояние, увиденное до захвата лока.
                recheck_reason = await self._recheck_ambient_budget(cfg, now)
                if recheck_reason is not None:
                    await self.db.insert_filter_log(
                        trigger_tg_message_id=trigger_msg_id,
                        candidate_text=None,
                        verdict="cut",
                        stage="send",
                        reason=recheck_reason,
                        shadow=False,
                        created_at=now,
                    )
                    return

            await self._generate_and_send(
                cfg=cfg,
                tz=tz,
                trigger_value=trigger_value,
                trigger_msg_id=trigger_msg_id,
                user_id=user_id,
                situation=situation,
                delay_sec=delay_sec,
                now=now,
            )

    async def _recheck_ambient_budget(self, cfg: Config, now: int) -> str | None:
        """Свежая проверка ambient/spontaneous-бюджета после захвата _respond_lock.

        Использует те же поля, что и гейт (шаг 10: GateState.ambient_count_today /
        last_ambient_at), но читает их напрямую из state, а не из снимка, снятого до
        ожидания лока — он мог устареть, пока этот ответ ждал своей очереди.
        """
        tz = cfg.persona.timezone
        ambient_count_raw = await self.db.get_state(day_key("ambient_count", now, tz))
        ambient_count = int(ambient_count_raw) if ambient_count_raw is not None else 0
        if ambient_count >= cfg.behaviour.daily_cap:
            return "send:recheck_ambient_cap"

        last_ambient_raw = await self.db.get_state("last_ambient_at")
        last_ambient_at = int(last_ambient_raw) if last_ambient_raw is not None else None
        if (
            last_ambient_at is not None
            and last_ambient_at + cfg.behaviour.chat_cooldown_min * 60 > now
        ):
            return "send:recheck_ambient_cooldown"
        return None

    async def _generate_and_send(
        self,
        *,
        cfg: Config,
        tz: str,
        trigger_value: str,
        trigger_msg_id: int | None,
        user_id: int | None,
        situation: str,
        delay_sec: int,
        now: int,
    ) -> None:
        context_rows = await self.db.recent_messages(self.chat_id, cfg.behaviour.context_window)
        recent_replies_list = await self.db.recent_bot_replies(cfg.behaviour.recent_replies_memory)
        context = render_context(context_rows)
        recent_replies = "\n".join(recent_replies_list)
        few_shot = self.few_shot_getter()
        age = cfg.persona.age(local_date(now, tz))

        messages = build_messages(
            self.prompt_template,
            age=age,
            few_shot=few_shot,
            context=context,
            recent_replies=recent_replies,
            places="",
            situation=situation,
        )

        result = await self.llm.call(
            messages, model=cfg.llm.main_model, max_tokens=cfg.llm.max_tokens, now=now
        )

        reply = parse_reply(result.text)
        if reply is None:
            await self.db.insert_filter_log(
                trigger_tg_message_id=trigger_msg_id,
                candidate_text=None,
                verdict="cut",
                stage="llm",
                reason="llm:invalid_json",
                shadow=False,
                created_at=now,
            )
            return

        if not reply.speak:
            await self.db.insert_filter_log(
                trigger_tg_message_id=trigger_msg_id,
                candidate_text=None,
                verdict="cut",
                stage="llm",
                reason="llm:silent",
                shadow=False,
                created_at=now,
            )
            return

        filter_ctx = FilterContext(
            cfg=cfg, recent_replies=recent_replies_list, context_rows=context_rows, places_names=[]
        )
        verdict = await filters.check_output(reply.text, filter_ctx)
        if not verdict.ok:
            shadow = cfg.filters.shadow
            stage = verdict.reason.split(":", 1)[0] if verdict.reason else "filter"
            await self.db.insert_filter_log(
                trigger_tg_message_id=trigger_msg_id,
                candidate_text=reply.text,
                verdict="cut",
                stage=stage,
                reason=verdict.reason,
                shadow=shadow,
                created_at=now,
            )
            if not shadow:
                return

        is_address = trigger_value in _ADDRESS_TRIGGER_VALUES
        reply_to_message_id: int | None = None
        if is_address and trigger_msg_id is not None:
            after_count = await self.db.messages_after(self.chat_id, trigger_msg_id)
            if after_count > 0 or delay_sec > cfg.behaviour.reply_as_reply_after_sec:
                reply_to_message_id = trigger_msg_id

        await self._run_typing(reply.text)

        sent = await self.bot.send_message(
            self.chat_id, reply.text, reply_to_message_id=reply_to_message_id
        )

        await self.db.insert_bot_reply(
            tg_message_id=sent.message_id,
            reply_to_tg_message_id=reply_to_message_id,
            trigger=trigger_value,
            trigger_tg_message_id=trigger_msg_id,
            text=reply.text,
            prompt_version=self.prompt_version,
            few_shot_version=self.few_shot_version,
            delay_sec=delay_sec,
            created_at=now,
        )

        if is_address:
            await self.db.increment_state(day_key("mention_count", now, tz))
            await self.db.set_state("last_mention_reply_at", str(now))
            if user_id is not None:
                await self.db.set_state(f"last_mention_reply_at:{user_id}", str(now))
        elif trigger_value == Trigger.AMBIENT.value:
            await self.db.increment_state(day_key("ambient_count", now, tz))
            await self.db.set_state("last_ambient_at", str(now))
        elif trigger_value == "spontaneous":
            await self.db.increment_state(day_key("ambient_count", now, tz))
            await self.db.set_state("last_ambient_at", str(now))
            await self.db.increment_state(week_key("spontaneous_count", now, tz))
        # morning: счётчики не меняются — не входит в бюджет обращений.

        await self.db.insert_filter_log(
            trigger_tg_message_id=trigger_msg_id,
            candidate_text=reply.text,
            verdict="pass",
            stage="send",
            reason=f"send:{trigger_value}",
            shadow=False,
            created_at=now,
        )

    async def _run_typing(self, text: str) -> None:
        duration = len(text) / _CHARS_PER_SEC
        elapsed = 0.0
        while elapsed < duration:
            await self.bot.send_chat_action(self.chat_id, _TYPING_ACTION)
            step = min(_TYPING_STEP_SEC, duration - elapsed)
            await self.sleep(step)
            elapsed += step

    # ------------------------------------------------------------------ #
    # Рестарт, утро, «просто так», остановка.
    # ------------------------------------------------------------------ #

    async def restore_pending(self) -> None:
        cfg = self.cfg_getter()
        now = self._clock()
        rows = await self.db.load_pending()
        for row in rows:
            if row.due_at >= now - cfg.behaviour.late_reply_threshold_sec:
                self._schedule_pending_timer(row, row.due_at)
            else:
                await self.db.mark_pending_done(row.id, now)
                await self.db.insert_filter_log(
                    trigger_tg_message_id=row.trigger_tg_message_id,
                    candidate_text=None,
                    verdict="cut",
                    stage="send",
                    reason="send:restart",
                    shadow=False,
                    created_at=now,
                )

    async def morning_job(self) -> None:
        """Бесконечный цикл: каждый заход спит до случайного момента внутри
        ``morning_reply_window``, отвечает на ночные сообщения, затем спит до начала
        следующего окна. По образцу ``retention.retention_loop``: падение одной
        итерации логируется и не останавливает цикл — после ошибки цикл спит
        ``_ERROR_RETRY_SEC`` (инжектированным ``sleep``), чтобы не уйти в busy-loop,
        и на следующем заходе пересчитывает окно заново."""
        while True:
            try:
                await self._morning_iteration()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("morning_job: iteration failed")
                await self.sleep(_ERROR_RETRY_SEC)

    async def _morning_iteration(self) -> None:
        cfg = self.cfg_getter()
        tz = cfg.persona.timezone
        start_hhmm, end_hhmm = cfg.behaviour.morning_reply_window
        now = self._clock()
        if in_window(now, tz, cfg.behaviour.morning_reply_window):
            remaining = seconds_until(now, tz, end_hhmm)
            wait = self.rng.uniform(0, remaining) if remaining > 0 else 0.0
        else:
            start_wait = seconds_until(now, tz, start_hhmm)
            duration = _window_hours(cfg.behaviour.morning_reply_window) * _SECONDS_PER_HOUR
            wait = start_wait + self.rng.uniform(0, max(duration, 0.0))

        await self.sleep(wait)
        await self._run_morning_once()

        cfg = self.cfg_getter()
        tz = cfg.persona.timezone
        start_hhmm = cfg.behaviour.morning_reply_window[0]
        await self.sleep(seconds_until(self._clock(), tz, start_hhmm))

    async def _run_morning_once(self) -> None:
        rows = await self.db.night_unanswered()
        if not rows:
            return
        await self._respond(
            trigger="morning",
            trigger_msg_id=None,
            user_id=None,
            situation=SITUATION_MORNING,
            delay_sec=0,
        )
        await self.db.mark_night_answered([row.id for row in rows], self._clock())

    async def spontaneous_job(self) -> None:
        """Бесконечный цикл: раз в час проверяет условия «просто так». По образцу
        ``retention.retention_loop`` — падение одной итерации логируется и не
        останавливает цикл, после ошибки цикл спит ``_ERROR_RETRY_SEC`` вместо
        часового интервала, чтобы не уйти в busy-loop."""
        while True:
            try:
                await self.sleep(float(_SECONDS_PER_HOUR))
                await self._maybe_spontaneous()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("spontaneous_job: iteration failed")
                await self.sleep(_ERROR_RETRY_SEC)

    async def _maybe_spontaneous(self) -> None:
        cfg = self.cfg_getter()
        tz = cfg.persona.timezone
        now = self._clock()
        spontaneous_cfg = cfg.behaviour.spontaneous

        week_raw = await self.db.get_state(week_key("spontaneous_count", now, tz))
        week_count = int(week_raw) if week_raw is not None else 0
        if week_count >= spontaneous_cfg.per_week:
            return

        if not in_window(now, tz, spontaneous_cfg.window):
            return

        last_msg_at = await self.db.last_message_at(self.chat_id)
        if (
            last_msg_at is not None
            and now - last_msg_at < spontaneous_cfg.min_quiet_hours * _SECONDS_PER_HOUR
        ):
            return

        day_raw = await self.db.get_state(day_key("ambient_count", now, tz))
        day_count = int(day_raw) if day_raw is not None else 0
        if day_count >= cfg.behaviour.daily_cap:
            return

        if state_reason := await self._spontaneous_gate_blocked(cfg, now):
            logger.debug("spontaneous skipped: %s", state_reason)
            return

        window_hours = _window_hours(spontaneous_cfg.window)
        if window_hours <= 0:
            return
        probability = spontaneous_cfg.per_week / (window_hours * 7)
        if self.rng.random() >= probability:
            return

        await self._respond(
            trigger="spontaneous",
            trigger_msg_id=None,
            user_id=None,
            situation=SITUATION_SPONTANEOUS,
            delay_sec=0,
        )

    async def _spontaneous_gate_blocked(self, cfg: Config, now: int) -> str | None:
        tz = cfg.persona.timezone
        synthetic = GateMessage(
            chat_id=self.chat_id,
            tg_message_id=0,
            user_id=0,
            is_bot=False,
            text="",
            reply_to_bot=False,
            created_at=now,
        )
        state = await load_gate_state(self.db, cfg, synthetic, now)
        if state.panic:
            return "panic"
        if state.stop_until is not None and state.stop_until > now:
            return "stop"
        if in_window(now, tz, cfg.behaviour.quiet_window):
            return "night"
        if state.topic_cooldown_until is not None and state.topic_cooldown_until > now:
            return "topic_cooldown"
        return None

    async def shutdown(self) -> None:
        """Отменяет дебаунс- и pending-таймеры. Сами pending остаются в БД —
        следующий restore_pending (после рестарта) подхватит их заново."""
        tasks: list[asyncio.Task[None]] = []
        if self._debounce_task is not None and not self._debounce_task.done():
            self._debounce_task.cancel()
            tasks.append(self._debounce_task)
        for task in self._pending_tasks.values():
            if not task.done():
                task.cancel()
            tasks.append(task)
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._pending_tasks.clear()
