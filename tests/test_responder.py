"""Тесты для trolobot.responder: дебаунс, отложенный ответ, генерация, джобы.

Bot — простая подделка (FakeBot), без aiogram. БД — настоящая ``Database`` на
временном файле, ``Patterns`` — настоящий из ``Config()``. LLM — настоящий
``LLMClient`` с ``httpx.MockTransport``: тесты проверяют и реальную сборку
промпта (по перехваченному запросу), и разбор ответа.

Время — управляемый виртуальный ``FakeClock``. Важно: ``sleep()`` НЕ продвигает
время сам — он регистрирует ожидающего и приостанавливается на ``asyncio.Event``,
который просыпается только когда тест явно зовёт ``run_until``/``run_until_idle``.
Это осознанный выбор: более простая версия (``sleep`` сразу продвигает fake-время
и отдаёт один тик циклу событий) на практике оказалась недетерминированной —
фоновый ``asyncio.Task`` мог обогнать код теста и списать ``pending`` ещё до того,
как тест успевал это проверить. С «спящими до явного будильника» тасками
`bot.on_gate_pass(...)`/`_handle_debounced(...)` создают фоновую задачу, но она
не делает вообще ничего, пока тест не решит её продвинуть — гонок нет по
конструкции, а не по везению.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from trolobot import filters as filters_module
from trolobot.config_models import Config
from trolobot.db import Database, PendingRow
from trolobot.filters import FilterVerdict
from trolobot.gate_types import GateMessage, Trigger
from trolobot.llm import LLMClient
from trolobot.patterns import Patterns
from trolobot.prompt import SITUATION_LATE, SITUATION_MORNING
from trolobot.responder import Responder
from trolobot.timeutil import day_key, in_window

CHAT_ID = -100123456
BOT_USER_ID = 999
BOT_USERNAME = "fedorbot"

PROMPT_TEMPLATE = (
    "Ты Фёдор, тебе {age} лет.\n"
    "Примеры:\n{few_shot}\n"
    "{context}\n{recent_replies}\n{places}\n{situation}"
)

WARSAW = ZoneInfo("Europe/Warsaw")
DAY_NOW = int(datetime(2026, 1, 10, 15, 0, tzinfo=WARSAW).timestamp())
NIGHT_NOW = int(datetime(2026, 1, 10, 3, 0, tzinfo=WARSAW).timestamp())


def _config() -> Config:
    cfg = Config()
    cfg.llm.main_model = "test/model"
    return cfg


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "bot.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


@dataclass
class SentMessage:
    message_id: int


class FakeBot:
    """Подделка ``_BotLike``: пишет вызовы в единый лог, чтобы проверять порядок."""

    def __init__(self) -> None:
        self.events: list[tuple[str, ...]] = []
        self.sent: list[tuple[int, str, int | None]] = []
        self._next_id = 5000

    async def send_message(
        self, chat_id: int, text: str, *, reply_to_message_id: int | None = None
    ) -> SentMessage:
        self._next_id += 1
        self.sent.append((chat_id, text, reply_to_message_id))
        self.events.append(("send", str(chat_id), text))
        return SentMessage(message_id=self._next_id)

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        self.events.append(("typing", str(chat_id), action))


class FakeClock:
    """Виртуальное время: sleep() не продвигает его сам, ждёт явного будильника.

    ``run_until(task)`` крутит цикл «разбудить самого раннего ожидающего,
    продвинуть время до его момента, дать циклу событий тик» пока указанная
    задача не завершится. ``run_until_idle()`` — то же самое, но пока вообще
    не останется ни ожидающих, ни готовых к выполнению корутин.
    """

    def __init__(self, start: int) -> None:
        self.value = float(start)
        self._waiters: list[tuple[float, asyncio.Event]] = []

    def now(self) -> int:
        return int(self.value)

    async def sleep(self, seconds: float) -> None:
        target = self.value + max(seconds, 0.0)
        event = asyncio.Event()
        self._waiters.append((target, event))
        await event.wait()

    async def _tick(self) -> None:
        if self._waiters:
            self._waiters.sort(key=lambda item: item[0])
            target, event = self._waiters.pop(0)
            if target > self.value:
                self.value = target
            event.set()
        await asyncio.sleep(0)

    async def run_until(self, task: asyncio.Task[None], max_rounds: int = 10_000) -> None:
        for _ in range(max_rounds):
            if task.done():
                return
            await self._tick()
        raise AssertionError("FakeClock.run_until: задача не завершилась")

    async def run_until_idle(self, max_rounds: int = 10_000) -> None:
        idle_streak = 0
        for _ in range(max_rounds):
            had_waiters = bool(self._waiters)
            await self._tick()
            if not had_waiters:
                idle_streak += 1
                if idle_streak >= 20:
                    return
            else:
                idle_streak = 0
        raise AssertionError("FakeClock.run_until_idle: не успокоилось")


async def _drive(clock: FakeClock, coro: Awaitable[None]) -> asyncio.Task[None]:
    """Заворачивает корутину в Task и прогоняет её до конца через FakeClock."""
    task: asyncio.Task[None] = asyncio.ensure_future(coro)  # type: ignore[arg-type]
    await clock.run_until(task)
    return task


Handler = Callable[[httpx.Request], httpx.Response]


def _make_llm(
    cfg: Config, db_: Database, handler: Handler
) -> tuple[LLMClient, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    client = LLMClient(api_key="test-key", cfg_getter=lambda: cfg, db=db_, http=http)
    return client, calls


def _ok_response(text: str = "Бывает.") -> httpx.Response:
    content = json.dumps({"speak": True, "text": text}, ensure_ascii=False)
    body = {
        "choices": [{"message": {"content": content}}],
        "usage": {"cost": 0.001, "prompt_tokens": 10, "completion_tokens": 5},
    }
    return httpx.Response(200, json=body)


def _silent_response() -> httpx.Response:
    content = json.dumps({"speak": False, "text": ""})
    body = {
        "choices": [{"message": {"content": content}}],
        "usage": {"cost": 0.0005, "prompt_tokens": 10, "completion_tokens": 2},
    }
    return httpx.Response(200, json=body)


def _invalid_json_response() -> httpx.Response:
    body = {
        "choices": [{"message": {"content": "это не json вообще, а простой текст"}}],
        "usage": {"cost": 0.0002, "prompt_tokens": 5, "completion_tokens": 2},
    }
    return httpx.Response(200, json=body)


def _fail_handler(_request: httpx.Request) -> httpx.Response:
    raise AssertionError("LLM не должен был вызываться")


class FixedRandom:
    """Подделка random.Random: .random() всегда отдаёт заданное значение (см. test_gate.py)."""

    def __init__(self, value: float) -> None:
        self._value = value

    def random(self) -> float:
        return self._value


def _make_responder(
    db_: Database,
    cfg: Config,
    llm: LLMClient,
    bot: FakeBot,
    clock: FakeClock,
    *,
    seed: int = 0,
    rng: random.Random | FixedRandom | None = None,
) -> Responder:
    patterns = Patterns(cfg.filters, cfg.persona.name_triggers, BOT_USERNAME)
    return Responder(
        bot=bot,
        db=db_,
        cfg_getter=lambda: cfg,
        llm=llm,
        patterns_getter=lambda: patterns,
        prompt_template=PROMPT_TEMPLATE,
        few_shot_getter=lambda: 'Дима: привет\n{"speak": true, "text": "И тебе."}',
        prompt_version=1,
        few_shot_version=1,
        rng=rng if rng is not None else random.Random(seed),  # type: ignore[arg-type]
        chat_id=CHAT_ID,
        bot_user_id=BOT_USER_ID,
        clock=clock.now,
        sleep=clock.sleep,
    )


async def _tick_until(
    clock: FakeClock, predicate: Callable[[], bool], max_rounds: int = 20_000
) -> None:
    """Крутит FakeClock тиками, пока predicate() не станет True. Для бесконечных
    фоновых циклов (morning_job/spontaneous_job), которые run_until не завершит,
    так как их task никогда не done()."""
    for _ in range(max_rounds):
        if predicate():
            return
        await clock._tick()
    raise AssertionError("_tick_until: условие не выполнилось")


def _gate_message(
    *, tg_message_id: int, user_id: int, text: str, created_at: int, reply_to_bot: bool = False
) -> GateMessage:
    return GateMessage(
        chat_id=CHAT_ID,
        tg_message_id=tg_message_id,
        user_id=user_id,
        is_bot=False,
        text=text,
        reply_to_bot=reply_to_bot,
        created_at=created_at,
    )


async def _insert_message(
    db_: Database,
    *,
    tg_message_id: int,
    user_id: int,
    display_name: str,
    text: str,
    created_at: int,
    reply_to: int | None = None,
) -> None:
    await db_.insert_message(
        tg_message_id=tg_message_id,
        chat_id=CHAT_ID,
        user_id=user_id,
        display_name=display_name,
        text=text,
        reply_to_tg_message_id=reply_to,
        is_bot=False,
        created_at=created_at,
    )


def _payload(request: httpx.Request) -> dict[str, object]:
    return json.loads(request.content)  # type: ignore[no-any-return]


# --- 1. Три ambient PASS за 2 секунды -> один ответ ---


async def test_ambient_debounce_collapses_into_one_reply(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Бывает."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        for i in range(3):
            await _insert_message(
                db,
                tg_message_id=100 + i,
                user_id=5 + i,
                display_name=f"Юзер{i}",
                text="привет всем",
                created_at=DAY_NOW + i,
            )
            msg = _gate_message(
                tg_message_id=100 + i, user_id=5 + i, text="привет всем", created_at=DAY_NOW + i
            )
            await responder.on_gate_pass(msg, Trigger.AMBIENT, f"Юзер{i}")

        assert responder._debounce_task is not None
        await clock.run_until(responder._debounce_task)

        assert len(calls) == 1
        assert len(bot.sent) == 1
        assert bot.sent[0][1] == "Бывает."
        assert bot.sent[0][2] is None  # ambient всегда в поток

        tz = cfg.persona.timezone
        ambient_key = day_key("ambient_count", clock.now(), tz)
        assert await db.get_state(ambient_key) == "1"
    finally:
        await responder.shutdown()
        await llm.aclose()


# --- 2. Mention -> pending, не раньше 30с, потом отправлено, счётчики выставлены ---


async def test_mention_creates_pending_and_fires_after_delay(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("И тебе привет."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await _insert_message(
            db,
            tg_message_id=10,
            user_id=5,
            display_name="Дима",
            text="@fedorbot как сам?",
            created_at=DAY_NOW,
        )
        msg = _gate_message(
            tg_message_id=10, user_id=5, text="@fedorbot как сам?", created_at=DAY_NOW
        )
        await responder.on_gate_pass(msg, Trigger.MENTION, "Дима")

        assert responder._debounce_task is not None
        await clock.run_until(responder._debounce_task)

        # Дебаунс отработал, но pending ещё не сработал — ничего не отправлено.
        pending_rows = await db.load_pending()
        assert len(pending_rows) == 1
        assert pending_rows[0].done_at is None
        assert bot.sent == []

        pending_id = pending_rows[0].id
        task = responder._pending_tasks[pending_id]
        start = clock.value
        await clock.run_until(task)

        assert clock.value - start >= 30  # быстрый бакет: минимум 30с
        assert len(calls) == 1
        assert len(bot.sent) == 1
        assert bot.sent[0][1] == "И тебе привет."
        assert (await db.recent_bot_replies(5)) == ["И тебе привет."]

        tz = cfg.persona.timezone
        mention_key = day_key("mention_count", clock.now(), tz)
        assert await db.get_state(mention_key) == "1"
        assert await db.get_state("last_mention_reply_at") is not None
        assert await db.get_state("last_mention_reply_at:5") is not None
    finally:
        await responder.shutdown()
        await llm.aclose()


# --- 3. Второй mention во время ожидания схлопывает pending ---


async def test_second_mention_collapses_pending_without_duplicate(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        msg1 = _gate_message(tg_message_id=20, user_id=5, text="фёдор, ты тут?", created_at=DAY_NOW)
        await responder._handle_debounced(Trigger.NAME, msg1, "Дима")

        pending_after_1 = await db.load_pending()
        assert len(pending_after_1) == 1
        pending_id = pending_after_1[0].id
        task1 = responder._pending_tasks[pending_id]
        assert not task1.done()  # ничего не продвигали — таймер ещё не срабатывал

        msg2 = _gate_message(
            tg_message_id=21, user_id=6, text="федя, ты где", created_at=DAY_NOW + 2
        )
        await responder._handle_debounced(Trigger.NAME, msg2, "Оля")

        pending_after_2 = await db.load_pending()
        assert len(pending_after_2) == 1  # не вторая задача, тот же id
        assert pending_after_2[0].id == pending_id

        fast_hi = cfg.behaviour.reply_delay_buckets[0].range_sec[1]
        assert pending_after_2[0].due_at - (DAY_NOW + 2) <= fast_hi
        assert responder._pending_tasks[pending_id] is not task1  # старый таймер заменён

        assert calls == []  # генерация ещё не случилась
    finally:
        await responder.shutdown()
        await llm.aclose()


# --- 4. Срочное сообщение -> быстрый бакет ---


async def test_urgent_message_gets_fast_bucket(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        msg = _gate_message(
            tg_message_id=30, user_id=5, text="@fedorbot куда идём сегодня?", created_at=DAY_NOW
        )
        await responder.on_gate_pass(msg, Trigger.MENTION, "Дима")
        assert responder._debounce_task is not None
        await clock.run_until(responder._debounce_task)

        rows = await db.load_pending()
        assert len(rows) == 1
        assert rows[0].due_at - rows[0].created_at <= cfg.behaviour.urgent_max_delay_sec
        assert calls == []
    finally:
        await responder.shutdown()
        await llm.aclose()


# --- 5. Pending, чей due попал в ночь -> night_queue, pending done, LLM не вызван ---


async def test_pending_due_in_quiet_window_goes_to_night_queue(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(NIGHT_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        pending_id = await db.insert_pending(
            trigger_tg_message_id=40,
            user_id=5,
            trigger="mention",
            due_at=NIGHT_NOW,
            created_at=NIGHT_NOW - 60,
        )
        row = PendingRow(
            id=pending_id,
            trigger_tg_message_id=40,
            user_id=5,
            trigger="mention",
            due_at=NIGHT_NOW,
            created_at=NIGHT_NOW - 60,
            done_at=None,
        )

        await responder._fire_pending(row)

        night_rows = await db.night_unanswered()
        assert len(night_rows) == 1
        assert night_rows[0].tg_message_id == 40
        assert night_rows[0].display_name == "Участник"  # нет в памяти -> заглушка

        pending_after = await db.load_pending()
        assert pending_after == []

        summary = dict(await db.filter_log_summary(0))
        assert summary.get("send:night") == 1
        assert calls == []
        assert bot.sent == []
    finally:
        await responder.shutdown()
        await llm.aclose()


# --- 6. /stop во время ожидания -> recheck_stop, не отправлено ---


async def test_recheck_catches_stop_during_wait(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await db.set_state("stop_until", str(DAY_NOW + 1000))
        pending_id = await db.insert_pending(
            trigger_tg_message_id=50,
            user_id=5,
            trigger="mention",
            due_at=DAY_NOW,
            created_at=DAY_NOW - 60,
        )
        row = PendingRow(
            id=pending_id,
            trigger_tg_message_id=50,
            user_id=5,
            trigger="mention",
            due_at=DAY_NOW,
            created_at=DAY_NOW - 60,
            done_at=None,
        )

        await responder._fire_pending(row)

        summary = dict(await db.filter_log_summary(0))
        assert summary.get("send:recheck_stop") == 1
        assert bot.sent == []
        assert calls == []

        pending_after = await db.load_pending()
        assert pending_after == []
    finally:
        await responder.shutdown()
        await llm.aclose()


# --- 7. speak=false -> llm:silent, не отправлено ---


async def test_speak_false_is_silent(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _silent_response())
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await responder._respond(
            trigger=Trigger.MENTION, trigger_msg_id=60, user_id=5, situation="", delay_sec=40
        )

        assert bot.sent == []
        assert len(calls) == 1
        summary = dict(await db.filter_log_summary(0))
        assert summary.get("llm:silent") == 1
    finally:
        await responder.shutdown()
        await llm.aclose()


# --- 8. Невалидный JSON -> llm:invalid_json ---


async def test_invalid_json_is_logged_and_silent(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _invalid_json_response())
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await responder._respond(
            trigger=Trigger.MENTION, trigger_msg_id=70, user_id=5, situation="", delay_sec=40
        )

        assert bot.sent == []
        assert len(calls) == 1
        summary = dict(await db.filter_log_summary(0))
        assert summary.get("llm:invalid_json") == 1
    finally:
        await responder.shutdown()
        await llm.aclose()


# --- 9. Бюджет исчерпан -> llm:budget, без сети ---


async def test_llm_budget_error_is_logged(db: Database) -> None:
    cfg = _config()
    cfg.llm.daily_budget_usd = 0.01
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    tz = cfg.persona.timezone
    await db.add_state_float(day_key("llm_spent_usd", DAY_NOW, tz), 1.0)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await responder._respond(
            trigger=Trigger.MENTION, trigger_msg_id=80, user_id=5, situation="", delay_sec=40
        )

        assert bot.sent == []
        assert calls == []  # llm:budget рубит до сети
        summary = dict(await db.filter_log_summary(0))
        assert summary.get("llm:budget") == 1
    finally:
        await responder.shutdown()
        await llm.aclose()


# --- 10. restore_pending: недавнее просроченное -> сразу с SITUATION_LATE; старое -> restart ---


async def test_restore_pending_late_flag_and_restart(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Задумался о своём."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        # Поставлен час назад, due почти сейчас: overdue маленький (< threshold),
        # но полная задержка с created_at большая -> SITUATION_LATE при срабатывании.
        recent_id = await db.insert_pending(
            trigger_tg_message_id=90,
            user_id=5,
            trigger="mention",
            due_at=DAY_NOW - 50,
            created_at=DAY_NOW - 3600,
        )
        # due просрочен на 700с (> late_reply_threshold_sec=600) -> restart.
        old_id = await db.insert_pending(
            trigger_tg_message_id=91,
            user_id=6,
            trigger="mention",
            due_at=DAY_NOW - 700,
            created_at=DAY_NOW - 4000,
        )

        await responder.restore_pending()

        summary = dict(await db.filter_log_summary(0))
        assert summary.get("send:restart") == 1

        pending_after_restore = await db.load_pending()
        remaining_ids = {row.id for row in pending_after_restore}
        assert remaining_ids == {recent_id}
        assert old_id not in responder._pending_tasks
        assert recent_id in responder._pending_tasks

        await clock.run_until(responder._pending_tasks[recent_id])

        assert len(calls) == 1
        payload = _payload(calls[0])
        user_content = payload["messages"][1]["content"]  # type: ignore[index]
        assert SITUATION_LATE in user_content
        assert len(bot.sent) == 1
    finally:
        await responder.shutdown()
        await llm.aclose()


# --- 11. Реплай-режим ---


async def test_reply_mode_depends_on_messages_after_and_delay(db: Database) -> None:
    cfg = _config()
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("Ок."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        # (a) есть сообщения после триггера -> reply_to = trigger id
        await _insert_message(
            db, tg_message_id=10, user_id=5, display_name="Дима", text="триггер", created_at=DAY_NOW
        )
        await _insert_message(
            db,
            tg_message_id=11,
            user_id=6,
            display_name="Оля",
            text="что-то ещё",
            created_at=DAY_NOW + 1,
        )
        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.MENTION, trigger_msg_id=10, user_id=5, situation="", delay_sec=5
            ),
        )
        assert bot.sent[-1][2] == 10

        # (b) нет сообщений после и быстро -> None
        await _insert_message(
            db,
            tg_message_id=20,
            user_id=5,
            display_name="Дима",
            text="ещё триггер",
            created_at=DAY_NOW + 10,
        )
        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.MENTION, trigger_msg_id=20, user_id=5, situation="", delay_sec=5
            ),
        )
        assert bot.sent[-1][2] is None

        # (c) ambient -> всегда None, даже с большой задержкой
        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.AMBIENT,
                trigger_msg_id=20,
                user_id=5,
                situation="",
                delay_sec=999_999,
            ),
        )
        assert bot.sent[-1][2] is None
    finally:
        await responder.shutdown()
        await llm.aclose()


# --- 12. Утренний джоб: два ночных -> один вызов, situation MORNING, обе отвечены ---


async def test_morning_job_answers_all_night_messages_once(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Доброе утро, был занят."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        night_id_1 = await db.enqueue_night(
            tg_message_id=200,
            user_id=5,
            display_name="Дима",
            text="фёдор ты тут?",
            created_at=NIGHT_NOW,
        )
        night_id_2 = await db.enqueue_night(
            tg_message_id=201,
            user_id=6,
            display_name="Оля",
            text="федя спишь?",
            created_at=NIGHT_NOW + 60,
        )

        await _drive(clock, responder._run_morning_once())

        assert len(calls) == 1
        payload = _payload(calls[0])
        user_content = payload["messages"][1]["content"]  # type: ignore[index]
        assert SITUATION_MORNING in user_content

        unanswered = await db.night_unanswered()
        assert unanswered == []

        conn = db._conn
        assert conn is not None
        cursor = await conn.execute(
            "SELECT answered_at FROM night_queue WHERE id IN (?, ?)", (night_id_1, night_id_2)
        )
        rows = await cursor.fetchall()
        assert all(row["answered_at"] is not None for row in rows)

        tz = cfg.persona.timezone
        assert await db.get_state(day_key("mention_count", clock.now(), tz)) is None
    finally:
        await responder.shutdown()
        await llm.aclose()


# --- 13. Typing вызван хотя бы раз перед send_message ---


async def test_typing_called_before_send(db: Database) -> None:
    cfg = _config()
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("Ответ подлиннее для тайпинга."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.MENTION, trigger_msg_id=300, user_id=5, situation="", delay_sec=5
            ),
        )

        kinds = [event[0] for event in bot.events]
        assert "typing" in kinds
        send_index = kinds.index("send")
        typing_index = kinds.index("typing")
        assert typing_index < send_index
    finally:
        await responder.shutdown()
        await llm.aclose()


# --- 14. shadow=True отправляет и логирует shadow=1; shadow=False не отправляет ---


async def test_shadow_mode_sends_but_logs_cut(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config()
    cfg.filters.shadow = True
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("Кандидат."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)

    async def fake_check_output(text: str, ctx: object) -> FilterVerdict:
        return FilterVerdict(ok=False, reason="regex:length")

    monkeypatch.setattr(filters_module, "check_output", fake_check_output)

    try:
        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.MENTION, trigger_msg_id=400, user_id=5, situation="", delay_sec=5
            ),
        )
        assert len(bot.sent) == 1

        conn = db._conn
        assert conn is not None
        cursor = await conn.execute(
            "SELECT shadow, verdict, reason FROM filter_log WHERE reason = ?", ("regex:length",)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["shadow"] == 1
        assert row["verdict"] == "cut"
    finally:
        await responder.shutdown()
        await llm.aclose()

    # shadow=False -> не отправлено (ранний return до typing, драйвить не нужно)
    cfg2 = _config()
    cfg2.filters.shadow = False
    llm2, _calls2 = _make_llm(cfg2, db, lambda _req: _ok_response("Кандидат2."))
    bot2 = FakeBot()
    clock2 = FakeClock(DAY_NOW)
    responder2 = _make_responder(db, cfg2, llm2, bot2, clock2)
    try:
        await responder2._respond(
            trigger=Trigger.MENTION, trigger_msg_id=401, user_id=5, situation="", delay_sec=5
        )
        assert bot2.sent == []

        conn = db._conn
        assert conn is not None
        cursor = await conn.execute(
            "SELECT shadow FROM filter_log WHERE trigger_tg_message_id = ?", (401,)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["shadow"] == 0
    finally:
        await responder2.shutdown()
        await llm2.aclose()


# --- 15. _fire_pending переживает ошибку в db.recent_messages: залогировано, ---
# --- процесс жив, следующий pending обрабатывается нормально. ---


async def test_fire_pending_survives_recent_messages_error_and_continues(
    db: Database, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Ок, живой."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        real_recent_messages = db.recent_messages
        state = {"raised": False}

        async def flaky_recent_messages(chat_id: int, limit: int) -> list[object]:
            if not state["raised"]:
                state["raised"] = True
                raise RuntimeError("db is on fire")
            return await real_recent_messages(chat_id, limit)  # type: ignore[return-value]

        monkeypatch.setattr(db, "recent_messages", flaky_recent_messages)

        pending_id_1 = await db.insert_pending(
            trigger_tg_message_id=900,
            user_id=5,
            trigger="mention",
            due_at=DAY_NOW,
            created_at=DAY_NOW - 60,
        )
        row1 = PendingRow(
            id=pending_id_1,
            trigger_tg_message_id=900,
            user_id=5,
            trigger="mention",
            due_at=DAY_NOW,
            created_at=DAY_NOW - 60,
            done_at=None,
        )

        with caplog.at_level(logging.ERROR):
            await responder._fire_pending(row1)

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert bot.sent == []
        assert calls == []
        # pending уже помечен done — упало уже внутри генерации, после mark_pending_done.
        assert await db.load_pending() == []

        # Процесс жив: следующий PASS (новый pending) обрабатывается штатно.
        pending_id_2 = await db.insert_pending(
            trigger_tg_message_id=901,
            user_id=6,
            trigger="mention",
            due_at=DAY_NOW,
            created_at=DAY_NOW - 60,
        )
        row2 = PendingRow(
            id=pending_id_2,
            trigger_tg_message_id=901,
            user_id=6,
            trigger="mention",
            due_at=DAY_NOW,
            created_at=DAY_NOW - 60,
            done_at=None,
        )
        await _drive(clock, responder._fire_pending(row2))

        assert len(bot.sent) == 1
        assert bot.sent[0][1] == "Ок, живой."
        assert len(calls) == 1
    finally:
        await responder.shutdown()
        await llm.aclose()


# --- 16. morning_job переживает падение одной итерации (night_unanswered бросает ---
# --- один раз), следующая итерация отрабатывает штатно. ---


async def test_morning_job_survives_failing_iteration_and_continues(
    db: Database, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Доброе утро, был занят."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await db.enqueue_night(
            tg_message_id=910,
            user_id=5,
            display_name="Дима",
            text="фёдор ты тут?",
            created_at=NIGHT_NOW,
        )

        real_night_unanswered = db.night_unanswered
        state = {"raised": False}

        async def flaky_night_unanswered() -> list[object]:
            if not state["raised"]:
                state["raised"] = True
                raise RuntimeError("night_unanswered is on fire")
            return await real_night_unanswered()  # type: ignore[return-value]

        monkeypatch.setattr(db, "night_unanswered", flaky_night_unanswered)

        task: asyncio.Task[None] = asyncio.ensure_future(responder.morning_job())
        try:
            with caplog.at_level(logging.ERROR):
                await _tick_until(clock, lambda: len(bot.sent) >= 1)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "morning_job" in errors[0].message

        assert len(calls) == 1
        assert len(bot.sent) == 1
        assert bot.sent[0][1] == "Доброе утро, был занят."
    finally:
        await responder.shutdown()
        await llm.aclose()


# --- 17. morning_job цикл: спит до момента внутри окна 07:00-08:00, отвечает ---
# --- один раз, следующий раз просыпается на следующий день (два дня подряд). ---


async def test_morning_job_loop_wakes_within_window_two_consecutive_days(
    db: Database,
) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Доброе утро."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)  # 2026-01-10 15:00 Warsaw — вне окна, после него
    responder = _make_responder(db, cfg, llm, bot, clock)
    tz = cfg.persona.timezone
    window = cfg.behaviour.morning_reply_window

    await db.enqueue_night(
        tg_message_id=920, user_id=5, display_name="Дима", text="утро1", created_at=DAY_NOW
    )

    task: asyncio.Task[None] = asyncio.ensure_future(responder.morning_job())
    try:
        await _tick_until(clock, lambda: len(bot.sent) >= 1)
        first_send_at = clock.now()
        assert in_window(first_send_at, tz, window)

        await db.enqueue_night(
            tg_message_id=921,
            user_id=6,
            display_name="Оля",
            text="утро2",
            created_at=clock.now(),
        )

        await _tick_until(clock, lambda: len(bot.sent) >= 2)
        second_send_at = clock.now()
        assert in_window(second_send_at, tz, window)

        # Окно фиксировано, момент внутри окна случайный — разница между двумя
        # заходами должна укладываться в сутки ± ширину окна (1 час).
        gap = second_send_at - first_send_at
        assert 23 * 3600 <= gap <= 25 * 3600
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await responder.shutdown()
        await llm.aclose()

    assert len(calls) == 2
    assert len(bot.sent) == 2


# --- 18. spontaneous_job / _maybe_spontaneous: условия «просто так». ---


async def test_maybe_spontaneous_sends_once_and_bumps_counters(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Скучаю по гаражу."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)  # день, внутри окна spontaneous по умолчанию (10:00-22:00)
    responder = _make_responder(db, cfg, llm, bot, clock, rng=FixedRandom(0.0))
    try:
        await _drive(clock, responder._maybe_spontaneous())

        assert len(calls) == 1
        assert len(bot.sent) == 1
        assert bot.sent[0][1] == "Скучаю по гаражу."
        assert bot.sent[0][2] is None  # в поток, без reply

        tz = cfg.persona.timezone
        assert await db.get_state(day_key("ambient_count", clock.now(), tz)) == "1"
        assert await db.get_state("last_ambient_at") is not None
        from trolobot.timeutil import week_key

        assert await db.get_state(week_key("spontaneous_count", clock.now(), tz)) == "1"
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_maybe_spontaneous_skipped_when_chat_recently_active(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock, rng=FixedRandom(0.0))
    try:
        await _insert_message(
            db,
            tg_message_id=930,
            user_id=5,
            display_name="Дима",
            text="привет",
            created_at=DAY_NOW - 60,  # минуту назад — меньше min_quiet_hours (3ч)
        )

        await responder._maybe_spontaneous()

        assert calls == []
        assert bot.sent == []
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_maybe_spontaneous_skipped_when_week_budget_exhausted(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock, rng=FixedRandom(0.0))
    tz = cfg.persona.timezone
    try:
        from trolobot.timeutil import week_key

        await db.set_state(
            week_key("spontaneous_count", DAY_NOW, tz), str(cfg.behaviour.spontaneous.per_week)
        )

        await responder._maybe_spontaneous()

        assert calls == []
        assert bot.sent == []
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_maybe_spontaneous_skipped_outside_window(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(NIGHT_NOW)  # 3:00 — вне spontaneous.window (10:00-22:00)
    responder = _make_responder(db, cfg, llm, bot, clock, rng=FixedRandom(0.0))
    try:
        await responder._maybe_spontaneous()

        assert calls == []
        assert bot.sent == []
    finally:
        await responder.shutdown()
        await llm.aclose()


# --- 19. Сериализация генерации: второй ambient PASS ждёт лок, пока первый висит ---
# --- в LLM, и после освобождения лока перепроверка кулдауна режет его. ---


async def test_ambient_generation_is_serialized_and_recheck_blocks_second(
    db: Database,
) -> None:
    cfg = _config()
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        started.set()
        await release.wait()
        return _ok_response("Бывает.")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm = LLMClient(api_key="test-key", cfg_getter=lambda: cfg, db=db, http=http)
    bot = FakeBot()

    async def instant_sleep(_seconds: float) -> None:
        await asyncio.sleep(0)

    patterns = Patterns(cfg.filters, cfg.persona.name_triggers, BOT_USERNAME)
    responder = Responder(
        bot=bot,
        db=db,
        cfg_getter=lambda: cfg,
        llm=llm,
        patterns_getter=lambda: patterns,
        prompt_template=PROMPT_TEMPLATE,
        few_shot_getter=lambda: 'Дима: привет\n{"speak": true, "text": "И тебе."}',
        prompt_version=1,
        few_shot_version=1,
        rng=random.Random(0),
        chat_id=CHAT_ID,
        bot_user_id=BOT_USER_ID,
        clock=lambda: DAY_NOW,
        sleep=instant_sleep,
    )
    try:
        msg1 = _gate_message(tg_message_id=940, user_id=5, text="привет всем", created_at=DAY_NOW)
        msg2 = _gate_message(
            tg_message_id=941, user_id=6, text="и правда погода", created_at=DAY_NOW
        )

        task1: asyncio.Task[None] = asyncio.ensure_future(
            responder._handle_debounced(Trigger.AMBIENT, msg1, "Юзер1")
        )
        await started.wait()

        task2: asyncio.Task[None] = asyncio.ensure_future(
            responder._handle_debounced(Trigger.AMBIENT, msg2, "Юзер2")
        )
        # Дать task2 продвинуться до ожидания на _respond_lock (он занят task1).
        for _ in range(10):
            await asyncio.sleep(0)

        release.set()
        await task1
        await task2

        assert len(calls) == 1  # второй LLM не вызывался вовсе
        assert len(bot.sent) == 1

        summary = dict(await db.filter_log_summary(0))
        assert summary.get("send:recheck_ambient_cooldown") == 1
    finally:
        await responder.shutdown()
        await llm.aclose()
