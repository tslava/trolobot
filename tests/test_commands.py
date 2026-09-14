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

import pytest
from aiogram.types import Chat, Message, MessageEntity, PhotoSize, User

from trolobot.commands import build_commands_router
from trolobot.config_models import Config
from trolobot.few_shot import FewShot
from trolobot.settings import Settings
from trolobot.timeutil import local_dt

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

    async def get_state(self, key: str) -> str | None:
        return self.state.get(key)

    async def set_state(self, key: str, value: str) -> None:
        self.state[key] = value

    async def delete_state(self, key: str) -> None:
        self.state.pop(key, None)

    async def audit_stop(self, key: str, changed_by: int, now: int) -> None:
        self.audit_stop_calls.append((key, changed_by, now))

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

    def get(self) -> Config:
        return self.cfg

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
class FakeDeps:
    """Структурно подходит под commands._CommandsDeps."""

    settings: Settings
    config_store: FakeConfigStore
    prompt_store: FakePromptStore
    db: FakeDb
    responder: object | None
    bot_user_id: int
    bot_username: str
    sticker_catalog_enabled: int = 0


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
    *, settings: Settings, config: Config, db: FakeDb | None = None
) -> tuple[FakeDeps, FakeDb, FakeConfigStore, FakePromptStore]:
    fake_db = db if db is not None else FakeDb()
    config_store = FakeConfigStore(cfg=config)
    prompt_store = FakePromptStore()
    deps = FakeDeps(
        settings=settings,
        config_store=config_store,
        prompt_store=prompt_store,
        db=fake_db,
        responder=None,
        bot_user_id=BOT_USER_ID,
        bot_username=BOT_USERNAME,
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
