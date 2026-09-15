"""Responder — оркестратор генерации и отправки (PLAN.md, этап 3).

Единственный модуль, который зовёт ``bot.send_message``/``bot.send_sticker``/
``bot.send_chat_action``.
Всё остальное (гейт, БД, LLM, промпт, фильтр) уже готово — этот модуль их склеивает:

- дебаунс входящих PASS (один ``asyncio.Task`` на чат, таймер сбрасывается каждым
  новым PASS в окне ``debounce_sec``);
- отложенный ответ на обращения через ``pending_replies`` (таймер — только
  будильник в памяти, данные переживают рестарт);
- перегенерация текста строго после задержки, на свежем контексте;
- утренний джоб и джоб «просто так».

``Bot`` из aiogram инжектируется как объект, реализующий ``_BotLike`` — узкий
протокол с ``send_message``/``send_sticker``/``send_chat_action`` — чтобы тесты этого модуля не
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

Обращений к боту, накопившихся за время дебаунс-схлопывания одного pending,
может быть несколько (несколько человек написали, пока бот молчал) —
``_pending_info`` хранит список пар ``(display_name, text)``, каждое новое
схлопывание дописывает элемент, а не заменяет предыдущий. При генерации
ответа на обращение (``_generate_and_send``) вся ``situation`` строится из
этого списка через ``prompt.situation_addressed`` — так модель знает, кому
именно отвечать, а не отвечает на самое заметное сообщение в окне контекста.
Если список после рестарта пуст, ``_fire_pending_inner`` восстанавливает его
из ``messages`` (``_collect_addressed_items``): все человеческие сообщения с
``created_at >= pending.created_at``, отмеченные как обращение (реплай на
сообщение бота, mention или имя-триггер) плюс сам исходный триггер, не более
последних 5.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from trolobot import filters
from trolobot.chat_memory import render_chat_memory
from trolobot.config_models import Config, HotWindowConfig
from trolobot.db import Database, LifeEventRow, MessageRow, PendingRow
from trolobot.delays import debounce_seconds, fast_delay, pick_delay
from trolobot.filters import FilterContext
from trolobot.followup import FollowupChecker
from trolobot.gate_state import load_gate_state
from trolobot.gate_types import GateMessage, Trigger
from trolobot.judge import Judge
from trolobot.llm import LLMClient, LLMError
from trolobot.motifs import render_avoid, story_count, used_motifs
from trolobot.patterns import Patterns
from trolobot.places import render_places_menu
from trolobot.postprocess import soften
from trolobot.prompt import (
    JSON_REMINDER_CHECKIN,
    SITUATION_LATE,
    SITUATION_MORNING,
    SITUATION_SPONTANEOUS,
    build_messages,
    parse_reply,
    render_context,
    render_life,
    situation_addressed,
    situation_checkin,
    situation_followup,
    situation_life,
)
from trolobot.stickers import Sticker, StickerChooser, recent_sticker_ids, sticker_allowed
from trolobot.timeutil import (
    day_key,
    day_start,
    in_window,
    local_date,
    parse_hhmm,
    seconds_until,
    week_key,
)

logger = logging.getLogger(__name__)

# Приоритет триггера обращения при схлопывании дебаунс-буфера: reply > mention > name >
# followup > ambient (CLAUDE.md, "Интерфейсы этапа 3", "внимание как у живого человека").
_TRIGGER_PRIORITY: dict[Trigger, int] = {
    Trigger.REPLY: 4,
    Trigger.MENTION: 3,
    Trigger.NAME: 2,
    Trigger.FOLLOWUP: 1,
    Trigger.AMBIENT: 0,
}

# followup (CLAUDE.md, "внимание как у живого человека") — дешёвая проверка признала
# сообщение адресованным боту; дальше по коду ведёт себя как обычное обращение:
# pending, кулдаун-сдвиг, mention_count, потолок задержки горячего окна, обычное
# правило reply_to, весь кэш заведений в промпт, стикеры как у обращения.
_ADDRESS_TRIGGER_VALUES = (
    Trigger.MENTION.value,
    Trigger.REPLY.value,
    Trigger.NAME.value,
    Trigger.FOLLOWUP.value,
)

# Потолок на число обращений, из которых строится situation_addressed (и на сколько
# восстанавливает _collect_addressed_items после рестарта) — не больше последних 5.
_ADDRESSED_ITEMS_LIMIT = 5

# Решение владельца после живого теста: детект запроса про место регулярками не
# покрывает живую речь («колись где пиво нормальное», «есть что-то тихое на
# Ежицах?»). Поэтому при любом прямом обращении (mention/reply/name) весь кэш
# заведений (render_places_menu) уходит в промпт целиком, а решение «спрашивали ли
# про место» принимает модель по инструкции в самом блоке — не select_places.
# ambient/spontaneous/morning по-прежнему никогда не получают список — "не
# вклиниваться с рекомендацией сам" (PLAN.md, этап 5). patterns.places_request
# остаётся — только для статистики: когда регулярка сработала на trigger_text
# обращения, записываемый trigger (bot_replies.trigger / filter_log.reason)
# подменяется на "places" (CLAUDE.md, "Интерфейсы этапа 5") — бюджет обращений
# (mention_count/last_mention_reply_at) при этом считается как для исходного
# trigger_value, is_address не меняется. Регулярка также используется отдельно
# для ambient (гейт, шаг про ambient не относится к этому модулю).
_PLACES_TRIGGER = "places"

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

# shutdown(): если идёт генерация (_respond_lock занят), сколько ждать её штатного
# завершения перед отменой таймеров — штатный SIGTERM не должен рвать вызов модели.
_SHUTDOWN_GENERATION_WAIT_SEC = 10.0

# FilterContext.recent_replies — последние 50 реплик бота (CLAUDE.md, "Интерфейсы этапа 4"),
# независимо от cfg.behaviour.recent_replies_memory, которым ограничен блок {recent_replies}
# в самом промпте генерации.
_FILTER_RECENT_REPLIES_LIMIT = 50

# llm.main_model может быть пустым (LLM включён на горячую только ключом OpenRouter,
# модель ставится позже через /set) — WARNING про это не чаще раза в 10 минут, иначе
# каждый PASS в чате без модели заспамит лог.
_NO_MODEL_WARN_INTERVAL_SEC = 600

# Стикер имеет смысл предлагать только на прямое обращение или ambient — morning и
# spontaneous реагируют "в пустоту", там стикер выглядел бы неуместно (CLAUDE.md,
# "Интерфейсы: стикеры"), поэтому в этот набор не входят.
_STICKER_ELIGIBLE_TRIGGER_VALUES = (*_ADDRESS_TRIGGER_VALUES, Trigger.AMBIENT.value)

# "replies_since_sticker" ещё никогда не выставлялся (стикер в этом чате ни разу не
# отправлялся) — считаем, что реплик "после последнего стикера" было предостаточно,
# min_replies_between не может это заблокировать.
_NO_STICKER_YET_REPLIES_SINCE = 10**9

# Пауза перед отправкой стикера — короткая и фиксированная, а не по длине надписи
# (у стикера нет "длины текста ответа", это не то же самое, что typing перед текстом).
_STICKER_TYPING_RANGE_SEC = (2.0, 3.0)

# "Вернулся проверить" (CLAUDE.md, "внимание как у живого человека") — не Trigger enum
# (как "spontaneous"/"morning"/"say"/"life"): это фоновый джоб, а не реакция на PASS
# гейта, отдельного места в приоритете дебаунс-схлопывания (_TRIGGER_PRIORITY) ему не
# нужно.
_CHECKIN_TRIGGER = "checkin"

# state-ключи джоба checkin_job/_maybe_checkin. checkin_due — момент следующей проверки;
# checkin_due_hot_until — значение "hot_until" (или last_bot_reply_at, если горячее окно
# выключено), от которого этот checkin_due был вычислен в последний раз — новый/изменившийся
# hot_until (новая реплика бота переоткрыла окно) обесценивает уже посчитанный due и требует
# пересчёта "с нуля" (иначе стухший due, посчитанный от прошлого закрытия окна, сработал бы
# сразу после того, как бот заговорил заново).
_CHECKIN_DUE_KEY = "checkin_due"
_CHECKIN_DUE_REF_KEY = "checkin_due_hot_until"
_CHECKIN_LAST_AT_KEY = "checkin_last_at"

# Сколько последних реплик персонажа отдаётся дешёвой проверке "это мне?" при разборе
# выбранного checkin-сообщения (CLAUDE.md, "меньше и разнообразнее", мера 3).
_CHECKIN_FOLLOWUP_RECENT_REPLIES = 3

# Триггеры, которые бот выбирает сам, без обращения к нему: потолок присутствия и пауза
# между репликами (CLAUDE.md, "меньше и разнообразнее", меры 1 и 2) режут именно их, и
# режут молчанием — переносить тут нечего, повода ждать нет. Обращения ограничивает
# _recheck (потолок) и перенос pending (пауза), /life и /say — кнопка владельца — не
# ограничены ничем.
_UNSOLICITED_TRIGGER_VALUES = (
    Trigger.AMBIENT.value,
    "spontaneous",
    "morning",
    _CHECKIN_TRIGGER,
)


def _unique_participant_names(context_rows: list[MessageRow]) -> list[str]:
    """Уникальные display_name не-ботов из context_rows, в порядке первого появления."""
    names: list[str] = []
    seen: set[str] = set()
    for row in context_rows:
        if row.is_bot or not row.display_name or row.display_name in seen:
            continue
        seen.add(row.display_name)
        names.append(row.display_name)
    return names


@dataclass(frozen=True)
class SendOutcome:
    """Итог одной попытки отправки для /life и /say (CLAUDE.md, «события жизни»).

    reason — та же строка, что записана в filter_log: "send:life", "send:say",
    "llm:silent", причина среза фильтра, "blocked:panic"/"blocked:stop".
    """

    sent: bool
    text: str
    reason: str
    tg_message_id: int | None = None


class _SentMessageLike(Protocol):
    @property
    def message_id(self) -> int: ...


class _BotLike(Protocol):
    """Узкий протокол вместо ``aiogram.Bot`` — тесты подделывают его без aiogram."""

    async def send_message(
        self, chat_id: int, text: str, *, reply_to_message_id: int | None = None
    ) -> _SentMessageLike: ...

    async def send_sticker(
        self, chat_id: int, sticker: str, *, reply_to_message_id: int | None = None
    ) -> _SentMessageLike: ...

    async def send_chat_action(self, chat_id: int, action: str) -> object: ...


class _PromptStoreLike(Protocol):
    """Узкий протокол вместо ``stores.PromptStore`` (CLAUDE.md, "Интерфейсы этапа 6").

    Читается заново на каждом ``_respond`` — не кэшируется в конструкторе — чтобы
    горячая правка промпта/few-shot (``/rollback``, ``/ex add``) подхватывалась
    следующим же ответом без рестарта процесса.
    """

    def system_prompt(self) -> str: ...
    def prompt_version(self) -> int: ...
    def few_shot_text(self) -> str: ...
    def few_shot_version(self) -> int: ...


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
        judge: Judge | None = None,
        sticker_chooser: StickerChooser | None = None,
        followup: FollowupChecker | None = None,
        patterns_getter: Callable[[], Patterns],
        prompt_store: _PromptStoreLike,
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
        self.judge = judge
        self.sticker_chooser = sticker_chooser
        # Дешёвая проверка "это мне?" — здесь она нужна не гейту (там её зовёт bot.py),
        # а checkin'у: строку, которую выбрала основная модель, перепроверяет вторая
        # (CLAUDE.md, "меньше и разнообразнее", мера 3). None -> проверки нет.
        self.followup = followup
        self.patterns_getter = patterns_getter
        self.prompt_store = prompt_store
        self.rng = rng
        self.chat_id = chat_id
        self.bot_user_id = bot_user_id
        self._clock = clock
        self.sleep = sleep

        self._debounce_buffer: list[tuple[Trigger, GateMessage, str]] = []
        self._debounce_task: asyncio.Task[None] | None = None
        self._pending_tasks: dict[int, asyncio.Task[None]] = {}
        # pending_id -> [(display_name, text), ...] — все обращения, накопленные за
        # время жизни этого pending (схлопывание дописывает, не заменяет). Только для
        # pending, поставленных в этом процессе; после рестарта список пуст и
        # _fire_pending_inner восстанавливает его из БД (_collect_addressed_items).
        self._pending_info: dict[int, list[tuple[str, str]]] = {}
        # Сериализует генерацию+отправку+счётчики одного Responder: без этого два PASS,
        # ждущих LLM параллельно, могли бы оба проскочить одну и ту же проверку бюджета.
        self._respond_lock = asyncio.Lock()
        # Таймстемп (в шкале clock()/now) последнего WARNING про пустой llm.main_model —
        # throttle на _NO_MODEL_WARN_INTERVAL_SEC, чтобы не спамить лог на каждый PASS.
        self._last_no_model_warn_at: int | None = None

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
                trigger_text="",
            )
            return

        cfg = self.cfg_getter()
        now = self._clock()
        hot_until = await self._hot_until(cfg, now)
        earliest = await self._mention_earliest_due(cfg, msg.user_id, now)
        if hot_until is not None:
            earliest = min(earliest, now + cfg.behaviour.hot_window.mention_max_delay_sec)
        pending_rows = await self.db.load_pending()
        if pending_rows:
            pending = pending_rows[0]
            new_due = now + fast_delay(cfg.behaviour, self.rng)
            if hot_until is not None:
                new_due = self._cap_hot_mention_delay(new_due, now, cfg.behaviour.hot_window)
            new_due = self._apply_cooldown_floor(new_due, earliest)
            await self.db.update_pending_due(pending.id, new_due)
            # Схлопывание: новое обращение дописывается к уже накопленным для этого
            # pending, а не заменяет их (несколько человек могли написать боту, пока
            # он ждал) — situation_addressed при генерации покажет их все.
            self._pending_info.setdefault(pending.id, []).append((display_name, msg.text))
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
            if hot_until is not None:
                due = self._cap_hot_mention_delay(due, now, cfg.behaviour.hot_window)
            due = self._apply_cooldown_floor(due, earliest)
            pending_id = await self.db.insert_pending(
                trigger_tg_message_id=msg.tg_message_id,
                user_id=msg.user_id,
                trigger=trigger.value,
                due_at=due,
                created_at=now,
            )
            self._pending_info[pending_id] = [(display_name, msg.text)]
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

    async def _mention_earliest_due(self, cfg: Config, user_id: int, now: int) -> int:
        """Кулдаун обращения превращён в задержку, не в отказ (решение владельца,
        CLAUDE.md/PLAN.md этап 3): гейт (шаг 6) больше не дропает по кулдауну, вместо
        этого ответ на обращение не может уйти раньше ``earliest`` — максимума из
        кулдауна по чату и кулдауна по автору. Значения last_*_at читаются из state
        напрямую (по образцу ``_recheck_ambient_budget``); отсутствующее -> 0, что на
        шкале unix-времени всегда меньше ``now`` и поэтому не сдвигает ничего."""
        behaviour = cfg.behaviour
        last_chat_raw = await self.db.get_state("last_mention_reply_at")
        last_chat = int(last_chat_raw) if last_chat_raw is not None else 0
        last_user_raw = await self.db.get_state(f"last_mention_reply_at:{user_id}")
        last_user = int(last_user_raw) if last_user_raw is not None else 0
        return max(
            last_chat + behaviour.mention_chat_cooldown_sec,
            last_user + behaviour.mention_cooldown_sec,
        )

    def _apply_cooldown_floor(self, due: int, earliest: int) -> int:
        """due, не раньше earliest; если пришлось сдвинуть — небольшой случайный
        разброс сверху earliest (5-30с), чтобы все сдвинутые ответы не били в одну
        секунду, и лог INFO про сдвиг."""
        if due < earliest:
            due = earliest + self.rng.randint(5, 30)
            logger.info("mention delayed by cooldown until %s", due)
        return due

    async def _hot_until(self, cfg: Config, now: int) -> int | None:
        """``hot_until`` из state (CLAUDE.md, "горячее окно"), если горячее окно
        включено и ещё открыто; иначе ``None``. Общий примитив: используется и для
        потолка задержки ответа на обращение (``_handle_debounced_inner``), и для
        признака «в окне» ambient-реплики (``_generate_and_send``). Читается из
        state напрямую, по образцу ``_mention_earliest_due`` — не через гейт,
        обращения гейт не трогает вовсе."""
        if not cfg.behaviour.hot_window.enabled:
            return None
        raw = await self.db.get_state("hot_until")
        if raw is None:
            return None
        try:
            hot_until = int(raw)
        except ValueError:
            return None
        return hot_until if hot_until > now else None

    def _cap_hot_mention_delay(self, due: int, now: int, hot_window: HotWindowConfig) -> int:
        """В горячем окне ответ на обращение не может быть отложен дальше
        ``mention_max_delay_sec`` — сдвиг кулдауном (``_apply_cooldown_floor``)
        применяется уже поверх этого потолка."""
        capped = now + hot_window.mention_max_delay_sec
        if due > capped:
            logger.info("hot window: mention delay capped")
            return capped
        return due

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
            items = self._pending_info.pop(row.id, None)
            if items:
                display_name, text = items[-1]
            else:
                display_name, text = "Участник", ""
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

        # Пауза между репликами (CLAUDE.md, "меньше и разнообразнее", мера 2):
        # обращение не отбрасывается, а переносится — ответ уйдёт, просто не встык к
        # предыдущей реплике бота.
        delayed_due = await self._min_gap_due(cfg, now)
        if delayed_due is not None:
            await self.db.update_pending_due(row.id, delayed_due)
            logger.info("min gap: pending delayed until %s", delayed_due)
            self._schedule_pending_timer(row, delayed_due)
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

        addressed_items = self._pending_info.pop(row.id, None)
        if not addressed_items:
            # Процесс перезапустился между постановкой pending и его срабатыванием —
            # _pending_info пуст. Восстанавливаем накопленные обращения из messages.
            addressed_items = await self._collect_addressed_items(row)
        pending_text = addressed_items[-1][1] if addressed_items else ""
        delay_sec = now - row.created_at
        # mark_pending_done НЕ вызывается здесь (инцидент: раньше ставился до генерации —
        # SIGTERM во время вызова модели терял ответ навсегда, restore_pending его уже не
        # видел). pending_id уходит в _respond/_generate_and_send и помечается done только
        # по итогу обработки — см. _finish_pending.
        await self._respond(
            trigger=row.trigger,
            trigger_msg_id=row.trigger_tg_message_id,
            user_id=row.user_id,
            situation="",
            delay_sec=delay_sec,
            trigger_text=pending_text,
            addressed_items=addressed_items,
            pending_id=row.id,
        )

    async def _collect_addressed_items(self, row: PendingRow) -> list[tuple[str, str]]:
        """Восстанавливает обращения к боту для pending, потерянного при рестарте
        (``_pending_info`` пуст — процесс перезапустился между постановкой pending и
        его срабатыванием, накопленные в памяти пары (display_name, text) утрачены).

        Берёт человеческие сообщения чата с ``created_at >= row.created_at`` (момент
        постановки pending) и оставляет те, что похожи на обращение к боту: реплай на
        сообщение бота (``bot_reply_by_tg_id`` не None), либо ``mentions_bot``, либо
        ``name_trigger``. Исходный триггер (``row.trigger_tg_message_id``) добавляется
        явно и всегда первым — debounce мог поставить его created_at чуть раньше
        ``row.created_at`` (created_at pending — время постановки, не время исходного
        сообщения), и тогда фильтр по created_at его бы не нашёл. Не более последних
        ``_ADDRESSED_ITEMS_LIMIT`` элементов — обрезает и prompt.situation_addressed,
        но дублируем здесь, чтобы не тащить в память лишнее на длинных схлопываниях."""
        patterns = self.patterns_getter()
        items: list[tuple[str, str]] = []
        seen_ids: set[int] = set()

        def _add(msg_row: MessageRow) -> None:
            if msg_row.tg_message_id is None or msg_row.tg_message_id in seen_ids:
                return
            seen_ids.add(msg_row.tg_message_id)
            items.append((msg_row.display_name or "", msg_row.text or ""))

        trigger_msg = await self.db.message_by_tg_id(self.chat_id, row.trigger_tg_message_id)
        if trigger_msg is not None:
            _add(trigger_msg)

        candidates = await self.db.messages_since(self.chat_id, row.created_at)
        for candidate in candidates:
            text = candidate.text or ""
            addressed = False
            if candidate.reply_to_tg_message_id is not None:
                bot_reply = await self.db.bot_reply_by_tg_id(candidate.reply_to_tg_message_id)
                addressed = bot_reply is not None
            if not addressed and patterns.mentions_bot(text):
                addressed = True
            if not addressed and patterns.name_trigger(text) is not None:
                addressed = True
            if addressed:
                _add(candidate)

        return items[-_ADDRESSED_ITEMS_LIMIT:]

    async def _presence_over_cap(self, cfg: Config, now: int) -> bool:
        """Суточный потолок присутствия выбран (CLAUDE.md, "меньше и разнообразнее",
        мера 1). Считает по тем же таблицам, что и ``gate_state`` — счётчики могли
        измениться, пока ответ ждал своей задержки или лока."""
        presence = cfg.behaviour.presence
        if not presence.enabled:
            return False
        midnight = day_start(now, cfg.persona.timezone)
        return presence.over_cap(
            human_messages_today=await self.db.count_messages_today(self.chat_id, midnight),
            bot_replies_today=await self.db.count_bot_replies_today(midnight),
        )

    async def _min_gap_due(self, cfg: Config, now: int) -> int | None:
        """Момент, раньше которого следующее сообщение бота уйти не может — или None,
        если пауза уже выдержана (CLAUDE.md, "меньше и разнообразнее", мера 2).

        Пауза считается от последней реплики бота в ``bot_replies``, то есть от любого
        сообщения — текста, стикера, ``/life``, ``/say``. Небольшой случайный разброс
        сверху (5-30с) — по образцу ``_apply_cooldown_floor``: чтобы несколько
        отложенных ответов не били в одну и ту же секунду."""
        min_gap = cfg.behaviour.min_gap_sec
        if min_gap <= 0:
            return None
        last_reply_at = await self.db.last_bot_reply_at()
        if last_reply_at is None or now - last_reply_at >= min_gap:
            return None
        return last_reply_at + min_gap + self.rng.randint(5, 30)

    async def _recheck(self, row: PendingRow, now: int, cfg: Config) -> str | None:
        """Только детерминированные шаги гейта (п.1-5, п.7 и лимиты обращений).

        Кубик (п.11) и живой разговор (п.9) намеренно не перепроверяются — иначе
        перепроверка срезала бы большинство уже одобренных ambient-реплик.

        Кулдаун обращения (по чату и по человеку) не перепроверяется здесь — гейт
        (шаг 6) его вообще не проверяет: решение владельца сделало кулдаун задержкой,
        а не отказом. По построению due_at этого pending уже не раньше earliest
        (см. _mention_earliest_due/_apply_cooldown_floor в _handle_debounced_inner),
        так что к моменту срабатывания кулдаун уже не может быть нарушен.
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
        # Потолок присутствия (CLAUDE.md, "меньше и разнообразнее", мера 1): реплай на
        # сообщение бота проходит под потолком всегда, как и в гейте; остальные поводы
        # (имя, @, followup) к моменту отправки могли исчерпать суточную долю.
        if row.trigger != Trigger.REPLY.value and cfg.behaviour.presence.over_cap(
            human_messages_today=state.human_messages_today,
            bot_replies_today=state.bot_replies_today,
        ):
            return "send:recheck_presence"
        if state.mention_count_today >= cfg.behaviour.mention_daily_cap:
            return "send:recheck_mention_cap"
        return None

    # ------------------------------------------------------------------ #
    # Генерация и отправка.
    # ------------------------------------------------------------------ #

    async def _finish_pending(self, pending_id: int | None, done_at: int) -> None:
        """Помечает pending done — но только по итогу обработки (успешная отправка
        плюс insert_bot_reply, либо любой filter_log-исход молчания: recheck-провал,
        night_queue, llm:no_model, llm:invalid_json, llm:silent, срез фильтром не в
        shadow, LLMError). Вызывается явно в каждой такой точке, а не в конце
        _respond_inner безусловно — иначе необработанное исключение внутри генерации
        (см. except Exception в _respond) тоже пометило бы pending done через finally,
        что и было причиной инцидента (SIGTERM во время llm.call → ответ потерян
        навсегда, restore_pending его уже не видел).

        Риск, принятый осознанно: если процесс убьют (SIGKILL, без шанса на finally)
        уже после bot.send_message, но до этого вызова — при следующем старте
        restore_pending подхватит тот же pending и отправит дубликат. Редкий случай,
        мириться с ним дешевле, чем с потерей ответа.
        """
        if pending_id is not None:
            await self.db.mark_pending_done(pending_id, done_at)

    async def _respond(
        self,
        *,
        trigger: Trigger | str,
        trigger_msg_id: int | None,
        user_id: int | None,
        situation: str,
        delay_sec: int,
        trigger_text: str = "",
        addressed_items: list[tuple[str, str]] | None = None,
        pending_id: int | None = None,
        checkin_rows: Sequence[MessageRow] | None = None,
    ) -> None:
        try:
            await self._respond_inner(
                trigger=trigger,
                trigger_msg_id=trigger_msg_id,
                user_id=user_id,
                situation=situation,
                delay_sec=delay_sec,
                trigger_text=trigger_text,
                addressed_items=addressed_items,
                pending_id=pending_id,
                checkin_rows=checkin_rows,
            )
        except LLMError as exc:
            now = self._clock()
            await self.db.insert_filter_log(
                trigger_tg_message_id=trigger_msg_id,
                candidate_text=None,
                verdict="cut",
                stage="llm",
                reason=exc.reason,
                shadow=False,
                created_at=now,
            )
            await self._finish_pending(pending_id, now)
        except Exception:
            logger.exception(
                "responder failed: trigger=%s trigger_msg_id=%s", trigger, trigger_msg_id
            )
            # Намеренно НЕ mark_pending_done: необработанное исключение (транспортный
            # обрыв во время SIGTERM, любой сбой внутри _generate_and_send) не должно
            # "хоронить" pending — restore_pending на следующем старте подхватит его
            # заново (см. _finish_pending).

    async def _respond_inner(
        self,
        *,
        trigger: Trigger | str,
        trigger_msg_id: int | None,
        user_id: int | None,
        situation: str,
        delay_sec: int,
        trigger_text: str = "",
        addressed_items: list[tuple[str, str]] | None = None,
        pending_id: int | None = None,
        checkin_rows: Sequence[MessageRow] | None = None,
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

            # Проверка бюджета ambient/spontaneous (recheck) и признак «горячего
            # окна» для ambient живут внутри _generate_and_send — см. её докстринг:
            # hot берётся ровно один раз, до вызова модели, чтобы recheck и
            # счётчики после отправки были согласованы, даже если окно успеет
            # закрыться, пока ждём ответа LLM.
            await self._generate_and_send(
                cfg=cfg,
                tz=tz,
                trigger_value=trigger_value,
                trigger_msg_id=trigger_msg_id,
                user_id=user_id,
                situation=situation,
                delay_sec=delay_sec,
                now=now,
                trigger_text=trigger_text,
                addressed_items=addressed_items,
                pending_id=pending_id,
                checkin_rows=checkin_rows,
            )

    async def _recheck_ambient_budget(self, cfg: Config, now: int, hot: bool) -> str | None:
        """Свежая проверка ambient/spontaneous-бюджета после захвата _respond_lock.

        Использует те же поля, что и гейт (шаг 10: GateState.ambient_count_today /
        last_ambient_at), но читает их напрямую из state, а не из снимка, снятого до
        ожидания лока — он мог устареть, пока этот ответ ждал своей очереди.

        ``hot`` — признак «горячего окна» ambient-реплики (CLAUDE.md, "горячее
        окно"), вычисленный один раз в ``_generate_and_send`` и переданный сюда
        параметром, а не перечитанный заново: в окне дневной бюджет ambient не
        расходуется вовсе, вместо него проверяется свой бюджет на окно
        (``hot_ambient_count`` / ``hot_window.ambient_cap``). Для spontaneous
        ``hot`` всегда ``False`` — горячее окно на него не распространяется.
        """
        if hot:
            hot_count_raw = await self.db.get_state("hot_ambient_count")
            hot_count = int(hot_count_raw) if hot_count_raw is not None else 0
            if hot_count >= cfg.behaviour.hot_window.ambient_cap:
                return "send:recheck_hot_cap"
            return None

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
        trigger_text: str = "",
        addressed_items: list[tuple[str, str]] | None = None,
        pending_id: int | None = None,
        checkin_rows: Sequence[MessageRow] | None = None,
    ) -> SendOutcome:
        """Возвращает SendOutcome с тем же reason, что уходит в filter_log — нужно
        ``announce_life``/``say`` (CLAUDE.md, "события жизни"), которые зовут этот
        метод напрямую (в обход дебаунса/пендинга) и должны сообщить владельцу через
        commands.py, отправился ли текст. ``_respond_inner`` результат игнорирует —
        поведение обычных триггеров (gate PASS, morning, spontaneous) не меняется.

        ``hot`` (признак «горячего окна», CLAUDE.md, "горячее окно") берётся ровно
        один раз здесь, в самом начале, до вызова модели — чтобы проверка бюджета
        (``_recheck_ambient_budget``) и счётчики после отправки были согласованы,
        даже если окно успеет закрыться, пока ждём ответа LLM. Распространяется
        только на ``ambient`` — spontaneous и обращения горячее окно не трогает
        (обращения обрабатывает отдельно ``_handle_debounced_inner``).
        """
        hot = False
        if trigger_value == Trigger.AMBIENT.value:
            hot = await self._hot_until(cfg, now) is not None

        if trigger_value in _AMBIENT_LIKE_TRIGGER_VALUES:
            # Пока этот ответ ждал лок, другой мог уже уйти и обновить
            # ambient_count/last_ambient_at (или hot_ambient_count) —
            # перечитываем их с нуля вместо того, чтобы полагаться на состояние,
            # увиденное до захвата лока.
            recheck_reason = await self._recheck_ambient_budget(cfg, now, hot)
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
                await self._finish_pending(pending_id, now)
                return SendOutcome(sent=False, text="", reason=recheck_reason)

        if trigger_value in _UNSOLICITED_TRIGGER_VALUES:
            # Потолок присутствия и пауза между репликами (CLAUDE.md, "меньше и
            # разнообразнее", меры 1 и 2) — до вызова модели: неадресный повод не
            # стоит ни денег, ни места в чате. Обращения сюда не попадают: их
            # ограничивает _recheck (потолок) и перенос pending (пауза), /life и
            # /say — кнопка владельца — не ограничены вовсе.
            unsolicited_reason: str | None = None
            if await self._presence_over_cap(cfg, now):
                unsolicited_reason = "send:presence_cap"
            elif await self._min_gap_due(cfg, now) is not None:
                unsolicited_reason = "send:min_gap"
            if unsolicited_reason is not None:
                await self.db.insert_filter_log(
                    trigger_tg_message_id=trigger_msg_id,
                    candidate_text=None,
                    verdict="cut",
                    stage="send",
                    reason=unsolicited_reason,
                    shadow=False,
                    created_at=now,
                )
                await self._finish_pending(pending_id, now)
                return SendOutcome(sent=False, text="", reason=unsolicited_reason)

        if not cfg.llm.main_model:
            # LLM включён (есть openrouter_api_key), но модель ещё не задана —
            # это не ошибка вызова, а нормальное состояние до первого /set
            # llm.main_model. Молчание, без похода в сеть.
            await self.db.insert_filter_log(
                trigger_tg_message_id=trigger_msg_id,
                candidate_text=None,
                verdict="cut",
                stage="llm",
                reason="llm:no_model",
                shadow=False,
                created_at=now,
            )
            if (
                self._last_no_model_warn_at is None
                or now - self._last_no_model_warn_at >= _NO_MODEL_WARN_INTERVAL_SEC
            ):
                self._last_no_model_warn_at = now
                logger.warning("llm.main_model не задан, ответ не сгенерирован")
            await self._finish_pending(pending_id, now)
            return SendOutcome(sent=False, text="", reason="llm:no_model")

        context_rows = await self.db.recent_messages(self.chat_id, cfg.behaviour.context_window)
        recent_replies_list = await self.db.recent_bot_replies(cfg.behaviour.recent_replies_memory)
        context = render_context(context_rows)
        recent_replies = "\n".join(recent_replies_list)
        # 50 последних реплик читаются один раз на всю генерацию и переиспользуются
        # трижды: слот {avoid} (CLAUDE.md, "меньше и разнообразнее", мера 5), soften
        # (окно style:emoji_freq) и FilterContext — второй раз в БД не ходим.
        filter_recent_replies = await self.db.recent_bot_replies(_FILTER_RECENT_REPLIES_LIMIT)
        patterns = self.patterns_getter()
        # {avoid} собирается ДО вызова модели из того же списка реплик, по которому
        # потом отработают dedup:motif и style:story_quota: предупредить дешевле,
        # чем срезать готовый ответ и промолчать.
        avoid_block = render_avoid(
            used_motifs(filter_recent_replies, cfg.filters.motif_avoid_window, patterns.motifs),
            no_story=(
                story_count(filter_recent_replies, cfg.filters.story_window, patterns.story_markers)
                >= cfg.filters.story_max
            ),
        )
        # Читаем промпт/few-shot из prompt_store заново на каждом _respond, а не
        # кэшируем в конструкторе: /rollback и /ex add должны подхватываться
        # следующим же ответом, без рестарта Responder.
        system_prompt = self.prompt_store.system_prompt()
        few_shot = self.prompt_store.few_shot_text()
        age = cfg.persona.age(local_date(now, tz))

        # Заведения: только по запросу в обращении (mention/reply/name), никогда для
        # ambient/spontaneous/morning ("не вклиниваться с рекомендацией сам").
        is_address = trigger_value in _ADDRESS_TRIGGER_VALUES
        if is_address:
            # Обращение (mention/reply/name/followup) строит собственную situation
            # из накопленных обращений (может быть несколько, если несколько людей
            # написали боту за время дебаунс-схлопывания) вместо той, что передал
            # вызывающий: без явного "к тебе обратился X" модель в 30-сообщениях
            # окна контекста отвечает на самое заметное сообщение, а не на того,
            # кто реально к ней обратился ("живой" баг, из-за которого это и
            # добавлено). followup (CLAUDE.md, "внимание как у живого человека") —
            # адресность только вероятная (дешёвая проверка, не гейт), поэтому
            # использует свою форму situation_followup с явной оговоркой и правом
            # промолчать. Поздний ответ по-прежнему добавляется отдельной строкой.
            if addressed_items:
                situation = (
                    situation_followup(addressed_items)
                    if trigger_value == Trigger.FOLLOWUP.value
                    else situation_addressed(addressed_items)
                )
            else:
                situation = ""
            if delay_sec > cfg.behaviour.late_reply_threshold_sec:
                situation = f"{situation}\n{SITUATION_LATE}" if situation else SITUATION_LATE
        # Прямое обращение -> весь кэш заведений в промпт, всегда, независимо от
        # текста; regex ниже влияет только на записываемый trigger (статистика).
        places_block = render_places_menu(await self.db.places_all()) if is_address else ""
        places_requested = is_address and patterns.places_request(trigger_text)
        record_trigger = _PLACES_TRIGGER if places_requested else trigger_value

        # Слот {life} заполняется всегда, для любого триггера (CLAUDE.md, "события
        # жизни") — это память персонажа, а не данные, ограниченные обращением.
        life_block = render_life(await self.db.life_events(), tz)

        # Слот {chat_memory} (CLAUDE.md, "долгая память чата") — тоже всегда и для
        # любого триггера: пересказы прошедших недель живут дольше самих сообщений
        # и заменяют персонажу то, что уже вычистил ретеншн.
        chat_memory_block = render_chat_memory(
            await self.db.chat_memories(cfg.behaviour.chat_memory.in_prompt), tz
        )

        # checkin (CLAUDE.md, "вернулся проверить") — единственный триггер, где модель
        # сама указывает, на какое из перечисленных в situation сообщений отвечает
        # (поле "reply_to"); остальные триггеры используют обычное JSON-напоминание
        # (дефолт build_messages).
        build_messages_kwargs: dict[str, str] = {}
        if trigger_value == _CHECKIN_TRIGGER:
            build_messages_kwargs["json_reminder"] = JSON_REMINDER_CHECKIN
        messages = build_messages(
            system_prompt,
            age=age,
            few_shot=few_shot,
            context=context,
            recent_replies=recent_replies,
            places=places_block,
            situation=situation,
            avoid=avoid_block,
            life=life_block,
            chat_memory=chat_memory_block,
            **build_messages_kwargs,
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
            await self._finish_pending(pending_id, now)
            return SendOutcome(sent=False, text="", reason="llm:invalid_json")

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
            await self._finish_pending(pending_id, now)
            return SendOutcome(sent=False, text="", reason="llm:silent")

        # checkin (CLAUDE.md, "вернулся проверить"): модель указала номер сообщения
        # (1-based индекс в checkin_rows), на которое отвечает — валидный номер даёт
        # и reply_to_message_id (ниже, вместе с обычным обращением), и trigger_text
        # (текст этого сообщения — для regex:venue/regex:latin выходного фильтра, как
        # если бы это было обычное обращение). Невалидный/отсутствующий номер -> оба
        # остаются пустыми, ответ уйдёт в чат без реплая (как ambient).
        checkin_reply_to_message_id: int | None = None
        if trigger_value == _CHECKIN_TRIGGER and checkin_rows is not None:
            reply_to_index = reply.reply_to
            if reply_to_index is None or not 1 <= reply_to_index <= len(checkin_rows):
                # Ответ «всем сразу», без адресата, больше не отправляется (CLAUDE.md,
                # "меньше и разнообразнее", мера 3): модель обязана назвать номер
                # сообщения, на которое отвечает, иначе это не ответ, а вклинивание.
                await self.db.insert_filter_log(
                    trigger_tg_message_id=trigger_msg_id,
                    candidate_text=reply.text,
                    verdict="cut",
                    stage="send",
                    reason="send:checkin_no_target",
                    shadow=False,
                    created_at=now,
                )
                await self._finish_pending(pending_id, now)
                return SendOutcome(sent=False, text=reply.text, reason="send:checkin_no_target")

            selected_index = reply_to_index - 1
            selected_row = checkin_rows[selected_index]
            checkin_reply_to_message_id = selected_row.tg_message_id
            trigger_text = selected_row.text or ""
            if self.followup is not None and not await self.followup.check(
                text=trigger_text,
                display_name=selected_row.display_name or "",
                context_rows=[
                    row for index, row in enumerate(checkin_rows) if index != selected_index
                ],
                recent_replies=recent_replies_list[-_CHECKIN_FOLLOWUP_RECENT_REPLIES:],
                now=now,
            ):
                # Вторая, дешёвая модель не увидела здесь обращения к Фёдору — молчим:
                # основная модель склонна считать адресованным себе любой разговор,
                # который она сама когда-то задела.
                await self.db.insert_filter_log(
                    trigger_tg_message_id=trigger_msg_id,
                    candidate_text=reply.text,
                    verdict="cut",
                    stage="send",
                    reason="send:checkin_not_addressed",
                    shadow=False,
                    created_at=now,
                )
                await self._finish_pending(pending_id, now)
                return SendOutcome(sent=False, text=reply.text, reason="send:checkin_not_addressed")

        # Мягкая правка ДО выходного фильтра: тире и слишком частые/лишние эмодзи —
        # косметика, не нарушение характера, поэтому правится руками, а не срезается
        # (CLAUDE.md, "Интерфейсы: пост-обработка").
        fixed = soften(reply.text, recent_replies=filter_recent_replies, cfg=cfg.filters)
        if fixed.fixes:
            for fix_reason in fixed.fixes:
                await self.db.insert_filter_log(
                    trigger_tg_message_id=trigger_msg_id,
                    candidate_text=reply.text,
                    verdict="fix",
                    stage="fix",
                    reason=fix_reason,
                    shadow=False,
                    created_at=now,
                )
            logger.info("fix: %s", ", ".join(fixed.fixes))
        text = fixed.text

        if not text:
            # Реплика состояла из одного эмодзи — после правки пусто, молчание,
            # как при llm:silent.
            await self.db.insert_filter_log(
                trigger_tg_message_id=trigger_msg_id,
                candidate_text=reply.text,
                verdict="cut",
                stage="fix",
                reason="fix:empty",
                shadow=False,
                created_at=now,
            )
            await self._finish_pending(pending_id, now)
            return SendOutcome(sent=False, text=reply.text, reason="fix:empty")

        muted_ids = await self.db.muted_user_ids()
        muted_names = list((await self.db.display_names(list(muted_ids))).values())
        participant_names = _unique_participant_names(context_rows)
        bot_names = [cfg.persona.name, cfg.persona.display_name, *cfg.persona.name_triggers]
        # Всегда (не только для триггера "places") — белый список regex:venue/regex:latin
        # должен знать реальные заведения независимо от того, спрашивали про них сейчас.
        places_names = await self.db.places_names()

        filter_ctx = FilterContext(
            cfg=cfg,
            recent_replies=filter_recent_replies,
            context_rows=context_rows,
            places_names=places_names,
            participant_names=participant_names,
            bot_names=bot_names,
            muted_names=muted_names,
            patterns=patterns,
            system_prompt=system_prompt,
            trigger_text=trigger_text,
            now=now,
        )
        verdict = await filters.check_output(text, filter_ctx, self.judge)
        if not verdict.ok:
            # reasons может быть пустым только у чужого FilterVerdict, собранного
            # вручную (тесты) — тогда используем verdict.reason.
            reasons = verdict.reasons or (verdict.reason,)
            # Мера 4 контракта «меньше и разнообразнее»: shadow остаётся щадящим
            # режимом для новых слоёв, но стадии из filters.enforce_stages
            # (по умолчанию dedup и style) режут всегда — именно повторы и сорванный
            # стиль остановили чат 15.09, а shadow их пропускал.
            enforce_stages = set(cfg.filters.enforce_stages)
            enforced = [reason for reason in reasons if reason.split(":", 1)[0] in enforce_stages]
            cut = (not cfg.filters.shadow) or bool(enforced)
            # На каждую сработавшую причину — своя строка filter_log (для shadow-статистики
            # по каждому слою отдельно); shadow=1 значит «записали, но отправили».
            for reason in reasons:
                stage = reason.split(":", 1)[0] if reason else "filter"
                await self.db.insert_filter_log(
                    trigger_tg_message_id=trigger_msg_id,
                    candidate_text=text,
                    verdict="cut",
                    stage=stage,
                    reason=reason,
                    shadow=not cut,
                    created_at=now,
                )
            if cut:
                logger.info("cut: %s", ", ".join(reasons))
                await self._finish_pending(pending_id, now)
                # В shadow режет только enforce-стадия — она и уходит причиной;
                # без shadow причина обычная, первая из списка.
                cut_reason = enforced[0] if cfg.filters.shadow else reasons[0]
                return SendOutcome(sent=False, text=text, reason=cut_reason)

        reply_to_message_id: int | None = None
        if is_address and trigger_msg_id is not None:
            after_count = await self.db.messages_after(self.chat_id, trigger_msg_id)
            if after_count > 0 or delay_sec > cfg.behaviour.reply_as_reply_after_sec:
                reply_to_message_id = trigger_msg_id
        elif trigger_value == _CHECKIN_TRIGGER:
            reply_to_message_id = checkin_reply_to_message_id

        # Второй, дешёвый вызов LLM: готовый (прошедший фильтр) текст может быть
        # заменён стикером из каталога — основная модель про стикеры не знает
        # вообще (CLAUDE.md, "Интерфейсы: стикеры"). None -> отправляем текст,
        # как раньше.
        sticker = await self._maybe_choose_sticker(
            cfg=cfg,
            tz=tz,
            trigger_value=trigger_value,
            reply_text=text,
            trigger_text=trigger_text,
            now=now,
        )

        if sticker is not None:
            await self._run_typing("…", duration=self.rng.uniform(*_STICKER_TYPING_RANGE_SEC))
            sticker_sent = await self.bot.send_sticker(
                self.chat_id, sticker.file_id, reply_to_message_id=reply_to_message_id
            )
            sent_message_id = sticker_sent.message_id
            # Номер стикера в тексте (CLAUDE.md) — так он попадает в recent_replies/
            # контекст модели, в /last, и по нему recent_sticker_ids восстанавливает
            # "недавно использованные" при следующем выборе.
            sent_text = f"[стикер #{sticker.id}] {sticker.text}"
            send_reason = "send:sticker"
        else:
            await self._run_typing(text)
            text_sent = await self.bot.send_message(
                self.chat_id, text, reply_to_message_id=reply_to_message_id
            )
            sent_message_id = text_sent.message_id
            sent_text = text
            # Горячее окно — своя причина в filter_log (для статистики /why),
            # bot_replies.trigger остаётся record_trigger ("ambient") как раньше.
            send_reason = "send:ambient_hot" if hot else f"send:{record_trigger}"

        await self.db.insert_bot_reply(
            tg_message_id=sent_message_id,
            reply_to_tg_message_id=reply_to_message_id,
            trigger=record_trigger,
            trigger_tg_message_id=trigger_msg_id,
            text=sent_text,
            prompt_version=self.prompt_store.prompt_version(),
            few_shot_version=self.prompt_store.few_shot_version(),
            delay_sec=delay_sec,
            created_at=now,
        )

        # Счётчики бюджета обращений/ambient/spontaneous — общий хвост, один и тот
        # же независимо от того, ушёл текст или стикер (различаются только способ
        # отправки и то, что легло в bot_replies.text выше).
        if is_address:
            await self.db.increment_state(day_key("mention_count", now, tz))
            await self.db.set_state("last_mention_reply_at", str(now))
            if user_id is not None:
                await self.db.set_state(f"last_mention_reply_at:{user_id}", str(now))
        elif trigger_value == Trigger.AMBIENT.value:
            if hot:
                # Горячее окно не ест дневной бюджет ambient — свой счётчик на
                # окно, ambient_count(day)/last_ambient_at не трогаем.
                await self.db.increment_state("hot_ambient_count")
            else:
                await self.db.increment_state(day_key("ambient_count", now, tz))
                await self.db.set_state("last_ambient_at", str(now))
        elif trigger_value == "spontaneous":
            await self.db.increment_state(day_key("ambient_count", now, tz))
            await self.db.set_state("last_ambient_at", str(now))
            await self.db.increment_state(week_key("spontaneous_count", now, tz))
        # morning: счётчики не меняются — не входит в бюджет обращений.

        if sticker is not None:
            await self.db.increment_state(day_key("sticker_count", now, tz))
            await self.db.set_state("replies_since_sticker", "0")
        else:
            await self.db.increment_state("replies_since_sticker")

        await self.db.insert_filter_log(
            trigger_tg_message_id=trigger_msg_id,
            candidate_text=text,
            verdict="pass",
            stage="send",
            reason=send_reason,
            shadow=False,
            created_at=now,
        )
        await self._finish_pending(pending_id, now)
        # Горячее окно из ОБЩЕГО хвоста — только если владелец включил
        # hot_window.open_on_any_reply (CLAUDE.md, "меньше и разнообразнее", мера 2).
        # По умолчанию окно открывают лишь /life и /say: «после каждой своей реплики
        # быть внимательнее» на практике означало, что бот сам себе продлевал
        # присутствие в чате бесконечно.
        if cfg.behaviour.hot_window.open_on_any_reply:
            await self._maybe_open_hot_window(cfg, now)
        return SendOutcome(
            sent=True, text=sent_text, reason=send_reason, tg_message_id=sent_message_id
        )

    async def _panic_or_stop_reason(self, now: int) -> str | None:
        """ "panic"/"stop", если бот сейчас молчит — читает ``state`` тем же
        способом, что ``gate_state.load_gate_state``/``_recheck``: panic — строка
        "1", stop_until — int больше ``now``. Используется ``announce_life``/``say``
        (CLAUDE.md, "события жизни"), у которых нет гейта на пути (кнопка
        владельца "опубликовать сейчас" обходит ночное окно, лимиты и кубик, но не
        panic/stop)."""
        panic_raw = await self.db.get_state("panic")
        if panic_raw is not None and panic_raw == "1":
            return "panic"
        stop_until_raw = await self.db.get_state("stop_until")
        if stop_until_raw is not None:
            try:
                stop_until = int(stop_until_raw)
            except ValueError:
                stop_until = None
            if stop_until is not None and stop_until > now:
                return "stop"
        return None

    async def _maybe_open_hot_window(self, cfg: Config, now: int) -> None:
        """После успешной отправки ``/life`` или ``/say`` — открыть горячее окно
        (CLAUDE.md, "горячее окно"): полчаса живее обычного, пока разговор,
        скорее всего, крутится вокруг вброшенной новости/реплики. Повторный
        ``/life``/``/say`` внутри уже открытого окна продлевает его заново от
        ``now`` (перезаписывает ``hot_until``), счётчик окна сбрасывается вместе
        с этим — прежние ambient-реплики этого окна в новый лимит не считаются."""
        hot_window = cfg.behaviour.hot_window
        if not hot_window.enabled or hot_window.minutes <= 0:
            return
        await self.db.set_state("hot_until", str(now + hot_window.minutes * 60))
        await self.db.set_state("hot_ambient_count", "0")

    async def announce_life(self, event: LifeEventRow) -> SendOutcome:
        """Публикует событие жизни (``/life``/``/life post``, CLAUDE.md, "события
        жизни") прямо сейчас — в обход дебаунса, pending, ночного окна, дневных
        лимитов и кубика (решение владельца: команда в личке — кнопка "опубликовать
        сейчас", а не очередь). Держат только ``panic``/``stop_until``.

        Под ``_respond_lock`` — тот же лок, что и обычная генерация: без него
        параллельный ambient-ответ и ``/life`` могли бы одновременно читать/писать
        общий бюджет (хотя "life" в него не входит, вызов модели и запись
        bot_replies всё равно должны идти по одному за раз, как и везде в этом
        классе)."""
        async with self._respond_lock:
            cfg = self.cfg_getter()
            now = self._clock()

            blocked = await self._panic_or_stop_reason(now)
            if blocked is not None:
                await self.db.insert_filter_log(
                    trigger_tg_message_id=None,
                    candidate_text=None,
                    verdict="cut",
                    stage="send",
                    reason=f"send:blocked_{blocked}",
                    shadow=False,
                    created_at=now,
                )
                return SendOutcome(sent=False, text="", reason=f"blocked:{blocked}")

            try:
                outcome = await self._generate_and_send(
                    cfg=cfg,
                    tz=cfg.persona.timezone,
                    trigger_value="life",
                    trigger_msg_id=None,
                    user_id=None,
                    situation=situation_life(event.text),
                    delay_sec=0,
                    now=now,
                    # Текст события — как trigger_text: латинские слова из заметки
                    # владельца («Kia Ceed») попадают в белый список regex:venue/
                    # regex:latin выходного фильтра, как если бы их назвал человек
                    # в чате. На заведения и стикеры для "life" это не влияет.
                    trigger_text=event.text,
                )
            except LLMError as exc:
                await self.db.insert_filter_log(
                    trigger_tg_message_id=None,
                    candidate_text=None,
                    verdict="cut",
                    stage="llm",
                    reason=exc.reason,
                    shadow=False,
                    created_at=now,
                )
                return SendOutcome(sent=False, text="", reason=exc.reason)

            if outcome.sent and outcome.tg_message_id is not None:
                await self.db.mark_life_event_announced(
                    event.id, tg_message_id=outcome.tg_message_id, now=now
                )
            # Окно после /life открывается всегда, независимо от
            # hot_window.open_on_any_reply: это кнопка владельца, вокруг вброшенной
            # новости разговор и правда идёт (CLAUDE.md, "меньше и разнообразнее",
            # мера 2). Повторный вызов при open_on_any_reply=true безвреден —
            # значения те же.
            if outcome.sent:
                await self._maybe_open_hot_window(cfg, now)
            return outcome

    async def say(self, text: str) -> SendOutcome:
        """Отправляет ``text`` в чат дословно, без модели и без выходного фильтра
        (``/say``, CLAUDE.md, "события жизни") — та же кнопка "опубликовать сейчас",
        те же ограничения (только panic/stop), в память ``life_events`` не пишет."""
        async with self._respond_lock:
            cfg = self.cfg_getter()
            now = self._clock()

            blocked = await self._panic_or_stop_reason(now)
            if blocked is not None:
                await self.db.insert_filter_log(
                    trigger_tg_message_id=None,
                    candidate_text=None,
                    verdict="cut",
                    stage="send",
                    reason=f"send:blocked_{blocked}",
                    shadow=False,
                    created_at=now,
                )
                return SendOutcome(sent=False, text="", reason=f"blocked:{blocked}")

            await self._run_typing(text)
            sent_message = await self.bot.send_message(self.chat_id, text)
            await self.db.insert_bot_reply(
                tg_message_id=sent_message.message_id,
                reply_to_tg_message_id=None,
                trigger="say",
                trigger_tg_message_id=None,
                text=text,
                prompt_version=self.prompt_store.prompt_version(),
                few_shot_version=self.prompt_store.few_shot_version(),
                delay_sec=0,
                created_at=now,
            )
            await self.db.increment_state("replies_since_sticker")
            await self.db.insert_filter_log(
                trigger_tg_message_id=None,
                candidate_text=text,
                verdict="pass",
                stage="send",
                reason="send:say",
                shadow=False,
                created_at=now,
            )
            await self._maybe_open_hot_window(cfg, now)
            return SendOutcome(
                sent=True, text=text, reason="send:say", tg_message_id=sent_message.message_id
            )

    async def _maybe_choose_sticker(
        self,
        *,
        cfg: Config,
        tz: str,
        trigger_value: str,
        reply_text: str,
        trigger_text: str,
        now: int,
    ) -> Sticker | None:
        """None без единого похода в БД/сеть, если чузера нет или триггер не
        подходит (morning/spontaneous — "не вклиниваться со стикером туда, где и
        текст сам по себе необязателен"). Иначе — проверка бюджета
        (``sticker_allowed``, до вызова чузера: "min_replies_between не выдержан
        -> чузер не вызывается вовсе", CLAUDE.md) и, если он не заблокирован,
        сам выбор."""
        if self.sticker_chooser is None:
            return None
        if trigger_value not in _STICKER_ELIGIBLE_TRIGGER_VALUES:
            return None

        stickers_cfg = cfg.behaviour.stickers
        replies_since_raw = await self.db.get_state("replies_since_sticker")
        replies_since = (
            int(replies_since_raw)
            if replies_since_raw is not None
            else _NO_STICKER_YET_REPLIES_SINCE
        )
        count_today_raw = await self.db.get_state(day_key("sticker_count", now, tz))
        count_today = int(count_today_raw) if count_today_raw is not None else 0
        if not sticker_allowed(
            cfg=stickers_cfg, replies_since=replies_since, count_today=count_today
        ):
            return None

        recent_texts = await self.db.recent_bot_replies(stickers_cfg.recent_window * 3)
        exclude_ids = recent_sticker_ids(recent_texts, stickers_cfg.recent_window)
        return await self.sticker_chooser.choose(
            reply_text=reply_text, trigger_text=trigger_text, exclude_ids=exclude_ids, now=now
        )

    async def _run_typing(self, text: str, *, duration: float | None = None) -> None:
        """typing-цикл на ``duration`` секунд. По умолчанию (``None``) — по длине
        ``text`` (обычный текстовый ответ); явный ``duration`` — короткая пауза
        перед стикером, у которого "длины ответа" не существует."""
        if duration is None:
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

    # ------------------------------------------------------------------ #
    # «Вернулся проверить» (CLAUDE.md, "внимание как у живого человека").
    # ------------------------------------------------------------------ #

    async def checkin_job(self) -> None:
        """Бесконечный цикл: раз в ``cfg.behaviour.checkin.poll_sec`` проверяет,
        не пора ли «вернуться проверить» чат после того, как горячее окно
        закрылось (CLAUDE.md, "внимание как у живого человека: вернулся
        проверить"). По образцу ``spontaneous_job``/``retention.retention_loop``
        — падение одной итерации логируется и не останавливает цикл, после
        ошибки цикл спит ``_ERROR_RETRY_SEC`` вместо обычного периода, чтобы
        не уйти в busy-loop. Период читается из конфига заново на каждом
        заходе — ``/set behaviour.checkin.poll_sec`` подхватывается без
        рестарта."""
        while True:
            try:
                poll_sec = self.cfg_getter().behaviour.checkin.poll_sec
                await self.sleep(float(poll_sec))
                await self._maybe_checkin()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("checkin_job: iteration failed")
                await self.sleep(_ERROR_RETRY_SEC)

    async def _maybe_checkin(self) -> None:
        """Один заход джоба — CLAUDE.md, "внимание как у живого человека:
        вернулся проверить", шаг за шагом:

        enabled -> не panic/stop/ночь/topic_cooldown (``_spontaneous_gate_blocked``,
        те же проверки, что и у "просто так", но без бюджетных: у checkin нет
        своего дневного/недельного лимита) -> горячее окно ещё открыто (телефон в
        руках, ``followup`` и так справляется) -> есть хоть одна реплика бота, и с
        неё прошло не больше ``topic_max_hours`` (иначе тема умерла — ``checkin_due``
        удаляется, следующий заход начнёт "с нуля") -> ``checkin_due`` посчитан от
        актуального ``hot_until`` (изменился — новая реплика бота переоткрыла окно —
        пересчитать) и уже наступил -> есть человеческие сообщения после
        ``max(last_reply_at, checkin_last_at)`` -> ``_respond`` с ``situation_checkin``.
        Независимо от того, нашлись ли сообщения, следующий ``checkin_due`` всегда
        переставляется на ``now + randint(after_min)*60`` — цикл продолжается, пока
        тема не умрёт."""
        cfg = self.cfg_getter()
        checkin_cfg = cfg.behaviour.checkin
        if not checkin_cfg.enabled:
            return

        now = self._clock()
        if await self._spontaneous_gate_blocked(cfg, now):
            return

        if await self._hot_until(cfg, now) is not None:
            # Телефон ещё в руках: горячее окно открыто, дешёвая проверка followup
            # уже ловит адресованные сообщения — отдельная проверка не нужна.
            return

        last_reply_at = await self.db.last_bot_reply_at()
        if last_reply_at is None:
            return
        if now - last_reply_at > checkin_cfg.topic_max_hours * _SECONDS_PER_HOUR:
            await self.db.delete_state(_CHECKIN_DUE_KEY)
            await self.db.delete_state(_CHECKIN_DUE_REF_KEY)
            return

        # due считается от последнего известного hot_until (даже уже закрытого) —
        # это момент, вокруг которого разговор ещё "тёплый". Горячее окно выключено
        # целиком (raw-ключ никогда не выставлялся) -> считаем от last_reply_at, той
        # же точки отсчёта, что использовал бы hot_window, будь он включён.
        hot_until_raw = await self.db.get_state("hot_until")
        try:
            due_ref = int(hot_until_raw) if hot_until_raw is not None else last_reply_at
        except ValueError:
            due_ref = last_reply_at

        stored_due_raw = await self.db.get_state(_CHECKIN_DUE_KEY)
        stored_ref_raw = await self.db.get_state(_CHECKIN_DUE_REF_KEY)
        try:
            stored_ref = int(stored_ref_raw) if stored_ref_raw is not None else None
        except ValueError:
            stored_ref = None

        if stored_due_raw is None or stored_ref != due_ref:
            due = due_ref + self.rng.randint(*checkin_cfg.after_min) * 60
            await self.db.set_state(_CHECKIN_DUE_KEY, str(due))
            await self.db.set_state(_CHECKIN_DUE_REF_KEY, str(due_ref))
        else:
            try:
                due = int(stored_due_raw)
            except ValueError:
                due = due_ref + self.rng.randint(*checkin_cfg.after_min) * 60
                await self.db.set_state(_CHECKIN_DUE_KEY, str(due))

        if now < due:
            return

        # Тишина (CLAUDE.md, "меньше и разнообразнее", мера 3): «вернулся проверить» —
        # про разговор, который уже остыл. Если в чате писали только что, бот ждёт:
        # due не переносится, следующий poll попробует снова.
        quiet_sec = checkin_cfg.quiet_min * 60
        if quiet_sec > 0:
            last_human_at = await self.db.last_message_at(self.chat_id)
            if last_human_at is not None and now - last_human_at < quiet_sec:
                return

        checkin_last_raw = await self.db.get_state(_CHECKIN_LAST_AT_KEY)
        try:
            checkin_last_at = int(checkin_last_raw) if checkin_last_raw is not None else 0
        except ValueError:
            checkin_last_at = 0
        since = max(last_reply_at, checkin_last_at)

        rows = await self.db.messages_since(self.chat_id, since, checkin_cfg.max_messages)
        if rows:
            await self._respond(
                trigger=_CHECKIN_TRIGGER,
                trigger_msg_id=None,
                user_id=None,
                situation=situation_checkin(rows),
                delay_sec=0,
                checkin_rows=rows,
            )

        # Следующая проверка — всегда через after_min от "сейчас" (не от due_ref):
        # решение "ответил -> окно снова открыто, промолчал -> следующая проверка
        # ещё через after_min" не зависит от того, были ли сообщения в этот заход.
        after_now = self._clock()
        await self.db.set_state(_CHECKIN_LAST_AT_KEY, str(after_now))
        await self.db.set_state(
            _CHECKIN_DUE_KEY, str(after_now + self.rng.randint(*checkin_cfg.after_min) * 60)
        )

    async def shutdown(self) -> None:
        """Отменяет дебаунс- и pending-таймеры. Сами pending остаются в БД —
        следующий restore_pending (после рестарта) подхватит их заново.

        Если в момент остановки идёт генерация (``_respond_lock`` занят — идёт
        вызов модели или отправка), даём ей до ``_SHUTDOWN_GENERATION_WAIT_SEC``
        довершиться штатно, а не рвём её отменой: штатный SIGTERM не должен
        обрывать вызов модели на полуслове (инцидент — mark_pending_done,
        поставленный до генерации, из-за этого терял ответ навсегда). Не успела за
        отведённое время — отменяем как обычно; pending останется в БД (done не
        выставлен) и будет подхвачен restore_pending на следующем старте.
        """
        if self._respond_lock.locked():
            try:
                await asyncio.wait_for(
                    self._respond_lock.acquire(), timeout=_SHUTDOWN_GENERATION_WAIT_SEC
                )
            except TimeoutError:
                pass
            else:
                self._respond_lock.release()

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
