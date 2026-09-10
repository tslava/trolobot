"""Тесты для trolobot.bot: хендлер сообщений без сети и без реального Bot.

Message/Chat/User собираются напрямую (обычные конструкторы pydantic-моделей aiogram,
без обращения к Telegram API) — этого достаточно, чтобы прогнать хендлер и проверить
запись в БД и логи.
"""

from __future__ import annotations

import logging
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from aiogram.types import Chat, Message, PhotoSize, User

from trolobot.bot import Deps, build_router
from trolobot.config_models import Config
from trolobot.db import Database
from trolobot.patterns import Patterns
from trolobot.sanitize import stable_n
from trolobot.settings import Settings

OWN_CHAT_ID = -1001234567890
FOREIGN_CHAT_ID = -100999
NOW = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)

WARSAW = ZoneInfo("Europe/Warsaw")
BOT_USERNAME = "fedorbot"
# День, вне quiet_window (02:00-07:00) и morning_reply_window по умолчанию.
DAY = datetime(2026, 1, 10, 15, 0, tzinfo=WARSAW)
# Ночь, внутри quiet_window по умолчанию.
NIGHT = datetime(2026, 1, 10, 3, 0, tzinfo=WARSAW)


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


def _deps(
    database: Database,
    cfg: Config,
    *,
    settings: Settings,
    bot_user_id: int = 999,
    bot_username: str = BOT_USERNAME,
    rng: random.Random | None = None,
) -> Deps:
    reserved = {cfg.persona.name, cfg.persona.display_name, *cfg.persona.name_triggers}
    patterns = Patterns(cfg.filters, cfg.persona.name_triggers, bot_username)
    return Deps(
        settings=settings,
        config_getter=lambda: cfg,
        db=database,
        bot_user_id=bot_user_id,
        reserved_names=reserved,
        patterns=patterns,
        rng=rng if rng is not None else random.Random(0),
    )


def _config_ambient_always() -> Config:
    """Config с ambient_probability=1.0 — шаг "кости" гейта детерминированно пропускает."""
    cfg = Config()
    behaviour = cfg.behaviour.model_copy(update={"ambient_probability": 1.0})
    return cfg.model_copy(update={"behaviour": behaviour})


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


# --- Гейт (этап 2), встроенный в хендлер после insert_message ---


class _RaisingPatterns:
    """Дублирует PatternsLike, но topic_stop падает — проверить, что гейт не роняет хендлер."""

    def topic_stop(self, text: str) -> str | None:
        raise RuntimeError("boom")

    def injection(self, text: str) -> str | None:
        return None

    def logistics(self, text: str) -> str | None:
        return None

    def name_trigger(self, text: str) -> str | None:
        return None

    def mentions_bot(self, text: str) -> bool:
        return False


async def test_gate_drop_not_live_for_single_message(
    db: Database, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID)
    deps = _deps(db, config, settings=settings)
    router = build_router(deps)
    handler = router.message.handlers[0].callback

    message = _message(
        from_user=_user(user_id=5, first_name="Дима"),
        text="какая погода вечером",
        date=DAY,
    )

    await handler(message)

    summary = dict(await db.filter_log_summary(0))
    assert summary.get("gate:not_live") == 1


async def test_gate_mention_during_day_passes(
    db: Database, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID)
    deps = _deps(db, config, settings=settings)
    router = build_router(deps)
    handler = router.message.handlers[0].callback

    message = _message(
        from_user=_user(user_id=5, first_name="Дима"),
        text=f"@{BOT_USERNAME} как сам?",
        date=DAY,
    )

    await handler(message)

    summary = dict(await db.filter_log_summary(0))
    assert summary.get("pass:mention") == 1

    conn = db._conn
    assert conn is not None
    cursor = await conn.execute("SELECT COUNT(*) AS cnt FROM night_queue")
    row = await cursor.fetchone()
    assert row is not None
    assert row["cnt"] == 0


async def test_gate_mention_at_night_queues(
    db: Database, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID)
    deps = _deps(db, config, settings=settings)
    router = build_router(deps)
    handler = router.message.handlers[0].callback

    message = _message(
        message_id=55,
        from_user=_user(user_id=5, first_name="Дима"),
        text=f"@{BOT_USERNAME} ты спишь?",
        date=NIGHT,
    )

    await handler(message)

    summary = dict(await db.filter_log_summary(0))
    assert summary.get("gate:night_queued") == 1

    conn = db._conn
    assert conn is not None
    cursor = await conn.execute("SELECT tg_message_id, display_name, text FROM night_queue")
    rows = await cursor.fetchall()
    assert len(rows) == 1
    assert rows[0]["tg_message_id"] == 55
    assert rows[0]["display_name"] == "Дима"


async def test_gate_reply_to_bot_passes(
    db: Database, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID)
    deps = _deps(db, config, settings=settings)
    router = build_router(deps)
    handler = router.message.handlers[0].callback

    bot_user = User(id=deps.bot_user_id, is_bot=True, first_name="Отец Фёдор")
    trigger = _message(message_id=100, from_user=bot_user, text="ну и денёк", date=DAY)
    message = _message(
        message_id=101,
        from_user=_user(user_id=5, first_name="Дима"),
        text="согласен полностью",
        reply_to_message=trigger,
        date=DAY,
    )

    await handler(message)

    summary = dict(await db.filter_log_summary(0))
    assert summary.get("pass:reply") == 1


async def test_gate_topic_stop_sets_cooldown(
    db: Database, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID)
    deps = _deps(db, config, settings=settings)
    router = build_router(deps)
    handler = router.message.handlers[0].callback

    message = _message(
        from_user=_user(user_id=5, first_name="Дима"),
        text="хватит уже про войну говорить",
        date=DAY,
    )

    await handler(message)

    summary = dict(await db.filter_log_summary(0))
    assert summary.get("gate:topic") == 1

    cooldown_raw = await db.get_state("topic_cooldown_until")
    assert cooldown_raw is not None
    assert int(cooldown_raw) > int(DAY.timestamp())


async def test_gate_drops_bot_authored_messages(
    db: Database, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID)
    deps = _deps(db, config, settings=settings)
    router = build_router(deps)
    handler = router.message.handlers[0].callback

    bot_author = User(id=deps.bot_user_id, is_bot=True, first_name="Отец Фёдор")
    message = _message(from_user=bot_author, text="сам с собой разговариваю", date=DAY)

    await handler(message)

    rows = await db.recent_messages(OWN_CHAT_ID, 10)
    assert len(rows) == 1
    assert rows[0].is_bot is True

    summary = dict(await db.filter_log_summary(0))
    assert summary.get("gate:is_bot") == 1


async def test_gate_ambient_pass_on_live_talk(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config_ambient_always()
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID)
    deps = _deps(db, cfg, settings=settings)
    router = build_router(deps)
    handler = router.message.handlers[0].callback

    users = [
        _user(user_id=5, first_name="Дима"),
        _user(user_id=6, first_name="Оля"),
        _user(user_id=5, first_name="Дима"),
    ]
    texts = ["привет всем", "как настроение", "погода класс"]
    for i, (user, text) in enumerate(zip(users, texts, strict=True)):
        message = _message(
            message_id=200 + i,
            from_user=user,
            text=text,
            date=DAY + timedelta(seconds=i * 10),
        )
        await handler(message)

    summary = dict(await db.filter_log_summary(0))
    assert summary.get("pass:ambient") == 1


async def test_gate_error_does_not_break_message_write(
    db: Database, config: Config, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _make_settings(monkeypatch, allowed_chat_id=OWN_CHAT_ID)
    deps = _deps(db, config, settings=settings)
    deps.patterns = _RaisingPatterns()
    router = build_router(deps)
    handler = router.message.handlers[0].callback

    message = _message(from_user=_user(user_id=5, first_name="Дима"), text="привет", date=DAY)

    with caplog.at_level(logging.ERROR, logger="trolobot.bot"):
        await handler(message)

    rows = await db.recent_messages(OWN_CHAT_ID, 10)
    assert len(rows) == 1
    assert rows[0].text == "привет"

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "gate failed" in errors[0].message
