"""Тесты для trolobot.commands: диспетчер команд без сети и без реального Bot.

Message/Chat/User собираются напрямую, как в tests/test_bot.py. Хендлер достаётся
из router и вызывается напрямую. ConfigStore/PromptStore/Database подделаны через
классы, структурно подходящие под Protocol'ы commands.py (_ConfigStoreLike,
_PromptStoreLike, _DbLike) — им не нужно наследоваться, только иметь нужные методы.
Message.answer перехватывается фикстурой `sent`, чтобы не дёргать реальный Bot API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from aiogram.types import Chat, Message, MessageEntity, PhotoSize, User

from trolobot.commands import build_commands_router
from trolobot.config import KeyInfo
from trolobot.config_models import Config
from trolobot.db import ChatMemoryRow, LifeEventRow
from trolobot.few_shot import FewShot
from trolobot.responder import SendOutcome
from trolobot.settings import Settings
from trolobot.timeutil import day_key, local_dt
from trolobot.weather import Place, Weather

OWN_CHAT_ID = -1001234567890
FOREIGN_CHAT_ID = -100999
ADMIN_ID = 555
BOT_USER_ID = 999
BOT_USERNAME = "fedorbot"
NOW = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)


# --- подделки Database/ConfigStore/PromptStore -----------------------------------


@dataclass
class FakeBotReplyRow:
    trigger: str
    trigger_tg_message_id: int | None
    text: str
    prompt_version: int
    few_shot_version: int
    delay_sec: int
    created_at: int


@dataclass
class FakeMessageRow:
    display_name: str | None
    text: str | None


@dataclass
class FakeVersionRow:
    version: int


@dataclass
class FakeDb:
    state: dict[str, str] = field(default_factory=dict)
    audit_stop_calls: list[tuple[str, int, int]] = field(default_factory=list)
    audit_values: dict[str, str | None] = field(default_factory=dict)
    add_mute_calls: list[tuple[int, str, int, int]] = field(default_factory=list)
    remove_mute_calls: list[int] = field(default_factory=list)
    bot_replies_by_id: dict[int, FakeBotReplyRow] = field(default_factory=dict)
    messages: dict[tuple[int, int], FakeMessageRow] = field(default_factory=dict)
    last_replies_value: list[FakeBotReplyRow] = field(default_factory=list)
    filter_log_summary_calls: list[int] = field(default_factory=list)
    filter_log_summary_value: list[tuple[str, int]] = field(default_factory=list)
    pending_value: list[object] = field(default_factory=list)
    night_value: list[object] = field(default_factory=list)
    prompt_versions_value: list[FakeVersionRow] = field(
        default_factory=lambda: [FakeVersionRow(1), FakeVersionRow(2), FakeVersionRow(3)]
    )
    life_events_store: dict[int, LifeEventRow] = field(default_factory=dict)
    _next_life_event_id: int = 1
    chat_memory_store: dict[int, ChatMemoryRow] = field(default_factory=dict)
    # Потолок присутствия (CLAUDE.md, "меньше и разнообразнее"): /status считает его по
    # таблицам, здесь — просто два числа.
    messages_today_value: int = 0
    bot_replies_today_value: int = 0

    async def count_messages_today(self, chat_id: int, day_start: int) -> int:
        return self.messages_today_value

    async def count_bot_replies_today(self, day_start: int) -> int:
        return self.bot_replies_today_value

    async def get_state(self, key: str) -> str | None:
        return self.state.get(key)

    async def set_state(self, key: str, value: str) -> None:
        self.state[key] = value

    async def delete_state(self, key: str) -> None:
        self.state.pop(key, None)

    async def audit_stop(
        self, key: str, changed_by: int, now: int, new_value: str | None = None
    ) -> None:
        self.audit_stop_calls.append((key, changed_by, now))
        self.audit_values[key] = new_value

    async def add_mute(self, user_id: int, display_name: str, muted_by: int, now: int) -> None:
        self.add_mute_calls.append((user_id, display_name, muted_by, now))

    async def remove_mute(self, user_id: int) -> bool:
        self.remove_mute_calls.append(user_id)
        return True

    async def last_bot_replies(self, n: int) -> list[FakeBotReplyRow]:
        return self.last_replies_value[:n]

    async def message_by_tg_id(self, chat_id: int, tg_message_id: int) -> FakeMessageRow | None:
        return self.messages.get((chat_id, tg_message_id))

    async def bot_reply_by_tg_id(self, tg_message_id: int) -> FakeBotReplyRow | None:
        return self.bot_replies_by_id.get(tg_message_id)

    async def filter_log_summary(self, since: int) -> list[tuple[str, int]]:
        self.filter_log_summary_calls.append(since)
        return self.filter_log_summary_value

    async def load_pending(self) -> list[object]:
        return self.pending_value

    async def night_unanswered(self) -> list[object]:
        return self.night_value

    async def prompt_versions(self) -> list[FakeVersionRow]:
        return self.prompt_versions_value

    async def insert_life_event(self, *, text: str, created_at: int) -> int:
        event_id = self._next_life_event_id
        self._next_life_event_id += 1
        self.life_events_store[event_id] = LifeEventRow(
            id=event_id,
            text=text,
            created_at=created_at,
            announced_at=None,
            announced_tg_message_id=None,
        )
        return event_id

    async def life_events(self) -> list[LifeEventRow]:
        return sorted(self.life_events_store.values(), key=lambda e: (e.created_at, e.id))

    async def life_event(self, event_id: int) -> LifeEventRow | None:
        return self.life_events_store.get(event_id)

    async def delete_life_event(self, event_id: int) -> bool:
        return self.life_events_store.pop(event_id, None) is not None

    async def chat_memories(self, limit: int) -> list[ChatMemoryRow]:
        rows = sorted(self.chat_memory_store.values(), key=lambda r: (r.period_end, r.id))
        return rows[-limit:] if limit > 0 else []

    async def chat_memory(self, memory_id: int) -> ChatMemoryRow | None:
        return self.chat_memory_store.get(memory_id)

    async def chat_memory_count(self) -> int:
        return len(self.chat_memory_store)

    async def delete_chat_memory(self, memory_id: int) -> bool:
        return self.chat_memory_store.pop(memory_id, None) is not None

    async def last_chat_memory_end(self) -> int | None:
        if not self.chat_memory_store:
            return None
        return max(row.period_end for row in self.chat_memory_store.values())


@dataclass
class FakeConfigStore:
    cfg: Config
    set_calls: list[tuple[str, str, int, int]] = field(default_factory=list)
    set_result: tuple[str | None, str] = ("3", "5")
    set_error: Exception | None = None
    unset_calls: list[tuple[str, int, int]] = field(default_factory=list)
    unset_result: str | None = "3"
    flat_value: list[tuple[str, str, bool]] = field(
        default_factory=lambda: [
            ("behaviour.daily_cap", "3", False),
            ("behaviour.ambient_probability", "0.15*", True),
            ("llm.main_model", "openrouter/x", False),
        ]
    )
    describe_value: dict[str, KeyInfo | None] = field(default_factory=dict)

    def get(self) -> Config:
        return self.cfg

    def describe(self, key: str) -> KeyInfo | None:
        return self.describe_value.get(key)

    async def set(
        self, key: str, raw_value: str, changed_by: int, now: int
    ) -> tuple[str | None, str]:
        self.set_calls.append((key, raw_value, changed_by, now))
        if self.set_error is not None:
            raise self.set_error
        return self.set_result

    async def unset(self, key: str, changed_by: int, now: int) -> str | None:
        self.unset_calls.append((key, changed_by, now))
        return self.unset_result

    def flat(self) -> list[tuple[str, str, bool]]:
        return self.flat_value


@dataclass
class FakePromptStore:
    prompt_version_value: int = 3
    few_shot_version_value: int = 2
    system_prompt_value: str = "системный промпт персонажа"
    rollback_result: bool = True
    rollback_calls: list[int] = field(default_factory=list)
    add_example_calls: list[tuple[str, str, str, int]] = field(default_factory=list)
    remove_example_calls: list[tuple[int, int]] = field(default_factory=list)
    remove_example_result: int = 7
    examples_value: list[FewShot] = field(default_factory=list)

    def system_prompt(self) -> str:
        return self.system_prompt_value

    def prompt_version(self) -> int:
        return self.prompt_version_value

    def few_shot_version(self) -> int:
        return self.few_shot_version_value

    async def rollback_prompt(self, version: int) -> bool:
        self.rollback_calls.append(version)
        return self.rollback_result

    async def add_example(self, name: str, user: str, text: str, now: int) -> int:
        self.add_example_calls.append((name, user, text, now))
        return 1

    async def remove_example(self, index: int, now: int) -> int:
        self.remove_example_calls.append((index, now))
        return self.remove_example_result

    def examples(self) -> list[FewShot]:
        return self.examples_value


@dataclass
class FakeResponder:
    """Структурно подходит под commands._ResponderLike."""

    announce_life_result: SendOutcome = field(
        default_factory=lambda: SendOutcome(sent=True, text="Продал Октавию", reason="send:life")
    )
    say_result: SendOutcome = field(
        default_factory=lambda: SendOutcome(sent=True, text="", reason="send:say")
    )
    announce_life_calls: list[LifeEventRow] = field(default_factory=list)
    say_calls: list[str] = field(default_factory=list)

    async def announce_life(self, event: LifeEventRow) -> SendOutcome:
        self.announce_life_calls.append(event)
        return self.announce_life_result

    async def say(self, text: str) -> SendOutcome:
        self.say_calls.append(text)
        return self.say_result


@dataclass
class FakeMemorizer:
    """Структурно подходит под commands._ChatMemorizerLike."""

    run_due_result: list[ChatMemoryRow] = field(default_factory=list)
    run_due_calls: list[int] = field(default_factory=list)

    async def run_due(self, *, now: int) -> list[ChatMemoryRow]:
        self.run_due_calls.append(now)
        return self.run_due_result


class FakeWeather:
    """Подделка WeatherClient для /status: снимок без сети (CLAUDE.md, "погода")."""

    def __init__(self, snapshot: Weather | None = None, home_name: str = "") -> None:
        self.snapshot = snapshot
        self._home_name = home_name
        self.calls = 0

    @property
    def home_name(self) -> str:
        return self._home_name

    async def get(self, place: Place | None = None) -> Weather | None:
        self.calls += 1
        return self.snapshot


@dataclass
class FakeDeps:
    """Структурно подходит под commands._CommandsDeps."""

    settings: Settings
    config_store: FakeConfigStore
    prompt_store: FakePromptStore
    db: FakeDb
    responder: FakeResponder | None
    bot_user_id: int
    bot_username: str
    sticker_catalog_enabled: int = 0
    memorizer: FakeMemorizer | None = None
    weather: FakeWeather | None = None


# --- вспомогательные конструкторы -------------------------------------------------


def _chat(chat_id: int = OWN_CHAT_ID) -> Chat:
    return Chat(id=chat_id, type="supergroup", title="АлкоПознань")


def _private_chat(chat_id: int) -> Chat:
    return Chat(id=chat_id, type="private")


def _user(user_id: int, first_name: str = "Дима", last_name: str | None = None) -> User:
    return User(id=user_id, is_bot=False, first_name=first_name, last_name=last_name)


def _message(
    *,
    chat: Chat,
    from_user: User | None,
    message_id: int = 10,
    text: str | None = None,
    caption: str | None = None,
    photo: list[PhotoSize] | None = None,
    reply_to_message: Message | None = None,
    entities: list[MessageEntity] | None = None,
    date: datetime = NOW,
) -> Message:
    return Message(
        message_id=message_id,
        date=date,
        chat=chat,
        from_user=from_user,
        text=text,
        caption=caption,
        photo=photo,
        reply_to_message=reply_to_message,
        entities=entities,
    )


def _make_settings(
    monkeypatch: pytest.MonkeyPatch, *, allowed_chat_id: int, admin_user_id: int
) -> Settings:
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("ALLOWED_CHAT_ID", str(allowed_chat_id))
    monkeypatch.setenv("ADMIN_USER_ID", str(admin_user_id))
    return Settings()


@pytest.fixture
def config() -> Config:
    return Config()


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Перехватывает Message.answer, чтобы не дёргать реальный Bot API."""
    captured: list[str] = []

    async def fake_answer(self: Message, text: str, **kwargs: object) -> None:
        captured.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    return captured


def _deps(
    *,
    settings: Settings,
    config: Config,
    db: FakeDb | None = None,
    responder: FakeResponder | None = None,
    memorizer: FakeMemorizer | None = None,
    weather: FakeWeather | None = None,
) -> tuple[FakeDeps, FakeDb, FakeConfigStore, FakePromptStore]:
    fake_db = db if db is not None else FakeDb()
    config_store = FakeConfigStore(cfg=config)
    prompt_store = FakePromptStore()
    deps = FakeDeps(
        settings=settings,
        config_store=config_store,
        prompt_store=prompt_store,
        db=fake_db,
        responder=responder,
        bot_user_id=BOT_USER_ID,
        bot_username=BOT_USERNAME,
        memorizer=memorizer,
        weather=weather,
    )
    return deps, fake_db, config_store, prompt_store


def _handler(deps: FakeDeps):  # type: ignore[no-untyped-def]
    router = build_commands_router(deps)  # type: ignore[arg-type]
    return router.message.handlers[0].callback


# --- /stop --------------------------------------------------------------------


async def test_stop_by_member_sets_stop_until_and_audits_silently(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_chat(), from_user=_user(7), text="/stop")
    await handler(message)

    now = int(NOW.timestamp())
    assert db.state["stop_until"] == str(now + 86400)
    assert db.audit_stop_calls == [("stop", 7, now)]
    assert sent == []


async def test_stop_in_foreign_chat_does_nothing(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_chat(FOREIGN_CHAT_ID), from_user=_user(7), text="/stop")
    await handler(message)

    assert db.state == {}
    assert db.audit_stop_calls == []
    assert sent == []


# --- /panic, /resume ------------------------------------------------------------


async def test_panic_by_owner_sets_panic_and_replies(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/panic")
    await handler(message)

    assert db.state["panic"] == "1"
    assert sent == ["Паника. Бот молчит до /resume."]
    now = int(NOW.timestamp())
    assert db.audit_stop_calls == [("panic", ADMIN_ID, now)]


async def test_panic_by_non_owner_in_private_does_nothing(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    other_id = 42
    message = _message(chat=_private_chat(other_id), from_user=_user(other_id), text="/panic")
    await handler(message)

    assert db.state == {}
    assert sent == []


async def test_panic_in_chat_does_nothing(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_chat(), from_user=_user(ADMIN_ID), text="/panic")
    await handler(message)

    assert db.state == {}
    assert sent == []


async def test_resume_clears_panic_and_stop_until(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    db.state["panic"] = "1"
    db.state["stop_until"] = "123"
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/resume")
    await handler(message)

    assert "panic" not in db.state
    assert "stop_until" not in db.state
    assert sent == ["Продолжаем. Снято: panic, stop."]
    now = int(NOW.timestamp())
    assert db.audit_stop_calls == [("resume", ADMIN_ID, now)]


async def test_resume_with_nothing_set_replies_without_cleared_list(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    """Ничего не было выставлено — не врём владельцу, что что-то снято."""
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/resume")
    await handler(message)

    assert sent == ["Продолжаем."]
    now = int(NOW.timestamp())
    assert db.audit_stop_calls == [("resume", ADMIN_ID, now)]


async def test_resume_clears_llm_circuit_and_resets_streak(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    db.state["llm_circuit_until"] = str(int(NOW.timestamp()) + 1800)
    db.state["llm_error_streak"] = "5"
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/resume")
    await handler(message)

    assert "llm_circuit_until" not in db.state
    assert db.state["llm_error_streak"] == "0"
    assert sent == ["Продолжаем. Снято: предохранитель LLM (было 5 ошибок подряд)."]


async def test_resume_clears_everything_at_once(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    db.state["panic"] = "1"
    db.state["stop_until"] = "123"
    db.state["llm_circuit_until"] = str(int(NOW.timestamp()) + 1800)
    db.state["llm_error_streak"] = "5"
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/resume")
    await handler(message)

    assert "panic" not in db.state
    assert "stop_until" not in db.state
    assert "llm_circuit_until" not in db.state
    assert db.state["llm_error_streak"] == "0"
    assert sent == ["Продолжаем. Снято: panic, stop, предохранитель LLM (было 5 ошибок подряд)."]


async def test_resume_with_expired_circuit_key_is_still_reported_as_cleared(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    """llm_circuit_until из прошлого — предохранитель фактически уже не действует
    (llm.py сверяет now), но ключ ещё лежит в state, то есть реально был
    выставлен: /resume чистит его и упоминает в ответе так же, как открытый."""
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    db.state["llm_circuit_until"] = str(int(NOW.timestamp()) - 10)
    db.state["llm_error_streak"] = "5"
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/resume")
    await handler(message)

    assert "llm_circuit_until" not in db.state
    assert db.state["llm_error_streak"] == "0"
    assert sent == ["Продолжаем. Снято: предохранитель LLM (было 5 ошибок подряд)."]


# --- /mute, /unmute --------------------------------------------------------------


async def test_mute_without_reply_mutes_self(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    """Без реплая — мьютит самого отправителя, доступно любому участнику."""
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_chat(), from_user=_user(7, "Дима"), text="/mute")
    await handler(message)

    now = int(NOW.timestamp())
    assert db.add_mute_calls == [(7, "Дима", 7, now)]
    assert db.audit_stop_calls == [("mute:7", 7, now)]
    assert sent == []


async def test_mute_by_reply_from_member_on_someone_else_does_nothing(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    """Реплаем на ЧУЖОЕ сообщение мьютить может только владелец — обычный
    участник не должен получить готовый инструмент травли."""
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    target = _message(chat=_chat(), from_user=_user(8, "Толя", "007 🔥"), text="исходное")
    message = _message(
        chat=_chat(), from_user=_user(7), text="/mute", reply_to_message=target, message_id=11
    )
    await handler(message)

    assert db.add_mute_calls == []
    assert db.audit_stop_calls == []
    assert sent == []


async def test_mute_by_reply_from_owner_on_someone_else_adds_mute(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    """Владелец реплаем мьютит другого — единственный, кому это разрешено."""
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    target = _message(chat=_chat(), from_user=_user(8, "Толя", "007 🔥"), text="исходное")
    message = _message(
        chat=_chat(),
        from_user=_user(ADMIN_ID, "Владелец"),
        text="/mute",
        reply_to_message=target,
        message_id=11,
    )
    await handler(message)

    now = int(NOW.timestamp())
    assert db.add_mute_calls == [(8, "Толя", ADMIN_ID, now)]
    assert db.audit_stop_calls == [("mute:8", ADMIN_ID, now)]
    assert sent == []


async def test_mute_target_owner_ignored(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    target = _message(chat=_chat(), from_user=_user(ADMIN_ID, "Владелец"), text="исходное")
    message = _message(
        chat=_chat(),
        from_user=_user(ADMIN_ID, "Владелец"),
        text="/mute",
        reply_to_message=target,
    )
    await handler(message)

    assert db.add_mute_calls == []
    assert sent == []


async def test_mute_target_bot_ignored(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    target = _message(chat=_chat(), from_user=_user(BOT_USER_ID, "Отец Фёдор"), text="исходное")
    message = _message(
        chat=_chat(),
        from_user=_user(ADMIN_ID, "Владелец"),
        text="/mute",
        reply_to_message=target,
    )
    await handler(message)

    assert db.add_mute_calls == []
    assert sent == []


async def test_unmute_without_reply_unmutes_self(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    """Без реплая — снимает мьют с самого отправителя, доступно любому участнику."""
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_chat(), from_user=_user(7, "Дима"), text="/unmute")
    await handler(message)

    now = int(NOW.timestamp())
    assert db.remove_mute_calls == [7]
    assert db.audit_stop_calls == [("unmute:7", 7, now)]
    assert sent == []


async def test_unmute_from_member_ignored_from_owner_removes(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    target = _message(chat=_chat(), from_user=_user(8, "Толя"), text="исходное")

    member_msg = _message(
        chat=_chat(), from_user=_user(7), text="/unmute", reply_to_message=target, message_id=20
    )
    await handler(member_msg)
    assert db.remove_mute_calls == []

    owner_msg = _message(
        chat=_chat(),
        from_user=_user(ADMIN_ID),
        text="/unmute",
        reply_to_message=target,
        message_id=21,
    )
    await handler(owner_msg)
    assert db.remove_mute_calls == [8]
    assert db.audit_stop_calls == [("unmute:8", ADMIN_ID, int(NOW.timestamp()))]
    assert sent == []


# --- /ex add ----------------------------------------------------------------------


async def test_ex_add_by_owner_reply_to_bot_adds_example(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, prompt_store = _deps(settings=settings, config=config)

    db.bot_replies_by_id[100] = FakeBotReplyRow(
        trigger="mention",
        trigger_tg_message_id=50,
        text="И тебе не хворать.",
        prompt_version=3,
        few_shot_version=2,
        delay_sec=12,
        created_at=int(NOW.timestamp()),
    )
    db.messages[(OWN_CHAT_ID, 50)] = FakeMessageRow(display_name="Дима", text="как дела, дед?")

    handler = _handler(deps)
    bot_reply_msg = _message(
        chat=_chat(), from_user=_user(BOT_USER_ID), text="И тебе не хворать.", message_id=100
    )
    message = _message(
        chat=_chat(),
        from_user=_user(ADMIN_ID),
        text="/ex add",
        reply_to_message=bot_reply_msg,
        message_id=101,
    )
    await handler(message)

    assert prompt_store.add_example_calls == [
        ("Дима", "как дела, дед?", "И тебе не хворать.", int(NOW.timestamp()))
    ]
    assert sent == []


async def test_ex_add_by_member_does_nothing(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, prompt_store = _deps(settings=settings, config=config)
    db.bot_replies_by_id[100] = FakeBotReplyRow(
        trigger="mention",
        trigger_tg_message_id=50,
        text="ответ",
        prompt_version=1,
        few_shot_version=1,
        delay_sec=1,
        created_at=int(NOW.timestamp()),
    )
    handler = _handler(deps)

    bot_reply_msg = _message(
        chat=_chat(), from_user=_user(BOT_USER_ID), text="ответ", message_id=100
    )
    message = _message(
        chat=_chat(), from_user=_user(7), text="/ex add", reply_to_message=bot_reply_msg
    )
    await handler(message)

    assert prompt_store.add_example_calls == []
    assert sent == []


# --- /set, /unset -----------------------------------------------------------------


async def test_set_calls_store_and_replies_with_old_and_new(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, config_store, _ = _deps(settings=settings, config=config)
    config_store.set_result = ("3", "5")
    handler = _handler(deps)

    message = _message(
        chat=_private_chat(ADMIN_ID),
        from_user=_user(ADMIN_ID),
        text="/set behaviour.daily_cap 5",
    )
    await handler(message)

    assert config_store.set_calls == [("behaviour.daily_cap", "5", ADMIN_ID, int(NOW.timestamp()))]
    assert sent == ["behaviour.daily_cap: 3 → 5"]


async def test_set_value_error_replies_with_error_text(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, config_store, _ = _deps(settings=settings, config=config)
    config_store.set_error = ValueError("ambient_probability must be <= 1.0")
    handler = _handler(deps)

    message = _message(
        chat=_private_chat(ADMIN_ID),
        from_user=_user(ADMIN_ID),
        text="/set behaviour.ambient_probability 5",
    )
    await handler(message)

    assert sent == ["Ошибка: ambient_probability must be <= 1.0"]


async def test_set_without_args_replies_with_usage(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, config_store, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/set")
    await handler(message)

    assert config_store.set_calls == []
    assert sent == ["Использование: /set <ключ> <значение>. Ключи: /get, описание: /get <ключ>"]


async def test_unset_replies_with_default(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, config_store, _ = _deps(settings=settings, config=config)
    config_store.unset_result = "3"
    handler = _handler(deps)

    message = _message(
        chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/unset behaviour.daily_cap"
    )
    await handler(message)

    assert config_store.unset_calls == [("behaviour.daily_cap", ADMIN_ID, int(NOW.timestamp()))]
    assert sent == ["behaviour.daily_cap: сброшен к 3"]


# --- /get ---------------------------------------------------------------------


async def test_get_all_and_with_prefix(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    await handler(_message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/get"))
    assert len(sent) == 1
    assert "behaviour.daily_cap: 3" in sent[0]
    assert "llm.main_model: openrouter/x" in sent[0]

    await handler(
        _message(
            chat=_private_chat(ADMIN_ID),
            from_user=_user(ADMIN_ID),
            text="/get behaviour.",
            message_id=2,
        )
    )
    assert "behaviour.daily_cap: 3" in sent[1]
    assert "behaviour.ambient_probability" in sent[1]
    assert "llm.main_model" not in sent[1]

    hint = "* — переопределено через /set. Описание ключа: /get <ключ>"
    assert sent[0].endswith(hint)
    assert sent[1].endswith(hint)


async def test_get_exact_key_shows_card(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, config_store, _ = _deps(settings=settings, config=config)
    config_store.describe_value["behaviour.daily_cap"] = KeyInfo(
        key="behaviour.daily_cap",
        value="3",
        default="3",
        overridden=False,
        type_name="int",
        bounds="0..50",
        description="сколько раз в сутки бот может влезть без адресации",
        settable=True,
    )
    handler = _handler(deps)

    message = _message(
        chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/get behaviour.daily_cap"
    )
    await handler(message)

    assert sent == [
        "behaviour.daily_cap: 3\n"
        "тип: int, 0..50\n"
        "сколько раз в сутки бот может влезть без адресации"
    ]


async def test_get_exact_key_card_overridden_and_not_settable(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, config_store, _ = _deps(settings=settings, config=config)
    config_store.describe_value["persona.name"] = KeyInfo(
        key="persona.name",
        value="Фёдор",
        default="Фёдор Второй",
        overridden=True,
        type_name="str",
        bounds="",
        description="",
        settable=False,
    )
    handler = _handler(deps)

    message = _message(
        chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/get persona.name"
    )
    await handler(message)

    assert sent == [
        "persona.name: Фёдор\n"
        "тип: str\n"
        "переопределён, в yaml: Фёдор Второй\n"
        "меняется только в config.yaml"
    ]


# --- /last ------------------------------------------------------------------------


async def test_last_format(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    created_at = int(datetime(2026, 1, 10, 10, 30, tzinfo=UTC).timestamp())
    db.last_replies_value = [
        FakeBotReplyRow(
            trigger="mention",
            trigger_tg_message_id=50,
            text="Привет",
            prompt_version=3,
            few_shot_version=2,
            delay_sec=45,
            created_at=created_at,
        )
    ]
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/last 3")
    await handler(message)

    expected_time = local_dt(created_at, config.persona.timezone).strftime("%H:%M %d.%m")
    assert sent == [f"{expected_time} | mention | p3/f2 | +45s | Привет"]


# --- /why -------------------------------------------------------------------------


async def test_why_computes_since_from_hours(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    db.filter_log_summary_value = [("gate:not_live", 14), ("regex:length", 2)]
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/why 2")
    await handler(message)

    now = int(NOW.timestamp())
    assert db.filter_log_summary_calls == [now - 2 * 3600]
    assert sent == ["gate:not_live 14\nregex:length 2"]


# --- /prompt ------------------------------------------------------------------


async def test_prompt_preview_and_full(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, prompt_store = _deps(settings=settings, config=config)
    prompt_store.system_prompt_value = "А" * 2000
    handler = _handler(deps)

    await handler(_message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/prompt"))
    assert "версия 3 (активная), всего 3" in sent[0]
    assert sent[0].endswith("…")
    assert len(sent[0]) < len(prompt_store.system_prompt_value)

    await handler(
        _message(
            chat=_private_chat(ADMIN_ID),
            from_user=_user(ADMIN_ID),
            text="/prompt full",
            message_id=2,
        )
    )
    assert prompt_store.system_prompt_value in sent[1]


# --- /rollback ----------------------------------------------------------------


async def test_rollback(monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, prompt_store = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/rollback 1")
    await handler(message)

    assert prompt_store.rollback_calls == [1]
    assert sent == ["промпт: версия 1"]


# --- /ex last, /ex rm --------------------------------------------------------------


async def test_ex_last(monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, prompt_store = _deps(settings=settings, config=config)
    prompt_store.examples_value = [
        FewShot(name="Дима", user="как дела?", speak=True, text="Нормально."),
        FewShot(name="Толя", user="где бар?", speak=True, text="На Вильде."),
    ]
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/ex last 2")
    await handler(message)

    assert sent == [
        'Дима: как дела? → {"speak": true, "text": "Нормально."}\n'
        'Толя: где бар? → {"speak": true, "text": "На Вильде."}'
    ]


async def test_ex_rm(monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, prompt_store = _deps(settings=settings, config=config)
    prompt_store.remove_example_result = 9
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/ex rm")
    await handler(message)

    assert prompt_store.remove_example_calls == [(1, int(NOW.timestamp()))]
    assert sent == ["few-shot: версия 9 (удалён пример 1 с конца)"]


# --- /life --------------------------------------------------------------------


async def test_life_add_empty_text_replies_usage(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/life")
    await handler(message)

    assert sent == ["Использование: /life <текст> | list | rm N | post N"]
    assert db.life_events_store == {}
    assert db.audit_stop_calls == []


async def test_life_add_without_responder_only_records(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config, responder=None)
    handler = _handler(deps)

    message = _message(
        chat=_private_chat(ADMIN_ID),
        from_user=_user(ADMIN_ID),
        text="/life продал старую машину, взял другую",
    )
    await handler(message)

    now = int(NOW.timestamp())
    assert list(db.life_events_store.values()) == [
        LifeEventRow(
            id=1,
            text="продал старую машину, взял другую",
            created_at=now,
            announced_at=None,
            announced_tg_message_id=None,
        )
    ]
    assert db.audit_stop_calls == [("life:add", ADMIN_ID, now)]
    assert db.audit_values["life:add"] == "продал старую машину, взял другую"
    assert sent == ["Записал #1. LLM не настроен, в чат не отправлено."]


async def test_life_add_with_responder_sent(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    responder = FakeResponder(
        announce_life_result=SendOutcome(
            sent=True, text="Продал Октавию, взял Кию Сид.", reason="send:life"
        )
    )
    deps, db, _, _ = _deps(settings=settings, config=config, responder=responder)
    handler = _handler(deps)

    message = _message(
        chat=_private_chat(ADMIN_ID),
        from_user=_user(ADMIN_ID),
        text="/life продал Октавию, взял Кию Сид",
    )
    await handler(message)

    now = int(NOW.timestamp())
    assert len(responder.announce_life_calls) == 1
    assert responder.announce_life_calls[0].id == 1
    assert responder.announce_life_calls[0].text == "продал Октавию, взял Кию Сид"
    assert db.audit_stop_calls == [("life:add", ADMIN_ID, now)]
    assert sent == ["Записал #1. Отправлено: Продал Октавию, взял Кию Сид."]


async def test_life_add_blocked_by_panic(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    responder = FakeResponder(
        announce_life_result=SendOutcome(sent=False, text="", reason="blocked:panic")
    )
    deps, _, _, _ = _deps(settings=settings, config=config, responder=responder)
    handler = _handler(deps)

    message = _message(
        chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/life новость"
    )
    await handler(message)

    assert sent == ["Записал #1. Бот молчит (panic/stop) — /resume."]


async def test_life_add_not_sent_shows_reason_and_candidate(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    responder = FakeResponder(
        announce_life_result=SendOutcome(sent=False, text="Продал тачку.", reason="regex:sentences")
    )
    deps, _, _, _ = _deps(settings=settings, config=config, responder=responder)
    handler = _handler(deps)

    message = _message(
        chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/life новость"
    )
    await handler(message)

    assert sent == ["Записал #1. Не отправлено: regex:sentences\nКандидат: Продал тачку."]


async def test_life_add_not_sent_without_candidate_text(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    responder = FakeResponder(
        announce_life_result=SendOutcome(sent=False, text="", reason="llm:silent")
    )
    deps, _, _, _ = _deps(settings=settings, config=config, responder=responder)
    handler = _handler(deps)

    message = _message(
        chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/life новость"
    )
    await handler(message)

    assert sent == ["Записал #1. Не отправлено: llm:silent"]


async def test_life_list_empty(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/life list")
    await handler(message)

    assert sent == ["Событий нет."]


async def test_life_list_formats_rows_oldest_first(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    db.life_events_store[1] = LifeEventRow(
        id=1,
        text="продал Октавию",
        created_at=int(datetime(2026, 1, 5, 9, 0, tzinfo=UTC).timestamp()),
        announced_at=int(datetime(2026, 1, 5, 9, 1, tzinfo=UTC).timestamp()),
        announced_tg_message_id=42,
    )
    db.life_events_store[2] = LifeEventRow(
        id=2,
        text="взял Кию Сид",
        created_at=int(datetime(2026, 1, 9, 9, 0, tzinfo=UTC).timestamp()),
        announced_at=None,
        announced_tg_message_id=None,
    )
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/life list")
    await handler(message)

    assert sent == ["#1 05.01.2026 ✓ продал Октавию\n#2 09.01.2026 — взял Кию Сид"]


async def test_life_rm_deletes_existing_event(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    db.life_events_store[3] = LifeEventRow(
        id=3, text="продал тачку", created_at=1, announced_at=None, announced_tg_message_id=None
    )
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/life rm 3")
    await handler(message)

    now = int(NOW.timestamp())
    assert 3 not in db.life_events_store
    assert db.audit_stop_calls == [("life:rm", ADMIN_ID, now)]
    assert sent == ["Событие #3 удалено."]


async def test_life_rm_missing_event(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/life rm 9")
    await handler(message)

    assert sent == ["Нет события #9."]
    assert db.audit_stop_calls == []


async def test_life_rm_without_number_replies_usage(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/life rm")
    await handler(message)

    assert sent == ["Использование: /life <текст> | list | rm N | post N"]


async def test_life_post_resends_existing_event_without_zapisal_prefix(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    responder = FakeResponder(
        announce_life_result=SendOutcome(sent=True, text="Продал тачку.", reason="send:life")
    )
    deps, db, _, _ = _deps(settings=settings, config=config, responder=responder)
    db.life_events_store[4] = LifeEventRow(
        id=4, text="продал тачку", created_at=1, announced_at=None, announced_tg_message_id=None
    )
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/life post 4")
    await handler(message)

    now = int(NOW.timestamp())
    assert len(responder.announce_life_calls) == 1
    assert responder.announce_life_calls[0].id == 4
    assert db.audit_stop_calls == [("life:post", ADMIN_ID, now)]
    assert sent == ["Отправлено: Продал тачку."]


async def test_life_post_missing_event(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/life post 9")
    await handler(message)

    assert sent == ["Нет события #9."]


async def test_life_post_without_responder(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config, responder=None)
    db.life_events_store[5] = LifeEventRow(
        id=5, text="продал тачку", created_at=1, announced_at=None, announced_tg_message_id=None
    )
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/life post 5")
    await handler(message)

    assert sent == ["LLM не настроен, в чат не отправлено."]


async def test_life_command_in_chat_does_nothing(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_chat(), from_user=_user(ADMIN_ID), text="/life новость")
    await handler(message)

    assert db.life_events_store == {}
    assert sent == []


# --- /say ---------------------------------------------------------------------


async def test_say_empty_text_replies_usage(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/say")
    await handler(message)

    assert sent == ["Использование: /say <текст>"]
    assert db.audit_stop_calls == []


async def test_say_without_responder(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config, responder=None)
    handler = _handler(deps)

    message = _message(
        chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/say Всем привет"
    )
    await handler(message)

    now = int(NOW.timestamp())
    assert db.audit_stop_calls == [("say", ADMIN_ID, now)]
    assert sent == ["LLM-часть выключена, /say недоступен."]


async def test_say_sends_text_verbatim(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    responder = FakeResponder(say_result=SendOutcome(sent=True, text="", reason="send:say"))
    deps, db, _, _ = _deps(settings=settings, config=config, responder=responder)
    handler = _handler(deps)

    message = _message(
        chat=_private_chat(ADMIN_ID),
        from_user=_user(ADMIN_ID),
        text="/say Всем   привет,   как дела?",
    )
    await handler(message)

    now = int(NOW.timestamp())
    assert responder.say_calls == ["Всем   привет,   как дела?"]
    assert db.audit_stop_calls == [("say", ADMIN_ID, now)]
    assert sent == ["Отправлено."]


async def test_say_blocked_by_stop(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    responder = FakeResponder(say_result=SendOutcome(sent=False, text="", reason="blocked:stop"))
    deps, _, _, _ = _deps(settings=settings, config=config, responder=responder)
    handler = _handler(deps)

    message = _message(
        chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/say Всем привет"
    )
    await handler(message)

    assert sent == ["Бот молчит (panic/stop) — /resume."]


async def test_say_command_in_chat_does_nothing(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_chat(), from_user=_user(ADMIN_ID), text="/say Всем привет")
    await handler(message)

    assert db.audit_stop_calls == []
    assert sent == []


# --- /status ------------------------------------------------------------------


async def test_status_contains_versions_and_models(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    llm = config.llm.model_copy(update={"main_model": "openrouter/main", "judge_model": "j/x"})
    cfg = config.model_copy(update={"llm": llm})
    deps, _, _, prompt_store = _deps(settings=settings, config=cfg)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status")
    await handler(message)

    assert len(sent) == 1
    text = sent[0]
    assert f"v{prompt_store.prompt_version_value}" in text
    assert f"v{prompt_store.few_shot_version_value}" in text
    assert "openrouter/main" in text
    assert "j/x" in text


async def test_status_shows_circuit_closed_and_zero_streak_by_default(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status")
    await handler(message)

    assert len(sent) == 1
    text = sent[0]
    assert "Предохранитель LLM: закрыт, ошибок подряд: 0" in text


async def test_status_shows_circuit_open_until_local_time_and_streak(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    now = int(NOW.timestamp())
    circuit_until = now + 1800
    db.state["llm_circuit_until"] = str(circuit_until)
    db.state["llm_error_streak"] = "5"
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status")
    await handler(message)

    assert len(sent) == 1
    text = sent[0]
    expected_time = local_dt(circuit_until, config.persona.timezone).strftime("%H:%M")
    assert f"Предохранитель LLM: открыт до {expected_time}, ошибок подряд: 5" in text


async def test_status_shows_circuit_closed_when_key_expired(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    """llm_circuit_until в прошлом — предохранитель фактически отпустил сам по
    таймеру (та же проверка, что в llm.py: `> now`), /status не должен врать,
    что он ещё открыт, даже если ключ ещё не подчищен."""
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    db.state["llm_circuit_until"] = str(int(NOW.timestamp()) - 10)
    db.state["llm_error_streak"] = "5"
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status")
    await handler(message)

    assert len(sent) == 1
    text = sent[0]
    assert "Предохранитель LLM: закрыт, ошибок подряд: 5" in text


async def test_status_shows_stop_only_while_active(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    """stop_until в прошлом — гейт стоп уже не применяет (сравнивает с now), а ключ
    никто не удаляет; /status не должен показывать истёкший стоп (живой случай
    16.09.2026: «у бота до сих пор стоп, хотя время прошло»). Действующий — показывает."""
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)
    now = int(NOW.timestamp())

    db.state["stop_until"] = str(now - 10)
    await handler(_message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status"))
    assert "Стоп до: нет" in sent[0]

    db.state["stop_until"] = str(now + 3600)
    await handler(
        _message(
            chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status", message_id=2
        )
    )
    assert "Стоп до: нет" not in sent[1]
    assert "Стоп до: " in sent[1]


async def test_status_shows_life_events_total_and_unsent(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    db.life_events_store[1] = LifeEventRow(
        id=1, text="продал тачку", created_at=1, announced_at=1, announced_tg_message_id=42
    )
    db.life_events_store[2] = LifeEventRow(
        id=2, text="взял другую", created_at=2, announced_at=None, announced_tg_message_id=None
    )
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status")
    await handler(message)

    assert len(sent) == 1
    assert "life events: 2 (1)" in sent[0]


async def test_status_shows_no_life_events_by_default(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status")
    await handler(message)

    assert "life events: 0 (0)" in sent[0]


async def test_status_shows_hot_window_none_by_default(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status")
    await handler(message)

    assert "hot window: нет" in sent[0]


async def test_status_shows_hot_window_open_until_local_time_and_count(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    now = int(NOW.timestamp())
    hot_until = now + 900
    db.state["hot_until"] = str(hot_until)
    db.state["hot_ambient_count"] = "2"
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status")
    await handler(message)

    expected_time = local_dt(hot_until, config.persona.timezone).strftime("%H:%M")
    cap = config.behaviour.hot_window.ambient_cap
    assert f"hot window: до {expected_time} (2/{cap})" in sent[0]


async def test_status_shows_hot_window_none_when_expired(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    """hot_until в прошлом — /status не должен врать, что окно ещё открыто."""
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    now = int(NOW.timestamp())
    db.state["hot_until"] = str(now - 10)
    db.state["hot_ambient_count"] = "1"
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status")
    await handler(message)

    assert "hot window: нет" in sent[0]


async def test_status_shows_followup_calls_zero_by_default(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status")
    await handler(message)

    cap = config.behaviour.followup.daily_cap
    assert f"followup calls: 0/{cap}" in sent[0]


async def test_status_shows_followup_calls_today_count(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    now = int(NOW.timestamp())
    db.state[day_key("followup_calls", now, config.persona.timezone)] = "7"
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status")
    await handler(message)

    cap = config.behaviour.followup.daily_cap
    assert f"followup calls: 7/{cap}" in sent[0]


async def test_status_shows_reaction_semantic_calls(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    """Реакции считаются двумя числами: сколько поставлено и сколько вызовов модели
    на них потрачено (свой потолок semantic_daily_cap)."""
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    now = int(NOW.timestamp())
    tz = config.persona.timezone
    db.state[day_key("reaction_count", now, tz)] = "2"
    db.state[day_key("reaction_calls", now, tz)] = "5"
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status")
    await handler(message)

    reactions_cfg = config.behaviour.reactions
    assert f"reactions=2/{reactions_cfg.daily_cap}" in sent[0]
    assert f"semantic calls 5/{reactions_cfg.semantic_daily_cap}" in sent[0]


async def test_status_shows_zero_reaction_semantic_calls_by_default(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status")
    await handler(message)

    cap = config.behaviour.reactions.semantic_daily_cap
    assert f"semantic calls 0/{cap}" in sent[0]


async def test_status_shows_vision_zero_by_default(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status")
    await handler(message)

    cap = config.behaviour.vision.daily_cap
    assert f"vision: 0/{cap}" in sent[0]


async def test_status_shows_vision_today_count(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    now = int(NOW.timestamp())
    db.state[day_key("vision_count", now, config.persona.timezone)] = "4"
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status")
    await handler(message)

    cap = config.behaviour.vision.daily_cap
    assert f"vision: 4/{cap}" in sent[0]


async def test_status_shows_checkin_none_by_default(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status")
    await handler(message)

    assert "checkin: нет" in sent[0]


async def test_status_shows_presence_allowance_and_counts(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    """Потолок присутствия (CLAUDE.md, "меньше и разнообразнее"): 20 сообщений людей
    при max_share 0.15 и free_replies 2 дают allowance 5."""
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    db.messages_today_value = 20
    db.bot_replies_today_value = 3
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status")
    await handler(message)

    assert "presence: 3/5 (people 20)" in sent[0]


async def test_status_shows_presence_free_replies_in_empty_chat(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status")
    await handler(message)

    assert "presence: 0/2 (people 0)" in sent[0]


async def test_status_shows_checkin_due_local_time(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    now = int(NOW.timestamp())
    due = now + 3600
    db.state["checkin_due"] = str(due)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status")
    await handler(message)

    expected_time = local_dt(due, config.persona.timezone).strftime("%H:%M")
    assert f"checkin: due {expected_time}" in sent[0]


async def test_status_shows_no_weather_without_client(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    """Клиента нет вовсе (или координаты не заданы) — «погода: нет»."""
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status")
    await handler(message)

    assert "погода: нет" in sent[0]


async def test_status_shows_no_weather_when_snapshot_is_none(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    weather = FakeWeather(snapshot=None, home_name="Город")
    deps, _, _, _ = _deps(settings=settings, config=config, weather=weather)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status")
    await handler(message)

    assert "погода: нет" in sent[0]
    assert weather.calls == 1


async def test_status_shows_weather_and_fetch_time(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    fetched_at = int(NOW.timestamp()) - 600
    snapshot = Weather(
        temp_now=9.4,
        code_now=3,
        today_min=4.2,
        today_max=11.6,
        today_code=3,
        tomorrow_min=-2.6,
        tomorrow_max=3.4,
        tomorrow_code=61,
        fetched_at=fetched_at,
    )
    weather = FakeWeather(snapshot=snapshot, home_name="Город")
    deps, _, _, _ = _deps(settings=settings, config=config, weather=weather)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status")
    await handler(message)

    expected_time = local_dt(fetched_at, config.persona.timezone).strftime("%H:%M")
    assert f"погода: +9, пасмурно, обновлена {expected_time}" in sent[0]


async def test_status_weather_without_home_name(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    snapshot = Weather(
        temp_now=-0.4,
        code_now=4242,  # неизвестный код -> описания нет
        today_min=-3.0,
        today_max=1.0,
        today_code=3,
        tomorrow_min=-5.0,
        tomorrow_max=0.0,
        tomorrow_code=71,
        fetched_at=int(NOW.timestamp()),
    )
    deps, _, _, _ = _deps(settings=settings, config=config, weather=FakeWeather(snapshot=snapshot))
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status")
    await handler(message)

    assert "погода: 0, обновлена" in sent[0]


# --- справка ------------------------------------------------------------------


async def test_unknown_command_replies_help(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/frobnicate")
    await handler(message)

    assert len(sent) == 1
    assert "/status" in sent[0]
    assert "/panic" in sent[0]
    assert "/stop" in sent[0]
    assert "/mute" in sent[0]
    assert "/life" in sent[0]
    assert "/say" in sent[0]
    assert "/help" in sent[0]


async def test_help_command_replies_with_same_help_text(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    unknown_msg = _message(
        chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/frobnicate"
    )
    await handler(unknown_msg)

    help_msg = _message(
        chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/help", message_id=2
    )
    await handler(help_msg)

    assert len(sent) == 2
    assert sent[0] == sent[1]
    assert "/stop" in sent[1]
    assert "/mute" in sent[1]


# --- суффикс @bot_username -----------------------------------------------------


async def test_command_with_bot_suffix_works(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_chat(), from_user=_user(7), text=f"/stop@{BOT_USERNAME}")
    await handler(message)

    assert db.state.get("stop_until") == str(int(NOW.timestamp()) + 86400)


async def test_command_with_wrong_bot_suffix_does_nothing(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    """Суффикс @другой_бот в группе рассылает команду всем ботам чата — исполняем
    только адресованную нам."""
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_chat(), from_user=_user(7), text="/stop@weatherbot")
    await handler(message)

    assert db.state == {}
    assert db.audit_stop_calls == []
    assert sent == []


# --- команда в подписи к фото ---------------------------------------------------


async def test_panic_command_as_photo_caption_from_owner(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    photo = [PhotoSize(file_id="f1", file_unique_id="u1", width=100, height=100)]
    message = _message(
        chat=_private_chat(ADMIN_ID),
        from_user=_user(ADMIN_ID),
        caption="/panic",
        photo=photo,
    )
    await handler(message)

    assert db.state["panic"] == "1"
    assert sent == ["Паника. Бот молчит до /resume."]


# --- обрезка длинного ответа ----------------------------------------------------


async def test_long_reply_is_truncated_to_3500(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, prompt_store = _deps(settings=settings, config=config)
    prompt_store.system_prompt_value = "Ы" * 5000
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/prompt full")
    await handler(message)

    assert len(sent) == 1
    assert len(sent[0]) == 3500
    assert sent[0].endswith("…")


# --- /mute через text_mention entity --------------------------------------------


async def test_mute_via_text_mention_entity_by_owner(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    """text_mention-упоминание — тоже "цель другой", поэтому доступно только владельцу."""
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    mentioned = _user(9, "Оля")
    entity = MessageEntity(type="text_mention", offset=6, length=3, user=mentioned)
    message = _message(
        chat=_chat(),
        from_user=_user(ADMIN_ID, "Владелец"),
        text="/mute Оля",
        entities=[entity],
    )
    await handler(message)

    assert db.add_mute_calls == [(9, "Оля", ADMIN_ID, int(NOW.timestamp()))]


async def test_mute_via_text_mention_entity_by_member_does_nothing(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    mentioned = _user(9, "Оля")
    entity = MessageEntity(type="text_mention", offset=6, length=3, user=mentioned)
    message = _message(chat=_chat(), from_user=_user(7), text="/mute Оля", entities=[entity])
    await handler(message)

    assert db.add_mute_calls == []


# --- /memory (CLAUDE.md, "долгая память чата") --------------------------------


def _memory_row(memory_id: int, text: str) -> ChatMemoryRow:
    """Период 08.09–14.09.2026 по Europe/Warsaw (persona.timezone по умолчанию)."""
    start = int(datetime(2026, 9, 8, tzinfo=ZoneInfo("Europe/Warsaw")).timestamp())
    end = int(datetime(2026, 9, 15, tzinfo=ZoneInfo("Europe/Warsaw")).timestamp())
    return ChatMemoryRow(
        id=memory_id,
        period_start=start + (memory_id - 1) * 7 * 86400,
        period_end=end + (memory_id - 1) * 7 * 86400,
        text=text,
        created_at=end,
    )


async def test_memory_list_empty(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/memory")
    await handler(message)

    assert sent == ["Памяти пока нет."]


async def test_memory_list_shows_period_and_text_oldest_first(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    db.chat_memory_store[1] = _memory_row(1, "Илья хвастался велосипедом")
    db.chat_memory_store[2] = _memory_row(2, "Собирались за грибами")
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/memory list")
    await handler(message)

    assert len(sent) == 1
    text = sent[0]
    assert text.startswith("#1 08.09–14.09.2026\nИлья хвастался велосипедом")
    assert "#2 15.09–21.09.2026\nСобирались за грибами" in text


async def test_memory_rm_deletes_and_audits(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    db.chat_memory_store[1] = _memory_row(1, "Илья хвастался велосипедом")
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/memory rm 1")
    await handler(message)

    assert sent == ["Пересказ #1 удалён."]
    assert db.chat_memory_store == {}
    assert db.audit_stop_calls == [("memory:rm", ADMIN_ID, int(NOW.timestamp()))]
    assert db.audit_values["memory:rm"] == "Илья хвастался велосипедом"


async def test_memory_rm_unknown_id(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/memory rm 7")
    await handler(message)

    assert sent == ["Нет пересказа #7."]


async def test_memory_rm_without_number_replies_usage(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/memory rm")
    await handler(message)

    assert sent == ["Использование: /memory [list | rm N | run]"]


async def test_memory_run_reports_created_rows(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    memorizer = FakeMemorizer(run_due_result=[_memory_row(1, "Илья хвастался велосипедом")])
    deps, db, _, _ = _deps(settings=settings, config=config, memorizer=memorizer)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/memory run")
    await handler(message)

    assert memorizer.run_due_calls == [int(NOW.timestamp())]
    assert len(sent) == 1
    assert sent[0].startswith("Добавлено пересказов: 1")
    assert "#1 08.09–14.09.2026\nИлья хвастался велосипедом" in sent[0]
    assert db.audit_stop_calls == [("memory:run", ADMIN_ID, int(NOW.timestamp()))]


async def test_memory_run_nothing_to_summarize(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    memorizer = FakeMemorizer()
    deps, _, _, _ = _deps(settings=settings, config=config, memorizer=memorizer)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/memory run")
    await handler(message)

    assert sent == ["Нечего пересказывать."]


async def test_memory_run_without_memorizer(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/memory run")
    await handler(message)

    assert sent == ["LLM-часть выключена, пересказы недоступны."]
    assert db.audit_stop_calls == []


async def test_memory_unknown_subcommand_replies_usage(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(
        chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/memory всё забудь"
    )
    await handler(message)

    assert sent == ["Использование: /memory [list | rm N | run]"]


async def test_memory_command_in_chat_does_nothing(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    """Команда владельца — только в личке; в чате памятью не светим."""
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    db.chat_memory_store[1] = _memory_row(1, "Илья хвастался велосипедом")
    handler = _handler(deps)

    message = _message(chat=_chat(), from_user=_user(ADMIN_ID), text="/memory list")
    await handler(message)

    assert sent == []


async def test_help_mentions_memory_command(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/help")
    await handler(message)

    assert "/memory" in sent[0]


async def test_status_shows_no_chat_memory_by_default(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status")
    await handler(message)

    assert "chat memory: нет" in sent[0]


async def test_status_shows_chat_memory_total_and_last_period_end(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, db, _, _ = _deps(settings=settings, config=config)
    db.chat_memory_store[1] = _memory_row(1, "Илья хвастался велосипедом")
    db.chat_memory_store[2] = _memory_row(2, "Собирались за грибами")
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status")
    await handler(message)

    assert "chat memory: 2, последний до 22.09.2026" in sent[0]


# --- /changelog -------------------------------------------------------------


_CHANGELOG_SAMPLE = """# Changelog

## [Unreleased]

### Для чата

### Для владельца

## [0.5.0] — 2026-09-16

### Для чата

- Фёдор стал говорить заметно реже.

### Для владельца

- Потолок присутствия `behaviour.presence` (#12).

## [0.4.0] — 2026-09-15

### Для чата

- Фёдор видит фото.

### Для владельца

- `/life`, `/say` (#10).

[Unreleased]: https://example.com/compare/v0.5.0...HEAD
[0.5.0]: https://example.com/releases/tag/v0.5.0
[0.4.0]: https://example.com/compare/v0.4.0...v0.5.0
"""


def _write_changelog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str = _CHANGELOG_SAMPLE
) -> None:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("CHANGELOG_PATH", str(path))


async def test_changelog_bare_returns_latest_for_chat(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str], tmp_path: Path
) -> None:
    _write_changelog(monkeypatch, tmp_path)
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/changelog")
    await handler(message)

    assert sent == ["v0.5.0 (2026-09-16)\n- Фёдор стал говорить заметно реже."]


async def test_changelog_owner_returns_latest_for_owner(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str], tmp_path: Path
) -> None:
    _write_changelog(monkeypatch, tmp_path)
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(
        chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/changelog owner"
    )
    await handler(message)

    assert sent == ["v0.5.0 (2026-09-16)\n- Потолок присутствия `behaviour.presence` (#12)."]


@pytest.mark.parametrize("version_arg", ["0.4.0", "v0.4.0"])
async def test_changelog_specific_version_accepts_v_prefix(
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
    sent: list[str],
    tmp_path: Path,
    version_arg: str,
) -> None:
    _write_changelog(monkeypatch, tmp_path)
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(
        chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text=f"/changelog {version_arg}"
    )
    await handler(message)

    assert sent == ["v0.4.0 (2026-09-15)\n- Фёдор видит фото."]


async def test_changelog_unknown_version_replies_not_found(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str], tmp_path: Path
) -> None:
    _write_changelog(monkeypatch, tmp_path)
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(
        chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/changelog 9.9.9"
    )
    await handler(message)

    assert sent == ["Версии 9.9.9 нет."]


async def test_changelog_missing_file_replies_not_found(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("CHANGELOG_PATH", str(tmp_path / "does-not-exist.md"))
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/changelog")
    await handler(message)

    assert sent == ["CHANGELOG.md не найден или пуст."]


async def test_changelog_ignored_in_chat(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str], tmp_path: Path
) -> None:
    _write_changelog(monkeypatch, tmp_path)
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_chat(), from_user=_user(ADMIN_ID), text="/changelog")
    await handler(message)

    assert sent == []


async def test_status_starts_with_version(
    monkeypatch: pytest.MonkeyPatch, config: Config, sent: list[str]
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID, admin_user_id=ADMIN_ID)
    deps, _, _, _ = _deps(settings=settings, config=config)
    handler = _handler(deps)

    message = _message(chat=_private_chat(ADMIN_ID), from_user=_user(ADMIN_ID), text="/status")
    await handler(message)

    assert sent[0].startswith("trolobot v")
