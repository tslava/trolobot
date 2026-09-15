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
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from trolobot import filters as filters_module
from trolobot.config_models import Config
from trolobot.db import Database, MessageRow, PendingRow, PlaceRow
from trolobot.filters import FilterContext, FilterVerdict
from trolobot.gate_types import GateMessage, Trigger
from trolobot.judge import Judge
from trolobot.llm import LLMClient
from trolobot.patterns import Patterns
from trolobot.prompt import PLACES_NONE, SITUATION_LATE, SITUATION_MORNING
from trolobot.responder import Responder
from trolobot.stickers import Sticker
from trolobot.timeutil import day_key, in_window

CHAT_ID = -100123456
BOT_USER_ID = 999
BOT_USERNAME = "fedorbot"

PROMPT_TEMPLATE = (
    "Ты Фёдор, тебе {age} лет.\n"
    "Примеры:\n{few_shot}\n"
    "{context}\n{recent_replies}\n{places}\n{situation}"
)

# Слот {life} (CLAUDE.md, "события жизни") в обычном PROMPT_TEMPLATE намеренно
# отсутствует — большинство тестов этого файла его не касаются. Этот вариант
# нужен только тестам, которые проверяют, что life реально попадает в system.
PROMPT_TEMPLATE_WITH_LIFE = (
    "Ты Фёдор, тебе {age} лет.\n"
    "{life}\n"
    "Примеры:\n{few_shot}\n"
    "{context}\n{recent_replies}\n{places}\n{situation}"
)

# То же самое для слота {chat_memory} (CLAUDE.md, "долгая память чата").
PROMPT_TEMPLATE_WITH_CHAT_MEMORY = (
    "Ты Фёдор, тебе {age} лет.\n"
    "{chat_memory}\n"
    "{life}\n"
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
        self.sent_stickers: list[tuple[int, str, int | None]] = []
        self._next_id = 5000

    async def send_message(
        self, chat_id: int, text: str, *, reply_to_message_id: int | None = None
    ) -> SentMessage:
        self._next_id += 1
        self.sent.append((chat_id, text, reply_to_message_id))
        self.events.append(("send", str(chat_id), text))
        return SentMessage(message_id=self._next_id)

    async def send_sticker(
        self, chat_id: int, sticker: str, *, reply_to_message_id: int | None = None
    ) -> SentMessage:
        self._next_id += 1
        self.sent_stickers.append((chat_id, sticker, reply_to_message_id))
        self.events.append(("sticker", str(chat_id), sticker))
        return SentMessage(message_id=self._next_id)

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        self.events.append(("typing", str(chat_id), action))


_REAL_IO_STEP_SEC = 0.001  # реальная пауза, пока корутина в I/O (поток aiosqlite)


class FakeClock:
    """Виртуальное время: sleep() не продвигает его сам, ждёт явного будильника.

    ``run_until(task)`` крутит цикл «разбудить самого раннего ожидающего,
    продвинуть время до его момента, дать циклу событий тик» пока указанная
    задача не завершится. ``run_until_idle()`` — то же самое, но пока вообще
    не останется ни ожидающих, ни готовых к выполнению корутин.

    Почему это не так просто, как кажется: между тем, как ``_tick`` разбудил
    ожидающего (``event.set()``) и тем, как разбуженная корутина либо
    завершится, либо снова дойдёт до ``sleep()`` и зарегистрируется новым
    ожидающим, может потребоваться произвольное число переключений цикла
    событий — например, если по пути есть настоящий I/O (httpx с реальным
    транспортом, aiosqlite) или несколько вложенных ``await``. Один
    ``asyncio.sleep(0)`` после пробуждения даёт только один такой
    переключение. Раньше ``run_until`` тратил ровно один "раунд" бюджета
    ``max_rounds`` на каждый такой промежуточный тик, из-за чего число
    раундов, нужных для завершения одного и того же теста, зависело от
    того, сколько лишних переключений подбросит конкретная машина/версия
    Python — на CI иногда не укладывалось в 10000. ``run_until`` теперь не
    считает раундом ожидание "цикл ещё не догнал до следующего sleep()":
    после каждого продвижения времени он ждёт крошечными реальными паузами
    (см. ``_drain``), пока не появится хотя бы один новый ожидающий или
    задача не завершится — и это не расходует ``max_rounds``. Реальные паузы,
    а не ``asyncio.sleep(0)``: настоящая БД в тестах — aiosqlite, он работает
    в отдельном потоке, и пустые прокруты цикла событий потоку времени не дают
    (на CI это давало ложные «застои»). Если за несколько секунд реального
    времени ни ожидающих не появилось, ни задача не завершилась — это
    настоящий застой, и ``run_until`` падает с сообщением, какие задачи живы. Отдельно
    исправлен off-by-one: раньше, если задача завершалась ровно на
    последнем тике бюджета, цикл ``for`` заканчивался без повторной
    проверки ``task.done()`` и падал, хотя задача уже была done —
    теперь это перепроверяется и после цикла.
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
        else:
            # Ожидающих нет: значит корутина сейчас в настоящем I/O (aiosqlite
            # работает в отдельном потоке). Пустой sleep(0) потоку времени не даёт,
            # нужна крошечная реальная пауза.
            await asyncio.sleep(_REAL_IO_STEP_SEC)

    async def _drain(self, task: asyncio.Task[None], max_wait_sec: float = 5.0) -> bool:
        """Даёт корутине догнать после пробуждения: ждёт реальным временем
        (шагами по _REAL_IO_STEP_SEC), пока не появится новый ожидающий или задача
        не завершится. Время FakeClock не двигает. Возвращает True, если дождались;
        False — если за max_wait_sec реального времени ничего не произошло:
        это настоящий застой, а не отставание планировщика или поток aiosqlite."""
        deadline = time.monotonic() + max_wait_sec
        while time.monotonic() < deadline:
            if self._waiters or task.done():
                return True
            await asyncio.sleep(_REAL_IO_STEP_SEC)
        return bool(self._waiters) or task.done()

    async def run_until(self, task: asyncio.Task[None], max_rounds: int = 10_000) -> None:
        for _ in range(max_rounds):
            if task.done():
                return
            if not self._waiters:
                caught_up = await self._drain(task)
                if task.done():
                    return
                if not caught_up:
                    alive = sorted(
                        repr(t) for t in asyncio.all_tasks() if t is not task and not t.done()
                    )
                    raise AssertionError(
                        "FakeClock.run_until: нет ни ожидающих, ни завершения задачи "
                        f"за 5 с реального ожидания; задача {task!r} "
                        f"жива; другие живые задачи: {alive}"
                    )
            await self._tick()
        if task.done():
            return
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


async def _drive[T](clock: FakeClock, coro: Awaitable[T]) -> asyncio.Task[T]:
    """Заворачивает корутину в Task и прогоняет её до конца через FakeClock.

    Обобщённая по возвращаемому типу — ``announce_life``/``say`` (CLAUDE.md, "события
    жизни") возвращают ``SendOutcome``, а не ``None``, как обычные хендлеры этого
    файла; ``FakeClock.run_until`` при этом сам типизирован под ``Task[None]``, потому
    что использует только ``task.done()`` и не читает результат — приведение типа
    здесь безопасно ровно поэтому.
    """
    task: asyncio.Task[T] = asyncio.ensure_future(coro)
    await clock.run_until(task)  # type: ignore[arg-type]  # run_until не читает .result()
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


def _varied_responses(*texts: str) -> Handler:
    """Handler, отдающий на каждый вызов свой текст (последний повторяется дальше).

    Нужен там, где тест ждёт несколько ответов подряд: одинаковый текст второй раз
    теперь режется выходным фильтром (``dedup:jaccard``), потому что стадия ``dedup``
    входит в ``filters.enforce_stages`` и режет даже при ``shadow: true``
    (CLAUDE.md, "меньше и разнообразнее", мера 4).
    """
    state = {"index": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        index = min(state["index"], len(texts) - 1)
        state["index"] += 1
        return _ok_response(texts[index])

    return handler


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


def _checkin_response(text: str, reply_to: int | None) -> httpx.Response:
    """Ответ модели для триггера "checkin" (CLAUDE.md, "вернулся проверить") —
    JSON с полем reply_to, как реально возвращает JSON_REMINDER_CHECKIN."""
    content = json.dumps({"speak": True, "text": text, "reply_to": reply_to}, ensure_ascii=False)
    body = {
        "choices": [{"message": {"content": content}}],
        "usage": {"cost": 0.001, "prompt_tokens": 10, "completion_tokens": 5},
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


class MinRandom:
    """Подделка random.Random, детерминированно отдающая минимум диапазона: .random()
    -> 0.0 (всегда первый, самый быстрый бакет задержки), .randint(lo, hi) -> lo,
    .uniform(lo, hi) -> lo. Нужна тестам кулдауна-как-задержки (earliest), чтобы
    базовая (без сдвига) задержка была минимально возможной и надёжно проверяла
    срабатывание/несрабатывание сдвига due_at."""

    def random(self) -> float:
        return 0.0

    def randint(self, lo: int, hi: int) -> int:
        return lo

    def uniform(self, lo: float, hi: float) -> float:
        return lo


class MaxRandom:
    """Подделка random.Random, детерминированно отдающая максимум диапазона: .random()
    -> 0.999999 (всегда последний, самый долгий бакет задержки), .randint(lo, hi) -> hi,
    .uniform(lo, hi) -> lo. Нужна тестам потолка задержки горячего окна
    (``hot_window.mention_max_delay_sec``), чтобы базовая (без потолка) задержка была
    заведомо больше потолка и надёжно проверяла срабатывание/несрабатывание сдвига."""

    def random(self) -> float:
        return 0.999999

    def randint(self, lo: int, hi: int) -> int:
        return hi

    def uniform(self, lo: float, hi: float) -> float:
        return lo


class FakeStickerChooser:
    """Подделка ``StickerChooser``: без реального LLM-вызова — тесты этого модуля
    проверяют интеграцию (когда чузер вызывается и что происходит после), а не сам
    выбор моделью (это уже покрыто test_stickers.py)."""

    def __init__(self, sticker: Sticker | None = None) -> None:
        self.sticker = sticker
        self.calls: list[dict[str, object]] = []

    async def choose(
        self, *, reply_text: str, trigger_text: str, exclude_ids: set[int], now: int
    ) -> Sticker | None:
        self.calls.append(
            {
                "reply_text": reply_text,
                "trigger_text": trigger_text,
                "exclude_ids": exclude_ids,
                "now": now,
            }
        )
        return self.sticker


class FakePromptStore:
    """Подделка stores.PromptStore: подмена системного промпта/few-shot и их версий.

    Отдаёт фиксированные значения, но через методы (не через поля Responder) —
    так тесты проверяют, что Responder действительно читает prompt_store на
    каждом _respond, а не кэширует его в конструкторе.
    """

    def __init__(
        self,
        *,
        prompt: str = PROMPT_TEMPLATE,
        few_shot: str = 'Дима: привет\n{"speak": true, "text": "И тебе."}',
        prompt_version: int = 1,
        few_shot_version: int = 1,
    ) -> None:
        self.prompt = prompt
        self.few_shot = few_shot
        self.prompt_version_value = prompt_version
        self.few_shot_version_value = few_shot_version

    def system_prompt(self) -> str:
        return self.prompt

    def prompt_version(self) -> int:
        return self.prompt_version_value

    def few_shot_text(self) -> str:
        return self.few_shot

    def few_shot_version(self) -> int:
        return self.few_shot_version_value


def _make_responder(
    db_: Database,
    cfg: Config,
    llm: LLMClient,
    bot: FakeBot,
    clock: FakeClock,
    *,
    seed: int = 0,
    rng: random.Random | FixedRandom | MinRandom | None = None,
    judge: Judge | None = None,
    sticker_chooser: FakeStickerChooser | None = None,
    prompt_store: FakePromptStore | None = None,
    followup: FakeFollowup | None = None,
) -> Responder:
    patterns = Patterns(cfg.filters, cfg.persona.name_triggers, BOT_USERNAME)
    return Responder(
        bot=bot,
        db=db_,
        cfg_getter=lambda: cfg,
        llm=llm,
        judge=judge,
        sticker_chooser=sticker_chooser,  # type: ignore[arg-type]
        followup=followup,  # type: ignore[arg-type]  # FakeFollowup повторяет только .check
        patterns_getter=lambda: patterns,
        prompt_store=prompt_store if prompt_store is not None else FakePromptStore(),
        rng=rng if rng is not None else random.Random(seed),  # type: ignore[arg-type]
        chat_id=CHAT_ID,
        bot_user_id=BOT_USER_ID,
        clock=clock.now,
        sleep=clock.sleep,
    )


class FakeFollowup:
    """Подделка ``FollowupChecker`` для checkin (CLAUDE.md, "меньше и разнообразнее",
    мера 3): только ``check``, без LLM. Пишет аргументы вызовов — тестам нужно
    убедиться, что проверяется именно выбранная строка, а не весь список."""

    def __init__(self, addressed: bool) -> None:
        self.addressed = addressed
        self.calls: list[dict[str, object]] = []

    async def check(
        self,
        *,
        text: str,
        display_name: str,
        context_rows: Sequence[MessageRow],
        recent_replies: Sequence[str],
        now: int,
    ) -> bool:
        self.calls.append(
            {
                "text": text,
                "display_name": display_name,
                "context_rows": list(context_rows),
                "recent_replies": list(recent_replies),
                "now": now,
            }
        )
        return self.addressed


async def _noop_open_hot_window(_cfg: Config, _now: int) -> None:
    """Подделка ``Responder._maybe_open_hot_window`` — для тестов, которые
    проверяют состояние счётчиков ДО того, как общий хвост ``_generate_and_send``
    откроет/продлит горячее окно (CLAUDE.md, "внимание как у живого человека")."""


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


# --- 3b. Кулдаун обращения (решение владельца) не отбрасывает ответ, а сдвигает ---
# --- due_at не раньше earliest = max(last_mention_reply_at + chat_cooldown, ---
# --- last_mention_reply_at:<user> + user_cooldown). ---


async def test_mention_without_active_cooldown_uses_normal_bucket_delay(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock, rng=MinRandom())
    try:
        # Нет ни last_mention_reply_at, ни last_mention_reply_at:<user> — earliest
        # мал (0 + cooldown), due не сдвигается, задержка как обычно (первый бакет).
        msg = _gate_message(tg_message_id=10, user_id=5, text="фёдор, как сам?", created_at=DAY_NOW)
        await responder.on_gate_pass(msg, Trigger.NAME, "Дима")
        assert responder._debounce_task is not None
        await clock.run_until(responder._debounce_task)

        rows = await db.load_pending()
        assert len(rows) == 1
        fast_lo = cfg.behaviour.reply_delay_buckets[0].range_sec[0]
        assert rows[0].due_at - rows[0].created_at == fast_lo
        assert calls == []
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_second_mention_after_reply_delayed_until_chat_cooldown_elapses(
    db: Database,
) -> None:
    """Второе обращение через 20с после отправленного ответа при активном
    mention_chat_cooldown_sec=90: новый pending получает due >= last_reply_at + 90
    (а не обычную короткую задержку), и при срабатывании ответ всё равно
    отправляется — кулдаун больше не отбрасывает обращение, только откладывает."""
    cfg = _config()
    assert cfg.behaviour.mention_chat_cooldown_sec == 90
    # Пауза между репликами (CLAUDE.md, "меньше и разнообразнее", мера 2) отложила бы
    # этот pending ещё раз и проверяется отдельными тестами — здесь речь именно о
    # кулдауне обращения, поэтому пауза выключена.
    cfg.behaviour.min_gap_sec = 0
    llm, calls = _make_llm(cfg, db, _varied_responses("Ответ1.", "Ответ2."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock, rng=MinRandom())
    try:
        msg1 = _gate_message(tg_message_id=10, user_id=5, text="фёдор, привет", created_at=DAY_NOW)
        await responder.on_gate_pass(msg1, Trigger.NAME, "Дима")
        assert responder._debounce_task is not None
        await clock.run_until(responder._debounce_task)

        pending_rows = await db.load_pending()
        assert len(pending_rows) == 1
        pending_id = pending_rows[0].id
        await clock.run_until(responder._pending_tasks[pending_id])

        assert len(bot.sent) == 1
        last_reply_raw = await db.get_state("last_mention_reply_at")
        assert last_reply_raw is not None
        last_reply_at = int(last_reply_raw)

        # Второе обращение — от другого человека, 20с после отправленного ответа.
        clock.value = float(last_reply_at + 20)
        msg2 = _gate_message(
            tg_message_id=11, user_id=6, text="федя, ты где", created_at=last_reply_at + 20
        )
        await responder.on_gate_pass(msg2, Trigger.NAME, "Оля")
        assert responder._debounce_task is not None
        await clock.run_until(responder._debounce_task)

        pending_rows_2 = await db.load_pending()
        assert len(pending_rows_2) == 1
        assert pending_rows_2[0].due_at >= last_reply_at + cfg.behaviour.mention_chat_cooldown_sec

        task2 = responder._pending_tasks[pending_rows_2[0].id]
        await clock.run_until(task2)
        assert len(bot.sent) == 2  # кулдаун отложил, но не отбросил
        assert len(calls) == 2
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_collapse_does_not_move_due_earlier_than_cooldown_floor(db: Database) -> None:
    """Схлопывание (update_pending_due) тоже подчиняется earliest: даже быстрый
    бакет не может утащить due_at раньше конца активного кулдауна по чату."""
    cfg = _config()
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock, rng=MinRandom())
    try:
        # Симулируем недавний ответ на обращение — кулдаун по чату ещё активен.
        await db.set_state("last_mention_reply_at", str(DAY_NOW - 10))
        earliest = DAY_NOW - 10 + cfg.behaviour.mention_chat_cooldown_sec

        msg1 = _gate_message(tg_message_id=20, user_id=5, text="фёдор, ты тут?", created_at=DAY_NOW)
        await responder._handle_debounced(Trigger.NAME, msg1, "Дима")

        pending_after_1 = await db.load_pending()
        assert len(pending_after_1) == 1
        pending_id = pending_after_1[0].id
        assert pending_after_1[0].due_at >= earliest

        msg2 = _gate_message(
            tg_message_id=21, user_id=6, text="федя, ты где", created_at=DAY_NOW + 2
        )
        await responder._handle_debounced(Trigger.NAME, msg2, "Оля")

        pending_after_2 = await db.load_pending()
        assert len(pending_after_2) == 1
        assert pending_after_2[0].id == pending_id  # схлопнулось, не вторая задача
        assert pending_after_2[0].due_at >= earliest
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
    llm, _calls = _make_llm(cfg, db, _varied_responses("Ок.", "Ага.", "Понял."))
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


# --- 11b. Обращение: situation называет того, кто реально обратился, а не просто ---
# --- "молчание" (живой баг: 30 сообщений контекста, модель отвечает на самое ---
# --- заметное, а не на того, кто позвал). ---


async def test_mention_reply_states_who_addressed_it(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("И тебе привет."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await _insert_message(
            db,
            tg_message_id=500,
            user_id=5,
            display_name="Дима",
            text="@fedorbot как сам?",
            created_at=DAY_NOW,
        )
        msg = _gate_message(
            tg_message_id=500, user_id=5, text="@fedorbot как сам?", created_at=DAY_NOW
        )
        await responder.on_gate_pass(msg, Trigger.MENTION, "Дима")
        assert responder._debounce_task is not None
        await clock.run_until(responder._debounce_task)

        pending_id = (await db.load_pending())[0].id
        await clock.run_until(responder._pending_tasks[pending_id])

        assert len(calls) == 1
        payload = _payload(calls[0])
        user_content = payload["messages"][1]["content"]  # type: ignore[index]
        assert "К тебе сейчас обратился Дима: «@fedorbot как сам?»" in user_content
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_ambient_reply_has_no_addressed_situation_line(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Бывает."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await _insert_message(
            db,
            tg_message_id=501,
            user_id=5,
            display_name="Дима",
            text="привет всем",
            created_at=DAY_NOW,
        )
        msg = _gate_message(tg_message_id=501, user_id=5, text="привет всем", created_at=DAY_NOW)
        await responder.on_gate_pass(msg, Trigger.AMBIENT, "Дима")
        assert responder._debounce_task is not None
        await clock.run_until(responder._debounce_task)

        assert len(calls) == 1
        payload = _payload(calls[0])
        user_content = payload["messages"][1]["content"]  # type: ignore[index]
        assert "К тебе сейчас обратился" not in user_content
        assert "К тебе обратились:" not in user_content
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_mention_late_reply_has_both_addressed_and_late_situation(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Задумался о своём."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        pending_id = await db.insert_pending(
            trigger_tg_message_id=502,
            user_id=5,
            trigger="mention",
            due_at=DAY_NOW,
            created_at=DAY_NOW - 3600,
        )
        # Обращение поставлено в этом же процессе -> _pending_info хранит пару.
        responder._pending_info[pending_id] = [("Дима", "фёдор, ты там?")]
        row = PendingRow(
            id=pending_id,
            trigger_tg_message_id=502,
            user_id=5,
            trigger="mention",
            due_at=DAY_NOW,
            created_at=DAY_NOW - 3600,
            done_at=None,
        )

        await _drive(clock, responder._fire_pending(row))

        assert len(calls) == 1
        payload = _payload(calls[0])
        user_content = payload["messages"][1]["content"]  # type: ignore[index]
        assert "К тебе сейчас обратился Дима: «фёдор, ты там?»" in user_content
        assert SITUATION_LATE in user_content
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_restored_pending_takes_address_from_messages_after_restart(db: Database) -> None:
    """После рестарта процесса _pending_info пуст -- имя и текст обращения
    восстанавливаются из messages (message_by_tg_id по trigger_tg_message_id)."""
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Ну да."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await _insert_message(
            db,
            tg_message_id=503,
            user_id=5,
            display_name="Дима",
            text="фёдор, как сам?",
            created_at=DAY_NOW - 30,
        )
        pending_id = await db.insert_pending(
            trigger_tg_message_id=503,
            user_id=5,
            trigger="mention",
            due_at=DAY_NOW,
            created_at=DAY_NOW - 30,
        )
        row = PendingRow(
            id=pending_id,
            trigger_tg_message_id=503,
            user_id=5,
            trigger="mention",
            due_at=DAY_NOW,
            created_at=DAY_NOW - 30,
            done_at=None,
        )
        # Ни одной записи в _pending_info -- как будто процесс перезапустился между
        # постановкой pending и его срабатыванием.
        assert pending_id not in responder._pending_info

        await _drive(clock, responder._fire_pending(row))

        assert len(calls) == 1
        payload = _payload(calls[0])
        user_content = payload["messages"][1]["content"]  # type: ignore[index]
        assert "К тебе сейчас обратился Дима: «фёдор, как сам?»" in user_content
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_collapsed_mentions_list_all_addressers_in_situation(db: Database) -> None:
    """Схлопывание дебаунс-буфера дописывает новое обращение к уже накопленным для
    этого pending, а не заменяет его -- situation перечисляет всех обратившихся."""
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Всем привет."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        msg1 = _gate_message(
            tg_message_id=520, user_id=5, text="фёдор, ты тут?", created_at=DAY_NOW
        )
        await responder._handle_debounced(Trigger.NAME, msg1, "Дима")

        msg2 = _gate_message(
            tg_message_id=521, user_id=6, text="федя, ты где", created_at=DAY_NOW + 2
        )
        await responder._handle_debounced(Trigger.NAME, msg2, "Оля")

        pending_id = (await db.load_pending())[0].id
        assert responder._pending_info[pending_id] == [
            ("Дима", "фёдор, ты тут?"),
            ("Оля", "федя, ты где"),
        ]
        await clock.run_until(responder._pending_tasks[pending_id])

        assert len(calls) == 1
        payload = _payload(calls[0])
        user_content = payload["messages"][1]["content"]  # type: ignore[index]
        assert "К тебе обратились:" in user_content
        assert "- Дима: «фёдор, ты тут?»" in user_content
        assert "- Оля: «федя, ты где»" in user_content
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_restored_pending_collects_multiple_addresses_from_messages(db: Database) -> None:
    """Восстановление после рестарта тоже собирает несколько обращений, не только
    исходный триггер -- любое сообщение-реплай на бота или mention/name_trigger,
    начиная с created_at постановки pending, тоже считается обращением."""
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Всем привет."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await _insert_message(
            db,
            tg_message_id=530,
            user_id=5,
            display_name="Дима",
            text="@fedorbot как сам?",
            created_at=DAY_NOW - 20,
        )
        await _insert_message(
            db,
            tg_message_id=531,
            user_id=6,
            display_name="Оля",
            text="федя, ты тут?",
            created_at=DAY_NOW - 10,
        )
        pending_id = await db.insert_pending(
            trigger_tg_message_id=530,
            user_id=5,
            trigger="mention",
            due_at=DAY_NOW,
            created_at=DAY_NOW - 20,
        )
        row = PendingRow(
            id=pending_id,
            trigger_tg_message_id=530,
            user_id=5,
            trigger="mention",
            due_at=DAY_NOW,
            created_at=DAY_NOW - 20,
            done_at=None,
        )

        await _drive(clock, responder._fire_pending(row))

        assert len(calls) == 1
        payload = _payload(calls[0])
        user_content = payload["messages"][1]["content"]  # type: ignore[index]
        assert "К тебе обратились:" in user_content
        assert "- Дима: «@fedorbot как сам?»" in user_content
        assert "- Оля: «федя, ты тут?»" in user_content
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_collect_addressed_items_caps_at_five(db: Database) -> None:
    cfg = _config()
    llm, _calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await _insert_message(
            db,
            tg_message_id=540,
            user_id=5,
            display_name="Триггер",
            text="федя, драсте",
            created_at=DAY_NOW - 100,
        )
        for i in range(6):
            await _insert_message(
                db,
                tg_message_id=541 + i,
                user_id=10 + i,
                display_name=f"Юзер{i}",
                text="федя, привет",
                created_at=DAY_NOW - 90 + i,
            )
        row = PendingRow(
            id=999999,
            trigger_tg_message_id=540,
            user_id=5,
            trigger="mention",
            due_at=DAY_NOW,
            created_at=DAY_NOW - 100,
            done_at=None,
        )

        items = await responder._collect_addressed_items(row)

        assert len(items) == 5
        names = [name for name, _ in items]
        assert "Триггер" not in names  # самый старый -- вытеснен потолком
        assert "Юзер0" not in names
        assert names == ["Юзер1", "Юзер2", "Юзер3", "Юзер4", "Юзер5"]
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

    async def fake_check_output(text: str, ctx: object, judge: object = None) -> FilterVerdict:
        return FilterVerdict(ok=False, reason="regex:length", reasons=("regex:length",))

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


# --- 15. _fire_pending переживает ошибку в db.recent_messages: залогировано, pending ---
# --- НЕ помечен done (необработанное исключение внутри генерации — не filter_log- ---
# --- исход), процесс жив, следующий pending обрабатывается нормально. ---


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
        # pending НЕ помечен done: упало необработанное исключение внутри генерации
        # (до вызова модели), а не filter_log-исход — restore_pending на следующем
        # старте должен снова его увидеть и повторить попытку (см. тест ниже про
        # выживание при исключении именно в самом вызове LLM).
        pending_after_failure = await db.load_pending()
        assert [row.id for row in pending_after_failure] == [pending_id_1]
        assert pending_after_failure[0].done_at is None

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


# --- 15b. Инцидент: mark_pending_done раньше ставился в начале обработки pending — ---
# --- SIGTERM во время вызова модели терял ответ навсегда, restore_pending его уже ---
# --- не видел (уже done). Теперь done не ставится при необработанном исключении ---
# --- внутри самого llm.call: pending остаётся в БД, следующий restore_pending ---
# --- (эмулирует рестарт процесса) подхватывает его и повторный вызов LLM успешен. ---


async def test_pending_stays_undone_on_llm_exception_and_retries_after_restore(
    db: Database,
) -> None:
    cfg = _config()
    state = {"raised": False}

    def handler(_request: httpx.Request) -> httpx.Response:
        if not state["raised"]:
            state["raised"] = True
            # Не httpx.HTTPError/TimeoutException -- LLMClient это не перехватывает и
            # не превращает в LLMError, ровно как "транспортный обрыв во время SIGTERM".
            raise RuntimeError("transport aborted (simulated SIGTERM)")
        return _ok_response("Ожил после рестарта.")

    llm, calls = _make_llm(cfg, db, handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        pending_id = await db.insert_pending(
            trigger_tg_message_id=950,
            user_id=5,
            trigger="mention",
            due_at=DAY_NOW,
            created_at=DAY_NOW - 60,
        )
        row = PendingRow(
            id=pending_id,
            trigger_tg_message_id=950,
            user_id=5,
            trigger="mention",
            due_at=DAY_NOW,
            created_at=DAY_NOW - 60,
            done_at=None,
        )

        await responder._fire_pending(row)

        assert bot.sent == []
        assert len(calls) == 1
        pending_after_crash = await db.load_pending()
        assert [r.id for r in pending_after_crash] == [pending_id]
        assert pending_after_crash[0].done_at is None

        # Эмулируем рестарт процесса: restore_pending на новом старте снова видит
        # незавершённый pending (просрочка мала -- сразу таймер, не send:restart).
        await responder.restore_pending()
        assert pending_id in responder._pending_tasks

        await clock.run_until(responder._pending_tasks[pending_id])

        assert len(calls) == 2
        assert len(bot.sent) == 1
        assert bot.sent[0][1] == "Ожил после рестарта."
        assert await db.load_pending() == []
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_pending_marked_done_after_speak_false_final_decision(db: Database) -> None:
    """speak=false -- окончательное решение о молчании (filter_log-исход llm:silent),
    поэтому pending помечается done."""
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _silent_response())
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        pending_id = await db.insert_pending(
            trigger_tg_message_id=970,
            user_id=5,
            trigger="mention",
            due_at=DAY_NOW,
            created_at=DAY_NOW - 60,
        )
        row = PendingRow(
            id=pending_id,
            trigger_tg_message_id=970,
            user_id=5,
            trigger="mention",
            due_at=DAY_NOW,
            created_at=DAY_NOW - 60,
            done_at=None,
        )

        await responder._fire_pending(row)

        assert bot.sent == []
        assert len(calls) == 1
        assert await db.load_pending() == []
        summary = dict(await db.filter_log_summary(0))
        assert summary.get("llm:silent") == 1
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_pending_marked_done_after_successful_send(db: Database) -> None:
    """Успешная отправка -- pending помечается done после insert_bot_reply, то есть
    после того как ответ уже появился в bot_replies."""
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Ну привет."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        pending_id = await db.insert_pending(
            trigger_tg_message_id=980,
            user_id=5,
            trigger="mention",
            due_at=DAY_NOW,
            created_at=DAY_NOW - 60,
        )
        row = PendingRow(
            id=pending_id,
            trigger_tg_message_id=980,
            user_id=5,
            trigger="mention",
            due_at=DAY_NOW,
            created_at=DAY_NOW - 60,
            done_at=None,
        )

        await _drive(clock, responder._fire_pending(row))

        assert len(calls) == 1
        assert len(bot.sent) == 1
        assert (await db.recent_bot_replies(5)) == ["Ну привет."]
        assert await db.load_pending() == []
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_shutdown_waits_for_inflight_generation_before_cancelling(db: Database) -> None:
    """shutdown() во время висящего вызова модели (_respond_lock занят) не отменяет
    генерацию сразу -- ждёт (asyncio.wait_for на лок), пока LLM-подделка, зависшая на
    Event, не будет отпущена; ответ должен успеть уйти."""
    cfg = _config()
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        await release.wait()
        return _ok_response("Договорил после shutdown.")

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
        prompt_store=FakePromptStore(),
        rng=random.Random(0),
        chat_id=CHAT_ID,
        bot_user_id=BOT_USER_ID,
        clock=lambda: DAY_NOW,
        sleep=instant_sleep,
    )
    try:
        msg = _gate_message(tg_message_id=990, user_id=5, text="привет всем", created_at=DAY_NOW)
        gen_task: asyncio.Task[None] = asyncio.ensure_future(
            responder._handle_debounced(Trigger.AMBIENT, msg, "Юзер1")
        )
        await started.wait()  # генерация внутри llm.call, держит _respond_lock

        assert responder._respond_lock.locked()
        shutdown_task: asyncio.Task[None] = asyncio.ensure_future(responder.shutdown())
        for _ in range(5):
            await asyncio.sleep(0)
        assert not shutdown_task.done()  # ждёт лок, не отменяет пока LLM не ответила

        release.set()  # LLM "отвечает" -- имитация того, что процесс дожил до ответа
        await gen_task
        await shutdown_task

        assert len(bot.sent) == 1
        assert bot.sent[0][1] == "Договорил после shutdown."
    finally:
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
    llm, calls = _make_llm(cfg, db, _varied_responses("Доброе утро.", "Всем привет."))
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


# --- 18b. checkin_job / _maybe_checkin: «вернулся проверить» (CLAUDE.md, "внимание ---
# --- как у живого человека"). ---


async def _insert_bot_reply(db_: Database, *, created_at: int, tg_message_id: int = 1) -> None:
    await db_.insert_bot_reply(
        tg_message_id=tg_message_id,
        reply_to_tg_message_id=None,
        trigger="ambient",
        trigger_tg_message_id=None,
        text="реплика Фёдора",
        prompt_version=1,
        few_shot_version=1,
        delay_sec=0,
        created_at=created_at,
    )


async def test_maybe_checkin_schedules_due_from_closed_hot_window(db: Database) -> None:
    """due считается от последнего известного (уже закрытого) hot_until, а не от now."""
    cfg = _config()
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock, rng=MinRandom())
    try:
        hot_until = DAY_NOW - 10  # окно уже закрылось
        await db.set_state("hot_until", str(hot_until))
        await _insert_bot_reply(db, created_at=hot_until)

        await responder._maybe_checkin()

        assert calls == []
        after_min_lo = cfg.behaviour.checkin.after_min[0]
        assert await db.get_state("checkin_due") == str(hot_until + after_min_lo * 60)
        assert await db.get_state("checkin_due_hot_until") == str(hot_until)
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_maybe_checkin_skipped_while_hot_window_still_open(db: Database) -> None:
    """Телефон ещё в руках — followup справляется, отдельная проверка не идёт,
    checkin_due вообще не вычисляется."""
    cfg = _config()
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await db.set_state("hot_until", str(DAY_NOW + 900))
        await _insert_bot_reply(db, created_at=DAY_NOW - 100)

        await responder._maybe_checkin()

        assert calls == []
        assert await db.get_state("checkin_due") is None
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_maybe_checkin_no_new_messages_reschedules_without_llm_call(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock, rng=MinRandom())
    try:
        last_reply_at = DAY_NOW - 20000
        await _insert_bot_reply(db, created_at=last_reply_at)
        # hot_window не выставлялся вовсе -> due_ref = last_reply_at (см. докстринг
        # _maybe_checkin); due уже наступил.
        await db.set_state("checkin_due", str(DAY_NOW - 10))
        await db.set_state("checkin_due_hot_until", str(last_reply_at))

        await responder._maybe_checkin()

        assert calls == []
        assert bot.sent == []
        after_min_lo = cfg.behaviour.checkin.after_min[0]
        assert await db.get_state("checkin_last_at") == str(DAY_NOW)
        assert await db.get_state("checkin_due") == str(DAY_NOW + after_min_lo * 60)
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_maybe_checkin_sends_reply_to_selected_message(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _checkin_response("Бывает, гараж зовёт.", 2))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock, rng=MinRandom())
    try:
        last_reply_at = DAY_NOW - 20000
        await _insert_bot_reply(db, created_at=last_reply_at)
        await _insert_message(
            db,
            tg_message_id=701,
            user_id=5,
            display_name="Дима",
            text="как сам, дед?",
            created_at=last_reply_at + 10,
        )
        await _insert_message(
            db,
            tg_message_id=702,
            user_id=6,
            display_name="Аня",
            text="федя, ты живой вообще?",
            created_at=last_reply_at + 20,
        )
        await db.set_state("checkin_due", str(DAY_NOW - 10))
        await db.set_state("checkin_due_hot_until", str(last_reply_at))

        await _drive(clock, responder._maybe_checkin())

        assert len(calls) == 1
        assert len(bot.sent) == 1
        assert bot.sent[0][1] == "Бывает, гараж зовёт."
        assert bot.sent[0][2] == 702  # reply_to=2 -> второе сообщение (Аня, tg 702)

        summary = dict(await db.filter_log_summary(0))
        assert summary.get("send:checkin") == 1
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_maybe_checkin_speak_false_logs_silent_and_reschedules(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _silent_response())
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock, rng=MinRandom())
    try:
        last_reply_at = DAY_NOW - 20000
        await _insert_bot_reply(db, created_at=last_reply_at)
        await _insert_message(
            db,
            tg_message_id=800,
            user_id=5,
            display_name="Дима",
            text="привет",
            created_at=last_reply_at + 10,
        )
        await db.set_state("checkin_due", str(DAY_NOW - 10))
        await db.set_state("checkin_due_hot_until", str(last_reply_at))

        await responder._maybe_checkin()

        assert len(calls) == 1
        assert bot.sent == []
        summary = dict(await db.filter_log_summary(0))
        assert summary.get("llm:silent") == 1
        after_min_lo = cfg.behaviour.checkin.after_min[0]
        assert await db.get_state("checkin_last_at") == str(DAY_NOW)
        assert await db.get_state("checkin_due") == str(DAY_NOW + after_min_lo * 60)
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_maybe_checkin_dead_topic_deletes_due_and_skips(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        topic_max_hours = cfg.behaviour.checkin.topic_max_hours
        last_reply_at = DAY_NOW - (topic_max_hours + 1) * 3600
        await _insert_bot_reply(db, created_at=last_reply_at)
        await db.set_state("checkin_due", str(DAY_NOW - 10))
        await db.set_state("checkin_due_hot_until", str(last_reply_at))

        await responder._maybe_checkin()

        assert calls == []
        assert await db.get_state("checkin_due") is None
        assert await db.get_state("checkin_due_hot_until") is None
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_maybe_checkin_blocked_by_panic(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await db.set_state("panic", "1")
        await _insert_bot_reply(db, created_at=DAY_NOW - 20000)

        await responder._maybe_checkin()

        assert calls == []
        assert await db.get_state("checkin_due") is None
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_maybe_checkin_blocked_by_stop(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await db.set_state("stop_until", str(DAY_NOW + 1000))
        await _insert_bot_reply(db, created_at=DAY_NOW - 20000)

        await responder._maybe_checkin()

        assert calls == []
        assert await db.get_state("checkin_due") is None
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_maybe_checkin_blocked_during_quiet_window(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(NIGHT_NOW)  # 3:00 — внутри quiet_window по умолчанию (02:00-07:00)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await _insert_bot_reply(db, created_at=NIGHT_NOW - 20000)

        await responder._maybe_checkin()

        assert calls == []
        assert await db.get_state("checkin_due") is None
    finally:
        await responder.shutdown()
        await llm.aclose()


# --- 19. Сериализация генерации: второй ambient PASS ждёт лок, пока первый висит ---
# --- в LLM, и после освобождения лока перепроверка кулдауна режет его. ---


async def test_ambient_generation_is_serialized_and_recheck_blocks_second(
    db: Database,
) -> None:
    cfg = _config()
    # Горячее окно выключено намеренно: этот тест про сериализацию генерации и
    # перепроверку дневного бюджета/кулдауна ambient, а не про горячее окно —
    # с ним включённым первая отправка открывала бы окно и снимала кулдаун для
    # второй (CLAUDE.md, "внимание как у живого человека": окно открывается
    # после любой отправки), что смешивало бы два независимых поведения в одном
    # тесте. Горячее окно проверяется отдельными тестами ниже.
    cfg.behaviour.hot_window.enabled = False
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
        prompt_store=FakePromptStore(),
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


# --- 20. FilterContext собирается полностью: trigger_text из pending, muted_names ---
# --- через db.display_names(muted_user_ids()), bot_names/system_prompt из Responder. ---


async def test_filter_context_wired_with_trigger_text_and_muted_names(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config()
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("Бывает, дед."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)

    # Замьюченный автор: его последнее имя в messages должно попасть в muted_names.
    await _insert_message(
        db,
        tg_message_id=500,
        user_id=7,
        display_name="Дима",
        text="фёдор, привет",
        created_at=DAY_NOW,
    )
    conn = db._conn
    assert conn is not None
    await conn.execute(
        "INSERT INTO muted_users (user_id, display_name, muted_by, created_at) VALUES (?, ?, ?, ?)",
        (7, "Дима", 1, DAY_NOW),
    )
    await conn.commit()

    real_check_output = filters_module.check_output
    captured: dict[str, FilterContext] = {}

    async def spy_check_output(
        text: str, ctx: FilterContext, judge: object = None
    ) -> FilterVerdict:
        captured["ctx"] = ctx
        return await real_check_output(text, ctx, judge)  # type: ignore[arg-type]

    monkeypatch.setattr(filters_module, "check_output", spy_check_output)

    try:
        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.MENTION,
                trigger_msg_id=500,
                user_id=7,
                situation="",
                delay_sec=5,
                trigger_text="фёдор, привет",
            ),
        )

        ctx = captured["ctx"]
        assert ctx.trigger_text == "фёдор, привет"
        assert ctx.system_prompt == PROMPT_TEMPLATE
        assert ctx.muted_names == ["Дима"]
        # patterns_getter() -> Patterns передаётся в FilterContext, а не только в
        # cfg.filters: одна компиляция regex на весь check_output, не пересборка.
        assert isinstance(ctx.patterns, Patterns)
        assert ctx.bot_names[0] == cfg.persona.name
        assert ctx.bot_names[1] == cfg.persona.display_name
        assert "Дима" in ctx.participant_names
        assert len(ctx.recent_replies) <= 50
    finally:
        await responder.shutdown()
        await llm.aclose()


# --- 21. Ambient/pending без текста триггера -> trigger_text="" в FilterContext. ---


async def test_ambient_reply_has_empty_trigger_text(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config()
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("Бывает."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)

    real_check_output = filters_module.check_output
    captured: dict[str, FilterContext] = {}

    async def spy_check_output(
        text: str, ctx: FilterContext, judge: object = None
    ) -> FilterVerdict:
        captured["ctx"] = ctx
        return await real_check_output(text, ctx, judge)  # type: ignore[arg-type]

    monkeypatch.setattr(filters_module, "check_output", spy_check_output)

    try:
        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.AMBIENT, trigger_msg_id=600, user_id=5, situation="", delay_sec=0
            ),
        )
        assert captured["ctx"].trigger_text == ""
    finally:
        await responder.shutdown()
        await llm.aclose()


# --- 22. Не-ok вердикт с несколькими причинами -> строка filter_log на каждую причину. ---


async def test_multiple_reasons_produce_one_filter_log_row_each(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config()
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("Кандидат."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)

    async def fake_check_output(
        text: str, ctx: FilterContext, judge: object = None
    ) -> FilterVerdict:
        return FilterVerdict(
            ok=False,
            reason="regex:length",
            reasons=("regex:length", "style:exclaim"),
        )

    monkeypatch.setattr(filters_module, "check_output", fake_check_output)

    try:
        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.MENTION, trigger_msg_id=700, user_id=5, situation="", delay_sec=5
            ),
        )

        conn = db._conn
        assert conn is not None
        cursor = await conn.execute(
            "SELECT stage, reason FROM filter_log "
            "WHERE trigger_tg_message_id = ? AND verdict = 'cut' ORDER BY id",
            (700,),
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        assert rows == [
            {"stage": "regex", "reason": "regex:length"},
            {"stage": "style", "reason": "style:exclaim"},
        ]
    finally:
        await responder.shutdown()
        await llm.aclose()


# --- 24. prompt_store читается заново на каждом _respond, не кэшируется в __init__ ---


async def test_prompt_store_is_read_fresh_on_each_respond(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, _varied_responses("Ответ.", "Другой ответ."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    prompt_store = FakePromptStore(
        prompt=PROMPT_TEMPLATE, few_shot="Аня: привет\n{}", prompt_version=1, few_shot_version=1
    )
    responder = _make_responder(db, cfg, llm, bot, clock, prompt_store=prompt_store)
    try:
        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.MENTION, trigger_msg_id=1000, user_id=5, situation="", delay_sec=5
            ),
        )
        payload = _payload(calls[0])
        system_content = payload["messages"][0]["content"]  # type: ignore[index]
        assert "Аня: привет" in system_content

        last = await db.last_bot_replies(1)
        assert last[0].prompt_version == 1
        assert last[0].few_shot_version == 1

        # Меняем версии на PromptStore "снаружи" (как это делают /rollback и /ex add) —
        # без пересоздания Responder следующий _respond должен увидеть новые значения.
        prompt_store.prompt = "Новый системный промпт для {age}, few-shot: {few_shot}"
        prompt_store.few_shot = "Дима: пока\n{}"
        prompt_store.prompt_version_value = 2
        prompt_store.few_shot_version_value = 3

        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.MENTION, trigger_msg_id=1001, user_id=5, situation="", delay_sec=5
            ),
        )
        payload2 = _payload(calls[1])
        system_content_2 = payload2["messages"][0]["content"]  # type: ignore[index]
        assert "Новый системный промпт" in system_content_2
        assert "Дима: пока" in system_content_2

        last2 = await db.last_bot_replies(1)
        assert last2[0].prompt_version == 2
        assert last2[0].few_shot_version == 3
    finally:
        await responder.shutdown()
        await llm.aclose()


# --- 23. Судья передаётся в check_output ---


async def test_judge_is_passed_through_to_check_output(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config()
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("Бывает."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)

    class _DummyJudge:
        async def check(self, *, candidate: str, trigger_text: str, now: int) -> list[str]:
            return []

    dummy_judge = _DummyJudge()
    responder = _make_responder(db, cfg, llm, bot, clock, judge=dummy_judge)  # type: ignore[arg-type]

    captured: dict[str, object] = {}

    async def fake_check_output(
        text: str, ctx: FilterContext, judge: object = None
    ) -> FilterVerdict:
        captured["judge"] = judge
        return FilterVerdict(ok=True, reason="pass", reasons=())

    monkeypatch.setattr(filters_module, "check_output", fake_check_output)

    try:
        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.MENTION, trigger_msg_id=800, user_id=5, situation="", delay_sec=5
            ),
        )
        assert captured["judge"] is dummy_judge
    finally:
        await responder.shutdown()
        await llm.aclose()


# --- 24. llm.main_model пустой: LLM включён ключом, но модель ещё не задана ---


async def test_no_main_model_skips_llm_and_logs_no_model(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """LLM/Judge/Responder создаются в app.py, как только есть openrouter_api_key,
    независимо от llm.main_model (CLAUDE.md, "Интерфейсы этапа 6") — пустая модель
    не должна ронять Responder, только тихо срезать ответ и не ходить в сеть."""
    cfg = _config()
    cfg.llm.main_model = ""
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        msg = _gate_message(tg_message_id=900, user_id=5, text="привет всем", created_at=DAY_NOW)
        with caplog.at_level(logging.WARNING):
            await responder.on_gate_pass(msg, Trigger.AMBIENT, "Дима")
            assert responder._debounce_task is not None
            await clock.run_until(responder._debounce_task)

        assert calls == []
        assert bot.sent == []

        summary = dict(await db.filter_log_summary(0))
        assert summary.get("llm:no_model") == 1

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "main_model" in warnings[0].message
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_main_model_set_hot_enables_llm_on_next_respond(db: Database) -> None:
    """После горячей установки llm.main_model (как сделал бы ConfigStore.set)
    следующий _respond того же Responder должен уже звать LLM — без пересборки
    Responder и без рестарта процесса."""
    cfg = _config()
    cfg.llm.main_model = ""
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Бывает."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        first_msg = _gate_message(
            tg_message_id=901, user_id=5, text="привет всем", created_at=DAY_NOW
        )
        await responder.on_gate_pass(first_msg, Trigger.AMBIENT, "Дима")
        assert responder._debounce_task is not None
        await clock.run_until(responder._debounce_task)

        assert calls == []
        assert bot.sent == []

        # Горячая установка модели (то же самое, что делает ConfigStore.set -> /set).
        cfg.llm.main_model = "test/model"

        await asyncio.sleep(0)  # разрешить предыдущему дебаунс-таску полностью улечься
        second_msg = _gate_message(
            tg_message_id=902, user_id=5, text="как настроение", created_at=DAY_NOW + 30
        )
        await responder.on_gate_pass(second_msg, Trigger.AMBIENT, "Дима")
        assert responder._debounce_task is not None
        await clock.run_until(responder._debounce_task)

        assert len(calls) == 1
        assert len(bot.sent) == 1
        assert bot.sent[0][1] == "Бывает."
    finally:
        await responder.shutdown()
        await llm.aclose()


# --- 22. Этап 5: заведения — интеграция в _generate_and_send ---


def _place_row(
    place_id: str,
    name: str,
    *,
    district: str = "Wilda",
    category: str = "craft",
    rating: float = 4.6,
    reviews: int = 100,
    quiet: bool = False,
    fact: str = "тихо",
    operational: bool = True,
) -> PlaceRow:
    return PlaceRow(
        place_id=place_id,
        name=name,
        district=district,
        category=category,
        rating=rating,
        reviews=reviews,
        price_level=2,
        quiet=quiet,
        fact=fact,
        operational=operational,
        refreshed_at=DAY_NOW,
    )


async def test_places_request_in_mention_sends_full_menu_and_marks_trigger_places(
    db: Database,
) -> None:
    """Решение владельца после живого теста: regex не покрывает живую речь, поэтому
    при обращении весь кэш заведений уходит в промпт целиком (не отфильтрованные
    select_places 1-2 места) -- решение "спрашивали ли про место" и выбор из списка
    отдаётся модели. patterns.places_request по-прежнему матчит "куда сходить" и
    переключает записываемый trigger на "places" -- но только для статистики."""
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Есть одно место, тихое."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)

    await db.upsert_place(_place_row("p1", "Тихий Дворик", quiet=True, fact="тихо"))
    await db.upsert_place(_place_row("p2", "Шумный Бар", quiet=False, fact="шумно"))

    try:
        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.MENTION,
                trigger_msg_id=10,
                user_id=5,
                situation="",
                delay_sec=5,
                trigger_text="Федя, посоветуй куда сходить тихо",
            ),
        )

        assert len(calls) == 1
        payload = _payload(calls[0])
        user_content = payload["messages"][1]["content"]  # type: ignore[index]
        assert "Если про место НЕ спрашивали" in user_content
        assert "Тихий Дворик" in user_content
        assert "Шумный Бар" in user_content

        assert len(bot.sent) == 1
        last = await db.last_bot_replies(1)
        assert last[0].trigger == "places"
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_places_block_absent_for_ambient_even_with_places_request_text(
    db: Database,
) -> None:
    """Ambient/spontaneous/morning никогда не получают блок мест — даже если
    trigger_text (на ambient обычно пустой, но контракт явный: "никогда") похож
    на запрос про места."""
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Бывает."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)

    await db.upsert_place(_place_row("p1", "Тихий Дворик", quiet=True))

    try:
        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.AMBIENT,
                trigger_msg_id=11,
                user_id=5,
                situation="",
                delay_sec=0,
                trigger_text="Федя, посоветуй куда сходить тихо",
            ),
        )

        assert len(calls) == 1
        payload = _payload(calls[0])
        user_content = payload["messages"][1]["content"]  # type: ignore[index]
        assert "Тихий Дворик" not in user_content
        assert PLACES_NONE in user_content

        last = await db.last_bot_replies(1)
        assert last[0].trigger == Trigger.AMBIENT.value
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_mention_without_places_words_still_gets_full_menu_and_keeps_trigger(
    db: Database,
) -> None:
    """Живой тест владельца: "колись где пиво нормальное" и подобные фразы не ловятся
    regex, поэтому список заведений уходит в промпт при ЛЮБОМ прямом обращении, даже
    когда trigger_text вообще не про место -- решение "спрашивали или нет" за моделью.
    trigger в bot_replies остаётся исходным (mention): regex на этот текст не сработала,
    поэтому счётчик "places" для статистики не трогается."""
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("И тебе привет."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)

    await db.upsert_place(_place_row("p1", "Тихий Дворик", quiet=True))

    try:
        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.MENTION,
                trigger_msg_id=12,
                user_id=5,
                situation="",
                delay_sec=5,
                trigger_text="Федя, как сам?",
            ),
        )

        payload = _payload(calls[0])
        user_content = payload["messages"][1]["content"]  # type: ignore[index]
        assert "Если про место НЕ спрашивали" in user_content
        assert "Тихий Дворик" in user_content

        last = await db.last_bot_replies(1)
        assert last[0].trigger == Trigger.MENTION.value
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_places_request_with_empty_places_table_uses_places_none_and_marks_trigger(
    db: Database,
) -> None:
    """Код-ревью: обращение с places_request, но таблица places пуста -- в
    user-сообщении всё равно PLACES_NONE (не пустая строка), а trigger в
    bot_replies остаётся "places" -- запрос был про места, даже если сказать
    нечего."""
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Даже не знаю, куда сходить."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)

    try:
        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.MENTION,
                trigger_msg_id=16,
                user_id=5,
                situation="",
                delay_sec=5,
                trigger_text="Федя, посоветуй куда сходить",
            ),
        )

        assert len(calls) == 1
        payload = _payload(calls[0])
        user_content = payload["messages"][1]["content"]  # type: ignore[index]
        assert PLACES_NONE in user_content

        last = await db.last_bot_replies(1)
        assert last[0].trigger == "places"
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_filter_context_places_names_passes_real_place_through_venue_regex(
    db: Database,
) -> None:
    """FilterContext.places_names всегда заполняется из db.places_names() — реальное
    заведение из таблицы не режется regex:venue, даже когда trigger не про места."""
    cfg = _config()
    cfg.filters.shadow = False
    # "Zielony Kot" намеренно не входит ни в filters.known_places, ни в
    # filters.places_whitelist по умолчанию — это проверяет именно places_names.
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("Был вчера в Zielony Kot."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)

    await db.upsert_place(_place_row("p1", "Zielony Kot"))

    try:
        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.AMBIENT, trigger_msg_id=13, user_id=None, situation="", delay_sec=0
            ),
        )

        assert len(bot.sent) == 1
        assert bot.sent[0][1] == "Был вчера в Zielony Kot."
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_filter_context_places_names_still_cuts_fabricated_venue(
    db: Database,
) -> None:
    """Выдуманное заведение (не из places, не из known_places/whitelist) режется
    regex:venue, несмотря на то что places_names теперь непустой список."""
    cfg = _config()
    cfg.filters.shadow = False
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("Был вчера в Fikcyjny Bar."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)

    await db.upsert_place(_place_row("p1", "Zielony Kot"))

    try:
        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.AMBIENT, trigger_msg_id=14, user_id=None, situation="", delay_sec=0
            ),
        )

        assert bot.sent == []
        logs = await db.filter_log_summary(DAY_NOW - 10)
        assert any(reason == "regex:venue" for reason, _count in logs)
    finally:
        await responder.shutdown()
        await llm.aclose()


# --- Стикеры: интеграция StickerChooser в _generate_and_send -----------------


async def test_sticker_chosen_sends_sticker_not_text(db: Database) -> None:
    cfg = _config()
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("И тебе привет."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    chooser = FakeStickerChooser(
        sticker=Sticker(
            id=3, file_id="FILE3", emoji="😂", text="Ну ты даёшь", when="", enabled=True
        )
    )
    responder = _make_responder(db, cfg, llm, bot, clock, sticker_chooser=chooser)
    try:
        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.AMBIENT, trigger_msg_id=50, user_id=None, situation="", delay_sec=0
            ),
        )

        assert bot.sent == []  # send_message НЕ вызывался
        assert len(bot.sent_stickers) == 1
        assert bot.sent_stickers[0][1] == "FILE3"
        assert len(chooser.calls) == 1

        replies = await db.recent_bot_replies(5)
        assert replies == ["[стикер #3] Ну ты даёшь"]

        assert await db.get_state("replies_since_sticker") == "0"
        sticker_key = day_key("sticker_count", clock.now(), cfg.persona.timezone)
        assert await db.get_state(sticker_key) == "1"

        # Счётчики бюджета обращений/ambient — общий хвост, тот же, что у текста.
        ambient_key = day_key("ambient_count", clock.now(), cfg.persona.timezone)
        assert await db.get_state(ambient_key) == "1"

        logs = await db.filter_log_summary(clock.now() - 10)
        assert any(reason == "send:sticker" for reason, _count in logs)
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_sticker_chooser_returns_none_sends_text_as_before(db: Database) -> None:
    cfg = _config()
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("Бывает."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    chooser = FakeStickerChooser(sticker=None)
    responder = _make_responder(db, cfg, llm, bot, clock, sticker_chooser=chooser)
    try:
        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.AMBIENT, trigger_msg_id=51, user_id=None, situation="", delay_sec=0
            ),
        )

        assert len(chooser.calls) == 1
        assert bot.sent_stickers == []
        assert len(bot.sent) == 1
        assert bot.sent[0][1] == "Бывает."

        assert await db.get_state("replies_since_sticker") == "1"
        sticker_key = day_key("sticker_count", clock.now(), cfg.persona.timezone)
        assert await db.get_state(sticker_key) is None
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_sticker_chooser_not_called_when_min_replies_between_not_elapsed(
    db: Database,
) -> None:
    cfg = _config()
    cfg.behaviour.stickers.min_replies_between = 4
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("Бывает."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    chooser = FakeStickerChooser(
        sticker=Sticker(id=1, file_id="F1", text="Не должно быть выбрано", enabled=True)
    )
    responder = _make_responder(db, cfg, llm, bot, clock, sticker_chooser=chooser)
    await db.set_state("replies_since_sticker", "1")
    try:
        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.AMBIENT, trigger_msg_id=52, user_id=None, situation="", delay_sec=0
            ),
        )

        assert chooser.calls == []  # чузер не вызывается вовсе
        assert bot.sent_stickers == []
        assert len(bot.sent) == 1
        assert await db.get_state("replies_since_sticker") == "2"
    finally:
        await responder.shutdown()
        await llm.aclose()


# --- события жизни (/life) и прямая реплика (/say), CLAUDE.md ---------------


async def test_announce_life_sends_and_marks_event_announced(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Ну вот, продал таки."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        event_id = await db.insert_life_event(
            text="продал Октавию, взял Кию Сид", created_at=DAY_NOW
        )
        event = await db.life_event(event_id)
        assert event is not None

        task = await _drive(clock, responder.announce_life(event))
        outcome = task.result()

        assert outcome.sent is True
        assert outcome.text == "Ну вот, продал таки."
        assert outcome.reason == "send:life"
        assert len(bot.sent) == 1
        assert bot.sent[0][1] == "Ну вот, продал таки."
        assert bot.sent[0][2] is None  # не реплай

        announced = await db.life_event(event_id)
        assert announced is not None
        assert announced.announced_tg_message_id is not None

        last = await db.last_bot_replies(1)
        assert last[0].trigger == "life"
        assert last[0].tg_message_id == announced.announced_tg_message_id
        # now зафиксирован в начале announce_life, до typing-паузы — тот же момент,
        # что и bot_replies.created_at (clock.now() после _drive уже другой:
        # typing-цикл успел продвинуть FakeClock вперёд).
        assert announced.announced_at == last[0].created_at

        # Ситуация (пересказ новости) уходит в user-сообщение, не в system.
        payload = _payload(calls[0])
        user_content = payload["messages"][1]["content"]  # type: ignore[index]
        assert "продал Октавию, взял Кию Сид" in user_content

        logs = await db.filter_log_summary(clock.now() - 10)
        assert any(reason == "send:life" for reason, _count in logs)
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_announce_life_does_not_touch_mention_or_ambient_counters(db: Database) -> None:
    cfg = _config()
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("Бывает."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        event_id = await db.insert_life_event(text="продал Октавию", created_at=DAY_NOW)
        event = await db.life_event(event_id)
        assert event is not None

        task = await _drive(clock, responder.announce_life(event))
        assert task.result().sent is True

        tz = cfg.persona.timezone
        assert await db.get_state(day_key("mention_count", clock.now(), tz)) is None
        assert await db.get_state(day_key("ambient_count", clock.now(), tz)) is None
        assert await db.get_state("last_mention_reply_at") is None
        assert await db.get_state("last_ambient_at") is None
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_announce_life_never_chooses_sticker(db: Database) -> None:
    cfg = _config()
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("Бывает."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    chooser = FakeStickerChooser(sticker=Sticker(id=1, file_id="F1", text="Т", enabled=True))
    responder = _make_responder(db, cfg, llm, bot, clock, sticker_chooser=chooser)
    try:
        event_id = await db.insert_life_event(text="продал Октавию", created_at=DAY_NOW)
        event = await db.life_event(event_id)
        assert event is not None

        await _drive(clock, responder.announce_life(event))

        assert chooser.calls == []
        assert bot.sent_stickers == []
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_announce_life_blocked_by_panic(db: Database) -> None:
    cfg = _config()
    llm, _calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await db.set_state("panic", "1")
        event_id = await db.insert_life_event(text="продал Октавию", created_at=DAY_NOW)
        event = await db.life_event(event_id)
        assert event is not None

        outcome = await responder.announce_life(event)

        assert outcome.sent is False
        assert outcome.reason == "blocked:panic"
        assert bot.sent == []
        still = await db.life_event(event_id)
        assert still is not None
        assert still.announced_at is None

        logs = await db.filter_log_summary(clock.now() - 10)
        assert any(reason == "send:blocked_panic" for reason, _count in logs)
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_announce_life_blocked_by_stop(db: Database) -> None:
    cfg = _config()
    llm, _calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await db.set_state("stop_until", str(DAY_NOW + 1000))
        event_id = await db.insert_life_event(text="продал Октавию", created_at=DAY_NOW)
        event = await db.life_event(event_id)
        assert event is not None

        outcome = await responder.announce_life(event)

        assert outcome.sent is False
        assert outcome.reason == "blocked:stop"
        assert bot.sent == []

        logs = await db.filter_log_summary(clock.now() - 10)
        assert any(reason == "send:blocked_stop" for reason, _count in logs)
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_announce_life_llm_error_returns_reason_and_does_not_send(db: Database) -> None:
    cfg = _config()
    llm, _calls = _make_llm(cfg, db, lambda _req: httpx.Response(500, text="boom"))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        event_id = await db.insert_life_event(text="продал Октавию", created_at=DAY_NOW)
        event = await db.life_event(event_id)
        assert event is not None

        outcome = await responder.announce_life(event)

        assert outcome.sent is False
        assert outcome.reason == "llm:http"
        assert bot.sent == []
        still = await db.life_event(event_id)
        assert still is not None
        assert still.announced_at is None

        logs = await db.filter_log_summary(clock.now() - 10)
        assert any(reason == "llm:http" for reason, _count in logs)
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_announce_life_filter_cut_not_shadow_returns_sent_false_with_candidate(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config()
    cfg.filters.shadow = False
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("Кандидат."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)

    async def fake_check_output(text: str, ctx: object, judge: object = None) -> FilterVerdict:
        return FilterVerdict(ok=False, reason="regex:length", reasons=("regex:length",))

    monkeypatch.setattr(filters_module, "check_output", fake_check_output)
    try:
        event_id = await db.insert_life_event(text="продал Октавию", created_at=DAY_NOW)
        event = await db.life_event(event_id)
        assert event is not None

        outcome = await responder.announce_life(event)

        assert outcome.sent is False
        assert outcome.text == "Кандидат."
        assert outcome.reason == "regex:length"
        assert bot.sent == []

        still = await db.life_event(event_id)
        assert still is not None
        assert still.announced_at is None
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_announce_life_latin_from_note_passes_venue_filter(db: Database) -> None:
    """Латинское название из заметки владельца («Kia Ceed») не режется regex:venue:
    текст события уходит как trigger_text и попадает в белый список фильтра."""
    cfg = _config()
    cfg.filters.shadow = False
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("Взял Kia Ceed, старую продал."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        event_id = await db.insert_life_event(
            text="продал Октавию, взял Kia Ceed 2015 года", created_at=DAY_NOW
        )
        event = await db.life_event(event_id)
        assert event is not None

        task = await _drive(clock, responder.announce_life(event))
        outcome = task.result()

        assert outcome.sent is True
        assert outcome.reason == "send:life"
        assert len(bot.sent) == 1
    finally:
        await responder.shutdown()


async def test_announce_life_shadow_mode_still_sends(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config()
    cfg.filters.shadow = True
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("Кандидат."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)

    async def fake_check_output(text: str, ctx: object, judge: object = None) -> FilterVerdict:
        return FilterVerdict(ok=False, reason="regex:length", reasons=("regex:length",))

    monkeypatch.setattr(filters_module, "check_output", fake_check_output)
    try:
        event_id = await db.insert_life_event(text="продал Октавию", created_at=DAY_NOW)
        event = await db.life_event(event_id)
        assert event is not None

        task = await _drive(clock, responder.announce_life(event))
        outcome = task.result()

        assert outcome.sent is True
        assert outcome.reason == "send:life"
        assert len(bot.sent) == 1

        announced = await db.life_event(event_id)
        assert announced is not None
        assert announced.announced_at is not None
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_life_slot_filled_in_system_for_ordinary_ambient_reply(db: Database) -> None:
    """CLAUDE.md: слот {life} заполняется ВСЕГДА, для любого триггера — не только
    для announce_life."""
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Бывает."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    prompt_store = FakePromptStore(prompt=PROMPT_TEMPLATE_WITH_LIFE)
    responder = _make_responder(db, cfg, llm, bot, clock, prompt_store=prompt_store)
    try:
        await db.insert_life_event(text="продал Октавию", created_at=DAY_NOW - 100)

        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.AMBIENT, trigger_msg_id=900, user_id=None, situation="", delay_sec=0
            ),
        )

        payload = _payload(calls[0])
        system_content = payload["messages"][0]["content"]  # type: ignore[index]
        assert "продал Октавию" in system_content
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_say_sends_text_verbatim_without_model(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        task = await _drive(clock, responder.say("Ну здарова, короче да."))
        outcome = task.result()

        assert outcome.sent is True
        assert outcome.text == "Ну здарова, короче да."
        assert outcome.reason == "send:say"
        assert calls == []  # модель не вызывалась
        assert len(bot.sent) == 1
        assert bot.sent[0][1] == "Ну здарова, короче да."

        last = await db.last_bot_replies(1)
        assert last[0].trigger == "say"
        assert last[0].text == "Ну здарова, короче да."
        assert last[0].trigger_tg_message_id is None

        assert await db.get_state("replies_since_sticker") == "1"

        tz = cfg.persona.timezone
        assert await db.get_state(day_key("mention_count", clock.now(), tz)) is None
        assert await db.get_state(day_key("ambient_count", clock.now(), tz)) is None

        logs = await db.filter_log_summary(clock.now() - 10)
        assert any(reason == "send:say" for reason, _count in logs)
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_say_blocked_by_panic(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await db.set_state("panic", "1")

        outcome = await responder.say("текст")

        assert outcome.sent is False
        assert outcome.reason == "blocked:panic"
        assert bot.sent == []
        assert calls == []
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_say_blocked_by_stop(db: Database) -> None:
    cfg = _config()
    llm, _calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await db.set_state("stop_until", str(DAY_NOW + 1000))

        outcome = await responder.say("текст")

        assert outcome.sent is False
        assert outcome.reason == "blocked:stop"
        assert bot.sent == []
    finally:
        await responder.shutdown()
        await llm.aclose()


# ---------------------------------------------------------------------------
# Горячее окно после /life и /say (CLAUDE.md, "горячее окно"): announce_life/say
# открывают/продлевают окно, обращения ограничены mention_max_delay_sec, ambient
# в окне живёт своим бюджетом (hot_ambient_count) вместо дневного.
# ---------------------------------------------------------------------------


async def test_announce_life_opens_hot_window(db: Database) -> None:
    cfg = _config()
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("Продал, ага."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        event_id = await db.insert_life_event(text="продал Октавию", created_at=DAY_NOW)
        event = await db.life_event(event_id)
        assert event is not None

        task = await _drive(clock, responder.announce_life(event))
        assert task.result().sent is True

        # now зафиксирован в начале announce_life, до typing-паузы (та же логика,
        # что и у announced_at в test_announce_life_sends_and_marks_event_announced).
        hot_until_raw = await db.get_state("hot_until")
        assert hot_until_raw == str(DAY_NOW + cfg.behaviour.hot_window.minutes * 60)
        assert await db.get_state("hot_ambient_count") == "0"
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_announce_life_does_not_open_hot_window_when_disabled(db: Database) -> None:
    cfg = _config()
    cfg.behaviour.hot_window.enabled = False
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("Продал, ага."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        event_id = await db.insert_life_event(text="продал Октавию", created_at=DAY_NOW)
        event = await db.life_event(event_id)
        assert event is not None

        await _drive(clock, responder.announce_life(event))

        assert await db.get_state("hot_until") is None
        assert await db.get_state("hot_ambient_count") is None
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_announce_life_does_not_open_hot_window_when_not_sent(db: Database) -> None:
    """Заблокировано panic'ом -> outcome.sent=False -> окно не открывается."""
    cfg = _config()
    llm, _calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await db.set_state("panic", "1")
        event_id = await db.insert_life_event(text="продал Октавию", created_at=DAY_NOW)
        event = await db.life_event(event_id)
        assert event is not None

        outcome = await responder.announce_life(event)
        assert outcome.sent is False

        assert await db.get_state("hot_until") is None
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_announce_life_extends_existing_hot_window_and_resets_counter(db: Database) -> None:
    """Повторный /life внутри уже открытого окна продлевает его заново от now и
    сбрасывает счётчик — старые ambient-реплики этого окна не переносятся в новое."""
    cfg = _config()
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("И такое бывает."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await db.set_state("hot_until", str(DAY_NOW + 60))
        await db.set_state("hot_ambient_count", "3")

        event_id = await db.insert_life_event(text="взял новую собаку", created_at=DAY_NOW)
        event = await db.life_event(event_id)
        assert event is not None

        await _drive(clock, responder.announce_life(event))

        expected_hot_until = DAY_NOW + cfg.behaviour.hot_window.minutes * 60
        assert await db.get_state("hot_until") == str(expected_hot_until)
        assert await db.get_state("hot_ambient_count") == "0"
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_say_opens_hot_window(db: Database) -> None:
    cfg = _config()
    llm, _calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        task = await _drive(clock, responder.say("Ну здарова."))
        assert task.result().sent is True

        assert await db.get_state("hot_until") == str(
            DAY_NOW + cfg.behaviour.hot_window.minutes * 60
        )
        assert await db.get_state("hot_ambient_count") == "0"
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_say_does_not_open_hot_window_when_blocked(db: Database) -> None:
    cfg = _config()
    llm, _calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await db.set_state("stop_until", str(DAY_NOW + 1000))

        outcome = await responder.say("текст")
        assert outcome.sent is False

        assert await db.get_state("hot_until") is None
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_mention_delay_capped_by_hot_window(db: Database) -> None:
    """Без окна (MaxRandom) обычный бакет отдал бы 3600с — в окне ответ на
    обращение не может быть отложен дальше mention_max_delay_sec."""
    cfg = _config()
    cfg.behaviour.hot_window.mention_max_delay_sec = 120
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock, rng=MaxRandom())
    try:
        await db.set_state("hot_until", str(DAY_NOW + 1000))

        msg = _gate_message(tg_message_id=30, user_id=5, text="фёдор, как сам?", created_at=DAY_NOW)
        await responder._handle_debounced(Trigger.NAME, msg, "Дима")

        rows = await db.load_pending()
        assert len(rows) == 1
        assert rows[0].due_at == DAY_NOW + cfg.behaviour.hot_window.mention_max_delay_sec
        assert calls == []
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_mention_delay_not_capped_outside_hot_window(db: Database) -> None:
    """Тот же сценарий (MaxRandom, долгий бакет), но без открытого окна — потолок
    не применяется, задержка остаётся обычной (не связана с mention_max_delay_sec)."""
    cfg = _config()
    cfg.behaviour.hot_window.mention_max_delay_sec = 120
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock, rng=MaxRandom())
    try:
        msg = _gate_message(tg_message_id=31, user_id=5, text="фёдор, как сам?", created_at=DAY_NOW)
        await responder._handle_debounced(Trigger.NAME, msg, "Дима")

        rows = await db.load_pending()
        assert len(rows) == 1
        assert rows[0].due_at - DAY_NOW > cfg.behaviour.hot_window.mention_max_delay_sec
        assert calls == []
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_mention_earliest_capped_by_hot_window(db: Database) -> None:
    """Кулдаун-сдвиг (earliest) в окне тоже ограничен mention_max_delay_sec: без
    этого ограничения earliest был бы DAY_NOW-10+200=DAY_NOW+190."""
    cfg = _config()
    cfg.behaviour.mention_chat_cooldown_sec = 200
    cfg.behaviour.hot_window.mention_max_delay_sec = 120
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock, rng=MinRandom())
    try:
        await db.set_state("hot_until", str(DAY_NOW + 1000))
        await db.set_state("last_mention_reply_at", str(DAY_NOW - 10))

        msg = _gate_message(tg_message_id=32, user_id=5, text="фёдор, ты где", created_at=DAY_NOW)
        await responder._handle_debounced(Trigger.NAME, msg, "Дима")

        rows = await db.load_pending()
        assert len(rows) == 1
        # earliest без окна был бы DAY_NOW+190; окно ограничивает его DAY_NOW+120,
        # MinRandom.randint -> 5 сверху earliest.
        assert rows[0].due_at == DAY_NOW + 120 + 5
        assert calls == []
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_ambient_recheck_hot_cap_blocks_send_in_hot_window(db: Database) -> None:
    cfg = _config()
    cfg.behaviour.hot_window.ambient_cap = 1
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await db.set_state("hot_until", str(DAY_NOW + 1000))
        await db.set_state("hot_ambient_count", "1")

        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.AMBIENT,
                trigger_msg_id=900,
                user_id=None,
                situation="",
                delay_sec=0,
            ),
        )

        assert calls == []
        assert bot.sent == []
        summary = dict(await db.filter_log_summary(0))
        assert summary.get("send:recheck_hot_cap") == 1
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_ambient_reply_in_hot_window_increments_hot_counter_not_daily(db: Database) -> None:
    cfg = _config()
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("Бывает."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await db.set_state("hot_until", str(DAY_NOW + 1000))
        # _maybe_open_hot_window сброшен намеренно: этот тест проверяет, какой
        # счётчик инкрементится ДО хвоста, открывающего/продлевающего окно
        # (CLAUDE.md, "внимание как у живого человека" — окно теперь открывается
        # после любой успешной отправки, а не только announce_life/say, и сразу
        # обнулило бы hot_ambient_count тем же вызовом). Что окно действительно
        # переоткрывается после ambient-ответа, проверяет отдельный тест ниже.
        responder._maybe_open_hot_window = _noop_open_hot_window  # type: ignore[method-assign]

        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.AMBIENT,
                trigger_msg_id=900,
                user_id=None,
                situation="",
                delay_sec=0,
            ),
        )

        assert len(bot.sent) == 1
        tz = cfg.persona.timezone
        assert await db.get_state(day_key("ambient_count", clock.now(), tz)) is None
        assert await db.get_state("last_ambient_at") is None
        assert await db.get_state("hot_ambient_count") == "1"

        last = await db.last_bot_replies(1)
        assert last[0].trigger == "ambient"  # bot_replies.trigger остаётся ambient

        logs = await db.filter_log_summary(clock.now() - 10)
        assert any(reason == "send:ambient_hot" for reason, _count in logs)
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_ambient_reply_outside_hot_window_uses_daily_budget_as_before(db: Database) -> None:
    """Регрессия: без ранее открытого окна ambient-ответ тратит дневной бюджет (не
    "горячий") и НЕ открывает новое окно — после стопа 15.09 общий хвост
    ``_generate_and_send`` делает это только при ``hot_window.open_on_any_reply``
    (CLAUDE.md, "меньше и разнообразнее", мера 2)."""
    cfg = _config()
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("Бывает."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.AMBIENT,
                trigger_msg_id=900,
                user_id=None,
                situation="",
                delay_sec=0,
            ),
        )

        tz = cfg.persona.timezone
        assert await db.get_state(day_key("ambient_count", clock.now(), tz)) == "1"
        assert await db.get_state("last_ambient_at") is not None
        assert await db.get_state("hot_ambient_count") is None
        assert await db.get_state("hot_until") is None

        logs = await db.filter_log_summary(clock.now() - 10)
        assert any(reason == "send:ambient" for reason, _count in logs)
        assert not any(reason == "send:ambient_hot" for reason, _count in logs)
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_morning_reply_does_not_open_hot_window_by_default(db: Database) -> None:
    """После стопа 15.09 обычный ответ (в том числе утренний) окно не открывает:
    ``hot_window.open_on_any_reply`` по умолчанию false, окно осталось кнопкой
    /life и /say (CLAUDE.md, "меньше и разнообразнее", мера 2)."""
    cfg = _config()
    assert cfg.behaviour.hot_window.open_on_any_reply is False
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("Доброе утро, был занят."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await db.enqueue_night(
            tg_message_id=200,
            user_id=5,
            display_name="Дима",
            text="фёдор ты тут?",
            created_at=DAY_NOW - 3600,
        )

        await _drive(clock, responder._run_morning_once())

        assert len(bot.sent) == 1
        assert await db.get_state("hot_until") is None
        assert await db.get_state("hot_ambient_count") is None
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_morning_reply_opens_hot_window_when_open_on_any_reply(db: Database) -> None:
    """Прежнее поведение возвращается флагом: с ``open_on_any_reply: true`` общий
    хвост _generate_and_send снова открывает окно после любой успешной отправки."""
    cfg = _config()
    cfg.behaviour.hot_window.open_on_any_reply = True
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("Доброе утро, был занят."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await db.enqueue_night(
            tg_message_id=201,
            user_id=5,
            display_name="Дима",
            text="фёдор ты тут?",
            created_at=DAY_NOW - 3600,
        )

        await _drive(clock, responder._run_morning_once())

        assert len(bot.sent) == 1
        hot_until_raw = await db.get_state("hot_until")
        assert hot_until_raw is not None
        assert int(hot_until_raw) > DAY_NOW  # окно открыто в будущее от момента ответа
        assert await db.get_state("hot_ambient_count") == "0"
    finally:
        await responder.shutdown()
        await llm.aclose()


# ---------------------------------------------------------------------------
# Followup: дешёвая проверка "это мне?" превращает сообщение в обращение
# (CLAUDE.md, "внимание как у живого человека"). bot.py уже отфильтровал
# кандидатов по FOLLOWUP_REASONS/hot_until/checker.check() — responder.py
# получает on_gate_pass(gm, Trigger.FOLLOWUP, display_name) и обязан вести
# себя как с любым другим обращением (mention/reply/name).
# ---------------------------------------------------------------------------


async def test_followup_creates_pending_like_other_address_triggers(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Бывает такое."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await _insert_message(
            db,
            tg_message_id=700,
            user_id=5,
            display_name="Дима",
            text="ну и денёк выдался",
            created_at=DAY_NOW,
        )
        msg = _gate_message(
            tg_message_id=700, user_id=5, text="ну и денёк выдался", created_at=DAY_NOW
        )
        await responder.on_gate_pass(msg, Trigger.FOLLOWUP, "Дима")
        assert responder._debounce_task is not None
        await clock.run_until(responder._debounce_task)

        pending_rows = await db.load_pending()
        assert len(pending_rows) == 1
        assert pending_rows[0].trigger == "followup"

        await clock.run_until(responder._pending_tasks[pending_rows[0].id])

        assert len(calls) == 1
        payload = _payload(calls[0])
        user_content = payload["messages"][1]["content"]  # type: ignore[index]
        assert "Вероятно, Дима сейчас написал тебе или о твоей теме" in user_content

        last = await db.last_bot_replies(1)
        assert last[0].trigger == "followup"

        tz = cfg.persona.timezone
        assert await db.get_state(day_key("mention_count", clock.now(), tz)) == "1"
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_followup_mention_delay_capped_by_hot_window(db: Database) -> None:
    """followup — обращение (CLAUDE.md), поэтому в горячем окне его задержка тоже
    ограничена mention_max_delay_sec, как у mention/reply/name."""
    cfg = _config()
    cfg.behaviour.hot_window.mention_max_delay_sec = 120
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("Бывает такое."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await db.set_state("hot_until", str(DAY_NOW + 1000))
        msg = _gate_message(
            tg_message_id=701, user_id=5, text="ну и денёк выдался", created_at=DAY_NOW
        )
        await responder._handle_debounced(Trigger.FOLLOWUP, msg, "Дима")

        rows = await db.load_pending()
        assert len(rows) == 1
        assert rows[0].due_at == DAY_NOW + cfg.behaviour.hot_window.mention_max_delay_sec
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_chat_memory_slot_filled_in_system_for_ordinary_ambient_reply(db: Database) -> None:
    """CLAUDE.md, "долгая память чата": слот заполняется всегда, для любого триггера."""
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Бывает."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    prompt_store = FakePromptStore(prompt=PROMPT_TEMPLATE_WITH_CHAT_MEMORY)
    responder = _make_responder(db, cfg, llm, bot, clock, prompt_store=prompt_store)
    try:
        await db.insert_chat_memory(
            period_start=DAY_NOW - 14 * 86400,
            period_end=DAY_NOW - 7 * 86400,
            text="Илья хвастался велосипедом",
            created_at=DAY_NOW - 7 * 86400,
        )

        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.AMBIENT, trigger_msg_id=900, user_id=None, situation="", delay_sec=0
            ),
        )

        system_content = _payload(calls[0])["messages"][0]["content"]  # type: ignore[index]
        assert "Илья хвастался велосипедом" in system_content
        assert "{chat_memory}" not in system_content
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_chat_memory_slot_empty_when_no_memories(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Бывает."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    prompt_store = FakePromptStore(prompt=PROMPT_TEMPLATE_WITH_CHAT_MEMORY)
    responder = _make_responder(db, cfg, llm, bot, clock, prompt_store=prompt_store)
    try:
        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.AMBIENT, trigger_msg_id=900, user_id=None, situation="", delay_sec=0
            ),
        )

        system_content = _payload(calls[0])["messages"][0]["content"]  # type: ignore[index]
        assert "{chat_memory}" not in system_content
        assert "Что было в чате раньше" not in system_content
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_chat_memory_slot_limited_by_in_prompt(db: Database) -> None:
    cfg = _config()
    cfg.behaviour.chat_memory.in_prompt = 1
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Бывает."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    prompt_store = FakePromptStore(prompt=PROMPT_TEMPLATE_WITH_CHAT_MEMORY)
    responder = _make_responder(db, cfg, llm, bot, clock, prompt_store=prompt_store)
    try:
        await db.insert_chat_memory(
            period_start=DAY_NOW - 21 * 86400,
            period_end=DAY_NOW - 14 * 86400,
            text="давняя неделя",
            created_at=DAY_NOW - 14 * 86400,
        )
        await db.insert_chat_memory(
            period_start=DAY_NOW - 14 * 86400,
            period_end=DAY_NOW - 7 * 86400,
            text="свежая неделя",
            created_at=DAY_NOW - 7 * 86400,
        )

        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.AMBIENT, trigger_msg_id=900, user_id=None, situation="", delay_sec=0
            ),
        )

        system_content = _payload(calls[0])["messages"][0]["content"]  # type: ignore[index]
        assert "свежая неделя" in system_content
        assert "давняя неделя" not in system_content
    finally:
        await responder.shutdown()
        await llm.aclose()


# ---------------------------------------------------------------------------
# «Меньше и разнообразнее» (CLAUDE.md, после стопа 15.09): потолок присутствия,
# пауза между репликами, тишина и адресность для «вернулся проверить».
# ---------------------------------------------------------------------------


async def _fill_presence(db_: Database, *, humans: int, bot_replies: int, now: int) -> None:
    """Сутки, в которых люди написали ``humans`` сообщений, а бот ответил
    ``bot_replies`` раз. Всё — в пределах локальных суток ``now``."""
    for index in range(humans):
        await _insert_message(
            db_,
            tg_message_id=6000 + index,
            user_id=5,
            display_name="Дима",
            text="разговор",
            created_at=now - 3600 + index,
        )
    for index in range(bot_replies):
        await db_.insert_bot_reply(
            tg_message_id=7000 + index,
            reply_to_tg_message_id=None,
            trigger="ambient",
            trigger_tg_message_id=None,
            text="реплика Фёдора",
            prompt_version=1,
            few_shot_version=1,
            delay_sec=0,
            created_at=now - 3600 + index,
        )


async def test_presence_cap_blocks_ambient_before_model(db: Database) -> None:
    """20 человеческих сообщений при max_share 0.15 и free_replies 2 -> allowance 5;
    пятая реплика бота уже выбрала потолок, модель не зовётся вовсе."""
    cfg = _config()
    cfg.behaviour.min_gap_sec = 0  # проверяется отдельно, здесь мешал бы
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await _fill_presence(db, humans=20, bot_replies=5, now=DAY_NOW)

        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.AMBIENT,
                trigger_msg_id=6000,
                user_id=5,
                situation="",
                delay_sec=0,
            ),
        )

        assert calls == []
        assert bot.sent == []
        summary = dict(await db.filter_log_summary(0))
        assert summary.get("send:presence_cap") == 1
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_presence_under_cap_lets_ambient_through(db: Database) -> None:
    cfg = _config()
    cfg.behaviour.min_gap_sec = 0
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Бывает."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await _fill_presence(db, humans=20, bot_replies=4, now=DAY_NOW)

        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.AMBIENT,
                trigger_msg_id=6000,
                user_id=5,
                situation="",
                delay_sec=0,
            ),
        )

        assert len(calls) == 1
        assert len(bot.sent) == 1
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_presence_cap_does_not_block_life_and_say(db: Database) -> None:
    """/life и /say — кнопка владельца: потолок и пауза их не держат."""
    cfg = _config()
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("Продал, ага."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await _fill_presence(db, humans=20, bot_replies=9, now=DAY_NOW)

        event_id = await db.insert_life_event(text="продал Октавию", created_at=DAY_NOW)
        event = await db.life_event(event_id)
        assert event is not None
        life_task = await _drive(clock, responder.announce_life(event))
        assert life_task.result().sent is True

        say_task = await _drive(clock, responder.say("Ну здарова."))
        assert say_task.result().sent is True
        assert len(bot.sent) == 2
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_recheck_presence_cuts_pending_name_address(db: Database) -> None:
    cfg = _config()
    cfg.behaviour.min_gap_sec = 0
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await _fill_presence(db, humans=20, bot_replies=5, now=DAY_NOW)
        pending_id = await db.insert_pending(
            trigger_tg_message_id=50,
            user_id=5,
            trigger="name",
            due_at=DAY_NOW,
            created_at=DAY_NOW - 60,
        )
        row = PendingRow(
            id=pending_id,
            trigger_tg_message_id=50,
            user_id=5,
            trigger="name",
            due_at=DAY_NOW,
            created_at=DAY_NOW - 60,
            done_at=None,
        )

        await responder._fire_pending(row)

        summary = dict(await db.filter_log_summary(0))
        assert summary.get("send:recheck_presence") == 1
        assert bot.sent == []
        assert calls == []
        assert await db.load_pending() == []
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_recheck_presence_lets_reply_to_bot_through(db: Database) -> None:
    """Реплай на сообщение бота проходит под потолком и в момент отправки."""
    cfg = _config()
    cfg.behaviour.min_gap_sec = 0
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Да ну."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await _fill_presence(db, humans=20, bot_replies=5, now=DAY_NOW)
        pending_id = await db.insert_pending(
            trigger_tg_message_id=50,
            user_id=5,
            trigger="reply",
            due_at=DAY_NOW,
            created_at=DAY_NOW - 60,
        )
        row = PendingRow(
            id=pending_id,
            trigger_tg_message_id=50,
            user_id=5,
            trigger="reply",
            due_at=DAY_NOW,
            created_at=DAY_NOW - 60,
            done_at=None,
        )

        await _drive(clock, responder._fire_pending(row))

        assert len(calls) == 1
        assert len(bot.sent) == 1
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_min_gap_delays_pending_instead_of_dropping_it(db: Database) -> None:
    """Обращение не отбрасывается паузой — pending переносится на last + min_gap."""
    cfg = _config()
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock, rng=MinRandom())
    try:
        last_reply_at = DAY_NOW - 100
        await _insert_bot_reply(db, created_at=last_reply_at, tg_message_id=800)
        pending_id = await db.insert_pending(
            trigger_tg_message_id=50,
            user_id=5,
            trigger="name",
            due_at=DAY_NOW,
            created_at=DAY_NOW - 60,
        )
        row = PendingRow(
            id=pending_id,
            trigger_tg_message_id=50,
            user_id=5,
            trigger="name",
            due_at=DAY_NOW,
            created_at=DAY_NOW - 60,
            done_at=None,
        )

        await responder._fire_pending_inner(row)

        rows = await db.load_pending()
        assert len(rows) == 1  # не помечен done — ответ ещё впереди
        assert rows[0].due_at == last_reply_at + cfg.behaviour.min_gap_sec + 5  # MinRandom -> 5
        assert calls == []
        assert bot.sent == []
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_min_gap_elapsed_lets_pending_fire(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Ага."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await _insert_bot_reply(
            db, created_at=DAY_NOW - cfg.behaviour.min_gap_sec - 1, tg_message_id=800
        )
        pending_id = await db.insert_pending(
            trigger_tg_message_id=50,
            user_id=5,
            trigger="name",
            due_at=DAY_NOW,
            created_at=DAY_NOW - 60,
        )
        row = PendingRow(
            id=pending_id,
            trigger_tg_message_id=50,
            user_id=5,
            trigger="name",
            due_at=DAY_NOW,
            created_at=DAY_NOW - 60,
            done_at=None,
        )

        await _drive(clock, responder._fire_pending(row))

        assert len(calls) == 1
        assert len(bot.sent) == 1
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_min_gap_cuts_ambient_with_silence(db: Database) -> None:
    """Неадресный повод паузу не пережидает — молчание, модель не зовётся."""
    cfg = _config()
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await _insert_bot_reply(db, created_at=DAY_NOW - 100, tg_message_id=800)

        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.AMBIENT,
                trigger_msg_id=900,
                user_id=None,
                situation="",
                delay_sec=0,
            ),
        )

        assert calls == []
        assert bot.sent == []
        summary = dict(await db.filter_log_summary(0))
        assert summary.get("send:min_gap") == 1
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_min_gap_zero_disables_the_pause(db: Database) -> None:
    cfg = _config()
    cfg.behaviour.min_gap_sec = 0
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Бывает."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)
    try:
        await _insert_bot_reply(db, created_at=DAY_NOW - 1, tg_message_id=800)

        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.AMBIENT,
                trigger_msg_id=900,
                user_id=None,
                situation="",
                delay_sec=0,
            ),
        )

        assert len(calls) == 1
        assert len(bot.sent) == 1
    finally:
        await responder.shutdown()
        await llm.aclose()


async def _prepare_checkin(db_: Database, *, last_reply_at: int, human_created_at: int) -> None:
    """Состояние, при котором _maybe_checkin дошёл бы до вызова модели."""
    await _insert_bot_reply(db_, created_at=last_reply_at)
    await _insert_message(
        db_,
        tg_message_id=701,
        user_id=5,
        display_name="Дима",
        text="как сам, дед?",
        created_at=human_created_at,
    )
    await _insert_message(
        db_,
        tg_message_id=702,
        user_id=6,
        display_name="Аня",
        text="федя, ты живой вообще?",
        created_at=human_created_at + 10,
    )
    await db_.set_state("checkin_due", str(last_reply_at + 10))
    await db_.set_state("checkin_due_hot_until", str(last_reply_at))


async def test_maybe_checkin_waits_for_quiet_chat(db: Database) -> None:
    """В чате только что писали — «вернулся проверить» откладывается без переноса
    due: следующий poll попробует снова."""
    cfg = _config()
    llm, calls = _make_llm(cfg, db, _fail_handler)
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock, rng=MinRandom())
    try:
        last_reply_at = DAY_NOW - 20000
        due = last_reply_at + 10
        await _prepare_checkin(db, last_reply_at=last_reply_at, human_created_at=DAY_NOW - 60)

        await responder._maybe_checkin()

        assert calls == []
        assert bot.sent == []
        assert await db.get_state("checkin_due") == str(due)  # due не сдвинут
        assert await db.get_state("checkin_last_at") is None
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_maybe_checkin_quiet_min_zero_does_not_wait(db: Database) -> None:
    cfg = _config()
    cfg.behaviour.checkin.quiet_min = 0
    llm, calls = _make_llm(cfg, db, lambda _req: _checkin_response("Бывает.", 2))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock, rng=MinRandom())
    try:
        await _prepare_checkin(db, last_reply_at=DAY_NOW - 20000, human_created_at=DAY_NOW - 60)

        await _drive(clock, responder._maybe_checkin())

        assert len(calls) == 1
        assert len(bot.sent) == 1
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_maybe_checkin_without_reply_to_stays_silent(db: Database) -> None:
    """Ответ «всем сразу», без номера сообщения, больше не отправляется."""
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _checkin_response("Всем привет.", None))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock, rng=MinRandom())
    try:
        last_reply_at = DAY_NOW - 20000
        await _prepare_checkin(db, last_reply_at=last_reply_at, human_created_at=last_reply_at + 10)

        await _drive(clock, responder._maybe_checkin())

        assert len(calls) == 1
        assert bot.sent == []
        summary = dict(await db.filter_log_summary(0))
        assert summary.get("send:checkin_no_target") == 1
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_maybe_checkin_followup_rejects_selected_message(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _checkin_response("Бывает.", 2))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    followup = FakeFollowup(addressed=False)
    responder = _make_responder(db, cfg, llm, bot, clock, rng=MinRandom(), followup=followup)
    try:
        last_reply_at = DAY_NOW - 20000
        await _prepare_checkin(db, last_reply_at=last_reply_at, human_created_at=last_reply_at + 10)

        await _drive(clock, responder._maybe_checkin())

        assert len(calls) == 1
        assert bot.sent == []
        summary = dict(await db.filter_log_summary(0))
        assert summary.get("send:checkin_not_addressed") == 1

        # Проверялась именно выбранная строка (номер 2 — Аня), остальные ушли в
        # контекст проверки.
        assert len(followup.calls) == 1
        assert followup.calls[0]["text"] == "федя, ты живой вообще?"
        assert followup.calls[0]["display_name"] == "Аня"
        context_rows = followup.calls[0]["context_rows"]
        assert isinstance(context_rows, list)
        assert [row.tg_message_id for row in context_rows] == [701]
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_maybe_checkin_followup_confirms_selected_message(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _checkin_response("Живой, чего нет.", 2))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    followup = FakeFollowup(addressed=True)
    responder = _make_responder(db, cfg, llm, bot, clock, rng=MinRandom(), followup=followup)
    try:
        last_reply_at = DAY_NOW - 20000
        await _prepare_checkin(db, last_reply_at=last_reply_at, human_created_at=last_reply_at + 10)

        await _drive(clock, responder._maybe_checkin())

        assert len(calls) == 1
        assert len(bot.sent) == 1
        assert bot.sent[0][2] == 702
        summary = dict(await db.filter_log_summary(0))
        assert summary.get("send:checkin") == 1
    finally:
        await responder.shutdown()
        await llm.aclose()


# --- 40. enforce_stages: dedup/style режут даже при shadow=true; слот {avoid} ---
# --- собирается из тех же recent_replies, что уходят в фильтр -------------------
# --- (CLAUDE.md, "меньше и разнообразнее", меры 4-5). --------------------------


PROMPT_TEMPLATE_WITH_AVOID = (
    "Ты Фёдор, тебе {age} лет.\n"
    "Примеры:\n{few_shot}\n"
    "{context}\n{recent_replies}\n{places}\n{avoid}\n{situation}"
)


async def test_enforce_stages_cut_dedup_even_in_shadow_mode(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config()
    cfg.filters.shadow = True
    assert cfg.filters.enforce_stages == ["dedup", "style"]
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("Кандидат."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)

    async def fake_check_output(text: str, ctx: object, judge: object = None) -> FilterVerdict:
        return FilterVerdict(
            ok=False,
            reason="regex:length",
            reasons=("regex:length", "dedup:motif"),
        )

    monkeypatch.setattr(filters_module, "check_output", fake_check_output)

    try:
        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.MENTION, trigger_msg_id=810, user_id=5, situation="", delay_sec=5
            ),
        )

        assert bot.sent == []

        conn = db._conn
        assert conn is not None
        cursor = await conn.execute(
            "SELECT stage, reason, shadow FROM filter_log "
            "WHERE trigger_tg_message_id = ? AND verdict = 'cut' ORDER BY id",
            (810,),
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        # Обе причины записаны, но shadow=0: реплика реально срезана.
        assert rows == [
            {"stage": "regex", "reason": "regex:length", "shadow": 0},
            {"stage": "dedup", "reason": "dedup:motif", "shadow": 0},
        ]
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_enforce_stages_cut_reason_is_the_enforced_one_in_shadow(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """В shadow режет только enforce-стадия — она и уходит причиной в SendOutcome."""
    cfg = _config()
    cfg.filters.shadow = True
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("Кандидат."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)

    async def fake_check_output(text: str, ctx: object, judge: object = None) -> FilterVerdict:
        return FilterVerdict(
            ok=False,
            reason="regex:length",
            reasons=("regex:length", "style:story_quota"),
        )

    monkeypatch.setattr(filters_module, "check_output", fake_check_output)

    try:
        event_id = await db.insert_life_event(text="продал Октавию", created_at=DAY_NOW)
        event = await db.life_event(event_id)
        assert event is not None

        task = await _drive(clock, responder.announce_life(event))
        outcome = task.result()

        assert outcome.sent is False
        assert outcome.reason == "style:story_quota"
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_non_enforced_stage_still_passes_in_shadow(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Стадии вне enforce_stages в shadow по-прежнему только логируются."""
    cfg = _config()
    cfg.filters.shadow = True
    cfg.filters.enforce_stages = ["dedup"]
    llm, _calls = _make_llm(cfg, db, lambda _req: _ok_response("Кандидат."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    responder = _make_responder(db, cfg, llm, bot, clock)

    async def fake_check_output(text: str, ctx: object, judge: object = None) -> FilterVerdict:
        return FilterVerdict(ok=False, reason="style:exclaim", reasons=("style:exclaim",))

    monkeypatch.setattr(filters_module, "check_output", fake_check_output)

    try:
        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.MENTION, trigger_msg_id=811, user_id=5, situation="", delay_sec=5
            ),
        )

        assert len(bot.sent) == 1

        conn = db._conn
        assert conn is not None
        cursor = await conn.execute(
            "SELECT shadow FROM filter_log WHERE trigger_tg_message_id = ?", (811,)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["shadow"] == 1
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_avoid_slot_filled_from_recent_bot_replies(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Ответ."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    prompt_store = FakePromptStore(prompt=PROMPT_TEMPLATE_WITH_AVOID)
    responder = _make_responder(db, cfg, llm, bot, clock, prompt_store=prompt_store)

    try:
        await db.insert_bot_reply(
            tg_message_id=980,
            reply_to_tg_message_id=None,
            trigger="ambient",
            trigger_tg_message_id=None,
            text="Жена сказала, что хватит. Помню, в девяностых так же было.",
            prompt_version=1,
            few_shot_version=1,
            delay_sec=0,
            created_at=DAY_NOW - 600,
        )

        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.MENTION, trigger_msg_id=981, user_id=5, situation="", delay_sec=5
            ),
        )

        system = _payload(calls[0])["messages"][0]["content"]  # type: ignore[index]
        assert "уже поминал: жену" in system
        assert "девяностые" in system
        assert "Байку сейчас не рассказывай" in system
    finally:
        await responder.shutdown()
        await llm.aclose()


async def test_avoid_slot_empty_without_recent_replies(db: Database) -> None:
    cfg = _config()
    llm, calls = _make_llm(cfg, db, lambda _req: _ok_response("Ответ."))
    bot = FakeBot()
    clock = FakeClock(DAY_NOW)
    prompt_store = FakePromptStore(prompt=PROMPT_TEMPLATE_WITH_AVOID)
    responder = _make_responder(db, cfg, llm, bot, clock, prompt_store=prompt_store)

    try:
        await _drive(
            clock,
            responder._respond(
                trigger=Trigger.MENTION, trigger_msg_id=982, user_id=5, situation="", delay_sec=5
            ),
        )

        system = _payload(calls[0])["messages"][0]["content"]  # type: ignore[index]
        assert "{avoid}" not in system
        assert "уже поминал" not in system
    finally:
        await responder.shutdown()
        await llm.aclose()
