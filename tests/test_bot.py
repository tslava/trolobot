"""Тесты для trolobot.bot: хендлер сообщений без сети и без реального Bot.

Message/Chat/User собираются напрямую (обычные конструкторы pydantic-моделей aiogram,
без обращения к Telegram API) — этого достаточно, чтобы прогнать хендлер и проверить
запись в БД и логи.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiogram.types import Chat, Message, PhotoSize, User

from trolobot.bot import Deps, build_router
from trolobot.config_models import Config
from trolobot.db import Database
from trolobot.sanitize import stable_n
from trolobot.settings import Settings

OWN_CHAT_ID = -1001234567890
FOREIGN_CHAT_ID = -100999
NOW = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)


def _chat(chat_id: int = OWN_CHAT_ID, title: str = "АлкоПознань") -> Chat:
    return Chat(id=chat_id, type="supergroup", title=title)


def _user(user_id: int = 1, first_name: str = "Дима", last_name: str | None = None) -> User:
    return User(id=user_id, is_bot=False, first_name=first_name, last_name=last_name)


def _message(
    *,
    chat: Chat | None = None,
    from_user: User | None = None,
    message_id: int = 10,
    text: str | None = None,
    caption: str | None = None,
    photo: list[PhotoSize] | None = None,
    reply_to_message: Message | None = None,
    date: datetime = NOW,
) -> Message:
    return Message(
        message_id=message_id,
        date=date,
        chat=chat or _chat(),
        from_user=from_user,
        text=text,
        caption=caption,
        photo=photo,
        reply_to_message=reply_to_message,
    )


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "bot.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def config() -> Config:
    return Config()


def _deps(database: Database, cfg: Config, *, settings: Settings, bot_user_id: int = 999) -> Deps:
    reserved = {cfg.persona.name, cfg.persona.display_name, *cfg.persona.name_triggers}
    return Deps(
        settings=settings,
        config_getter=lambda: cfg,
        db=database,
        bot_user_id=bot_user_id,
        reserved_names=reserved,
    )


def _make_settings(monkeypatch: pytest.MonkeyPatch, allowed_chat_id: int) -> Settings:
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("ALLOWED_CHAT_ID", str(allowed_chat_id))
    return Settings()


async def test_discovery_mode_logs_and_skips_db(
    db: Database, config: Config, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=0)
    deps = _deps(db, config, settings=settings)
    router = build_router(deps)
    handler = router.message.handlers[0].callback

    message = _message(
        chat=_chat(chat_id=777, title="Неизвестный чат"),
        from_user=_user(user_id=5, first_name="Дима"),
        text="привет",
    )

    with caplog.at_level(logging.WARNING, logger="trolobot.bot"):
        await handler(message)

    assert await db.recent_messages(777, 10) == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "chat_id=777" in warnings[0].message


async def test_foreign_chat_is_silently_dropped(
    db: Database, config: Config, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID)
    deps = _deps(db, config, settings=settings)
    router = build_router(deps)
    handler = router.message.handlers[0].callback

    message = _message(
        chat=_chat(chat_id=FOREIGN_CHAT_ID),
        from_user=_user(user_id=5, first_name="Дима"),
        text="привет",
    )

    with caplog.at_level(logging.DEBUG, logger="trolobot.bot"):
        await handler(message)

    assert await db.recent_messages(FOREIGN_CHAT_ID, 10) == []
    assert caplog.records == []


async def test_own_chat_text_is_sanitized_and_stored(
    db: Database, config: Config, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID)
    deps = _deps(db, config, settings=settings)
    router = build_router(deps)
    handler = router.message.handlers[0].callback

    trigger = _message(message_id=41, text="исходное")
    message = _message(
        message_id=42,
        from_user=_user(user_id=5, first_name="Дима", last_name="007 🔥"),
        text="  Привет,\nкак дела?  ",
        reply_to_message=trigger,
    )

    with caplog.at_level(logging.INFO, logger="trolobot.bot"):
        await handler(message)

    rows = await db.recent_messages(OWN_CHAT_ID, 10)
    assert len(rows) == 1
    row = rows[0]
    assert row.tg_message_id == 42
    assert row.chat_id == OWN_CHAT_ID
    assert row.user_id == 5
    assert row.display_name == "Дима"
    assert row.text == "Привет, как дела?"
    assert row.reply_to_tg_message_id == 41
    assert row.is_bot is False
    assert row.created_at == int(NOW.timestamp())

    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1
    assert infos[0].message == "Дима: Привет, как дела?"


async def test_photo_without_caption_becomes_placeholder(
    db: Database, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID)
    deps = _deps(db, config, settings=settings)
    router = build_router(deps)
    handler = router.message.handlers[0].callback

    photo = [PhotoSize(file_id="f1", file_unique_id="u1", width=100, height=100)]
    message = _message(from_user=_user(user_id=5), photo=photo)

    await handler(message)

    rows = await db.recent_messages(OWN_CHAT_ID, 10)
    assert len(rows) == 1
    assert rows[0].text == "[фото]"


async def test_photo_with_caption_stores_normalized_caption(
    db: Database, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID)
    deps = _deps(db, config, settings=settings)
    router = build_router(deps)
    handler = router.message.handlers[0].callback

    photo = [PhotoSize(file_id="f1", file_unique_id="u1", width=100, height=100)]
    message = _message(
        from_user=_user(user_id=5), photo=photo, caption="  Смотрите,\nкакой закат  "
    )

    await handler(message)

    rows = await db.recent_messages(OWN_CHAT_ID, 10)
    assert len(rows) == 1
    assert rows[0].text == "Смотрите, какой закат"


async def test_service_message_without_text_or_media_is_not_stored(
    db: Database, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID)
    deps = _deps(db, config, settings=settings)
    router = build_router(deps)
    handler = router.message.handlers[0].callback

    message = _message(from_user=_user(user_id=5))  # нет text/caption/media

    await handler(message)

    assert await db.recent_messages(OWN_CHAT_ID, 10) == []


async def test_edited_message_is_ignored(
    db: Database, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID)
    deps = _deps(db, config, settings=settings)
    router = build_router(deps)
    edited_handler = router.edited_message.handlers[0].callback

    message = _message(from_user=_user(user_id=5), text="было бы записано")

    await edited_handler(message)

    assert await db.recent_messages(OWN_CHAT_ID, 10) == []


async def test_display_name_colliding_with_reserved_becomes_participant(
    db: Database, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID)
    deps = _deps(db, config, settings=settings)
    router = build_router(deps)
    handler = router.message.handlers[0].callback

    user_id = 77
    message = _message(
        from_user=_user(user_id=user_id, first_name="Отец", last_name="Фёдор"), text="привет"
    )

    await handler(message)

    rows = await db.recent_messages(OWN_CHAT_ID, 10)
    assert len(rows) == 1
    assert rows[0].display_name == f"Участник {stable_n(user_id)}"


async def test_anonymous_from_user_none_uses_user_id_zero(
    db: Database, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID)
    deps = _deps(db, config, settings=settings)
    router = build_router(deps)
    handler = router.message.handlers[0].callback

    message = _message(from_user=None, text="анонимный админ")

    await handler(message)

    rows = await db.recent_messages(OWN_CHAT_ID, 10)
    assert len(rows) == 1
    assert rows[0].user_id == 0
    assert rows[0].display_name == f"Участник {stable_n(0)}"
    assert rows[0].is_bot is False
