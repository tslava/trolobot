"""Сквозной тест этапа 6: настоящие Database/ConfigStore/PromptStore и оба роутера
(build_commands_router, build_router), а не подделки протоколов commands.py.

Отличие от tests/test_commands.py (тот проверяет командный роутер в изоляции,
на структурных фейках _DbLike/_ConfigStoreLike/_PromptStoreLike) — здесь вся
цепочка "команда владельца/чата -> ConfigStore/PromptStore/Database -> обратно
видно в config_store.get()/gate" прогоняется целиком, на временной БД (tmp_path),
но настоящих config.yaml/prompts/system.txt/few_shot.yaml из корня репозитория —
их эти тесты только читают, ConfigStore.set()/PromptStore пишут исключительно
в БД (yaml и txt/yaml-файлы не трогаются ни разу).
"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, MessageEntity, Update, User

from trolobot.bot import Deps, build_router
from trolobot.commands import build_commands_router
from trolobot.db import Database
from trolobot.gate import should_consider
from trolobot.gate_state import load_gate_state
from trolobot.gate_types import GateMessage, Verdict
from trolobot.settings import Settings
from trolobot.stores import ConfigStore, PromptStore
from trolobot.timeutil import local_dt

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _REPO_ROOT / "config.yaml"
_PROMPT_PATH = _REPO_ROOT / "prompts" / "system.txt"
_FEW_SHOT_PATH = _REPO_ROOT / "few_shot.yaml"

OWN_CHAT_ID = -1001234567890
ADMIN_ID = 555
BOT_USER_ID = 999
BOT_USERNAME = "fedorbot"
NOW = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
NOW_TS = int(NOW.timestamp())


def _chat(chat_id: int = OWN_CHAT_ID) -> Chat:
    return Chat(id=chat_id, type="supergroup", title="АлкоПознань")


def _private_chat() -> Chat:
    return Chat(id=ADMIN_ID, type="private")


def _user(user_id: int, first_name: str = "Дима", last_name: str | None = None) -> User:
    return User(id=user_id, is_bot=False, first_name=first_name, last_name=last_name)


def _message(
    *,
    chat: Chat,
    from_user: User | None,
    message_id: int = 10,
    text: str | None = None,
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
        reply_to_message=reply_to_message,
        entities=entities,
    )


def _make_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("ALLOWED_CHAT_ID", str(OWN_CHAT_ID))
    monkeypatch.setenv("ADMIN_USER_ID", str(ADMIN_ID))
    return Settings()


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Перехватывает Message.answer, чтобы не дёргать реальный Bot API."""
    captured: list[str] = []

    async def fake_answer(self: Message, text: str, **kwargs: object) -> None:
        captured.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    return captured


@pytest.fixture
async def wired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Deps, Database, ConfigStore, PromptStore]]:
    """Полная проводка этапа 6 (app.py, без Bot.get_me()/polling) на временной БД."""
    settings = _make_settings(monkeypatch)
    db = Database(tmp_path / "bot.db")
    await db.connect()

    config_store = ConfigStore(_CONFIG_PATH, db)
    await config_store.load()

    prompt_store = PromptStore(db, _PROMPT_PATH, _FEW_SHOT_PATH)
    await prompt_store.load()

    config_store.set_bot_username(BOT_USERNAME)
    cfg = config_store.get()
    reserved = {
        cfg.persona.name,
        cfg.persona.display_name,
        *cfg.persona.name_triggers,
        BOT_USERNAME,
    }

    deps = Deps(
        settings=settings,
        config_getter=config_store.get,
        db=db,
        bot_user_id=BOT_USER_ID,
        reserved_names=reserved,
        patterns_getter=config_store.patterns,
        rng=random.Random(0),
        clock=lambda: 0,
        config_store=config_store,
        prompt_store=prompt_store,
        bot_username=BOT_USERNAME,
    )
    try:
        yield deps, db, config_store, prompt_store
    finally:
        await db.close()


def _commands_handler(deps: Deps):  # type: ignore[no-untyped-def]
    router = build_commands_router(deps)
    return router.message.handlers[0].callback


def _main_handler(deps: Deps):  # type: ignore[no-untyped-def]
    router = build_router(deps)
    return router.message.handlers[0].callback


# --- /set, /unset: ConfigStore.get() меняется, override в БД, аудит, patterns ------


async def test_set_writes_override_audits_and_rebuilds_patterns(
    wired: tuple[Deps, Database, ConfigStore, PromptStore], sent: list[str]
) -> None:
    deps, db, config_store, _prompt_store = wired
    handler = _commands_handler(deps)

    assert config_store.get().behaviour.daily_cap == 3  # дефолт из config.yaml

    message = _message(
        chat=_private_chat(),
        from_user=_user(ADMIN_ID, "Владелец"),
        text="/set behaviour.daily_cap 7",
    )
    await handler(message)

    assert config_store.get().behaviour.daily_cap == 7
    assert sent == ["behaviour.daily_cap: None → 7"]

    overrides = await db.get_overrides()
    assert overrides["behaviour.daily_cap"] == "7"

    conn = db._conn
    assert conn is not None
    cursor = await conn.execute(
        "SELECT key, old_value, new_value, changed_by FROM config_audit "
        "WHERE key = 'behaviour.daily_cap'"
    )
    rows = await cursor.fetchall()
    assert len(rows) == 1
    assert rows[0]["old_value"] is None
    assert rows[0]["new_value"] == "7"
    assert rows[0]["changed_by"] == ADMIN_ID

    # Patterns пересобираются на каждом ConfigStore.load() (внутри .set()) — новый
    # topic_stop-паттерн должен сработать сразу после следующего /set, без рестарта.
    assert config_store.patterns().topic_stop("виджет xyz777 виджет") is None
    await handler(
        _message(
            chat=_private_chat(),
            from_user=_user(ADMIN_ID, "Владелец"),
            text="/set filters.topic_stop [xyz777]",
            message_id=11,
        )
    )
    assert config_store.patterns().topic_stop("виджет xyz777 виджет") is not None


async def test_set_topic_stop_is_seen_hot_by_real_gate_handler(
    wired: tuple[Deps, Database, ConfigStore, PromptStore], sent: list[str]
) -> None:
    """Критично (CLAUDE.md, "Интерфейсы этапа 6"): Deps.patterns_getter — не
    замороженный снимок Patterns, а геттер на config_store.patterns. Владелец
    меняет filters.topic_stop через реальный командный роутер, и САМОЕ СЛЕДУЮЩЕЕ
    обычное сообщение чата, прогнанное через настоящий build_router-хендлер (не
    через config_store.patterns() напрямую, как в тесте /set выше), должно
    словить новый паттерн в гейте — без рестарта и без пересборки Deps."""
    deps, db, config_store, _prompt_store = wired
    commands_handler = _commands_handler(deps)
    main_handler = _main_handler(deps)

    assert config_store.patterns().topic_stop("кактус зацвёл") is None

    set_message = _message(
        chat=_private_chat(),
        from_user=_user(ADMIN_ID, "Владелец"),
        text="/set filters.topic_stop ['\\bкактус\\w*']",
    )
    await commands_handler(set_message)
    assert config_store.patterns().topic_stop("кактус зацвёл") is not None

    chat_message = _message(
        chat=_chat(),
        from_user=_user(7, "Дима"),
        text="кактус зацвёл на балконе",
        message_id=300,
    )
    await main_handler(chat_message)

    summary = dict(await db.filter_log_summary(0))
    assert summary.get("gate:topic") == 1


async def test_set_llm_main_model_garbage_replies_with_error_and_does_not_write(
    wired: tuple[Deps, Database, ConfigStore, PromptStore], sent: list[str]
) -> None:
    deps, db, config_store, _prompt_store = wired
    handler = _commands_handler(deps)

    message = _message(
        chat=_private_chat(),
        from_user=_user(ADMIN_ID, "Владелец"),
        text="/set llm.main_model мусор",
    )
    await handler(message)

    assert len(sent) == 1
    assert sent[0].startswith("Ошибка")
    assert config_store.get().llm.main_model == "anthropic/claude-opus-5"
    assert "llm.main_model" not in await db.get_overrides()


async def test_unset_resets_to_yaml_default(
    wired: tuple[Deps, Database, ConfigStore, PromptStore], sent: list[str]
) -> None:
    deps, db, config_store, _prompt_store = wired
    handler = _commands_handler(deps)

    await handler(
        _message(
            chat=_private_chat(),
            from_user=_user(ADMIN_ID, "Владелец"),
            text="/set behaviour.daily_cap 7",
        )
    )
    assert config_store.get().behaviour.daily_cap == 7

    await handler(
        _message(
            chat=_private_chat(),
            from_user=_user(ADMIN_ID, "Владелец"),
            text="/unset behaviour.daily_cap",
            message_id=11,
        )
    )

    assert config_store.get().behaviour.daily_cap == 3
    assert sent[-1] == "behaviour.daily_cap: сброшен к 3"
    assert "behaviour.daily_cap" not in await db.get_overrides()


# --- /rollback --------------------------------------------------------------------


async def test_rollback_after_adding_version_switches_active(
    wired: tuple[Deps, Database, ConfigStore, PromptStore], sent: list[str]
) -> None:
    deps, db, _config_store, prompt_store = wired
    handler = _commands_handler(deps)

    assert prompt_store.prompt_version() == 1
    original_body = prompt_store.system_prompt()

    v2 = await db.add_prompt_version("Альтернативный текст промпта на пробу.", "test edit", NOW_TS)
    assert v2 == 2

    message = _message(
        chat=_private_chat(), from_user=_user(ADMIN_ID, "Владелец"), text="/rollback 1"
    )
    await handler(message)

    assert prompt_store.prompt_version() == 1
    assert prompt_store.system_prompt() == original_body
    assert sent == ["промпт: версия 1"]


# --- /ex add: реплай на настоящую запись bot_replies -------------------------------


async def test_ex_add_from_real_bot_reply_bumps_few_shot_version(
    wired: tuple[Deps, Database, ConfigStore, PromptStore], sent: list[str]
) -> None:
    deps, db, _config_store, prompt_store = wired
    handler = _commands_handler(deps)

    before_version = prompt_store.few_shot_version()
    before_count = len(prompt_store.examples())

    await db.insert_message(
        tg_message_id=50,
        chat_id=OWN_CHAT_ID,
        user_id=7,
        display_name="Дима",
        text="как дела, дед?",
        reply_to_tg_message_id=None,
        is_bot=False,
        created_at=NOW_TS,
    )
    await db.insert_bot_reply(
        tg_message_id=100,
        reply_to_tg_message_id=50,
        trigger="mention",
        trigger_tg_message_id=50,
        text="И тебе не хворать.",
        prompt_version=1,
        few_shot_version=before_version,
        delay_sec=12,
        created_at=NOW_TS,
    )

    bot_reply_msg = _message(
        chat=_chat(),
        from_user=_user(BOT_USER_ID, "Отец Фёдор"),
        text="И тебе не хворать.",
        message_id=100,
    )
    message = _message(
        chat=_chat(),
        from_user=_user(ADMIN_ID, "Владелец"),
        text="/ex add",
        reply_to_message=bot_reply_msg,
        message_id=101,
    )
    await handler(message)

    assert prompt_store.few_shot_version() == before_version + 1
    assert len(prompt_store.examples()) == before_count + 1
    added = prompt_store.examples()[-1]
    assert added.name == "Дима"
    assert added.user == "как дела, дед?"
    assert added.text == "И тебе не хворать."
    assert sent == []  # /ex add молча, без ответа в чат


# --- /stop: должно быть видно гейту следующего сообщения --------------------------


async def test_stop_command_makes_gate_drop_next_message(
    wired: tuple[Deps, Database, ConfigStore, PromptStore],
) -> None:
    deps, db, config_store, _prompt_store = wired
    handler = _commands_handler(deps)

    message = _message(chat=_chat(), from_user=_user(7, "Дима"), text="/stop")
    await handler(message)

    assert await db.get_state("stop_until") is not None

    gate_msg = GateMessage(
        chat_id=OWN_CHAT_ID,
        tg_message_id=200,
        user_id=7,
        is_bot=False,
        text="фёдор ты тут?",
        reply_to_bot=False,
        created_at=NOW_TS + 10,
    )
    cfg = config_store.get()
    state = await load_gate_state(db, cfg, gate_msg, NOW_TS + 10)
    decision = should_consider(
        gate_msg, state, cfg, config_store.patterns(), NOW_TS + 10, random.Random(0)
    )

    assert decision.verdict is Verdict.DROP
    assert decision.reason == "gate:stop"


# --- /mute: должно быть видно гейту следующего сообщения именно этого юзера -------


async def test_mute_command_makes_gate_drop_that_user(
    wired: tuple[Deps, Database, ConfigStore, PromptStore],
) -> None:
    deps, db, config_store, _prompt_store = wired
    handler = _commands_handler(deps)

    # Мьют ЧУЖОГО сообщения (реплаем) — только владелец, см. PLAN.md этап 6.
    target_msg = _message(chat=_chat(), from_user=_user(8, "Толя"), text="исходное")
    mute_msg = _message(
        chat=_chat(),
        from_user=_user(ADMIN_ID, "Владелец"),
        text="/mute",
        reply_to_message=target_msg,
        message_id=11,
    )
    await handler(mute_msg)

    muted_ids = await db.muted_user_ids()
    assert 8 in muted_ids

    gate_msg = GateMessage(
        chat_id=OWN_CHAT_ID,
        tg_message_id=201,
        user_id=8,
        is_bot=False,
        text="фёдор, слышишь?",
        reply_to_bot=False,
        created_at=NOW_TS + 10,
    )
    cfg = config_store.get()
    state = await load_gate_state(db, cfg, gate_msg, NOW_TS + 10)
    decision = should_consider(
        gate_msg, state, cfg, config_store.patterns(), NOW_TS + 10, random.Random(0)
    )

    assert decision.verdict is Verdict.DROP
    assert decision.reason == "gate:muted"


# --- /status ------------------------------------------------------------------


async def test_status_returns_text_with_prompt_and_few_shot_versions(
    wired: tuple[Deps, Database, ConfigStore, PromptStore], sent: list[str]
) -> None:
    deps, _db, _config_store, prompt_store = wired
    handler = _commands_handler(deps)

    message = _message(chat=_private_chat(), from_user=_user(ADMIN_ID, "Владелец"), text="/status")
    await handler(message)

    assert len(sent) == 1
    text = sent[0]
    assert f"v{prompt_store.prompt_version()}" in text
    assert f"v{prompt_store.few_shot_version()}" in text
    assert "Паника" in text
    assert "Стоп до" in text
    assert "Предохранитель LLM: закрыт, ошибок подряд: 0" in text


async def test_status_shows_open_circuit_and_error_streak(
    wired: tuple[Deps, Database, ConfigStore, PromptStore], sent: list[str]
) -> None:
    deps, db, config_store, _prompt_store = wired
    handler = _commands_handler(deps)

    await db.set_state("llm_circuit_until", str(NOW_TS + 1800))
    await db.set_state("llm_error_streak", "5")

    message = _message(chat=_private_chat(), from_user=_user(ADMIN_ID, "Владелец"), text="/status")
    await handler(message)

    assert len(sent) == 1
    text = sent[0]
    expected_time = local_dt(NOW_TS + 1800, config_store.get().persona.timezone).strftime("%H:%M")
    assert f"Предохранитель LLM: открыт до {expected_time}, ошибок подряд: 5" in text


# --- /resume: снимает panic/stop и предохранитель LLM целиком через реальную БД ----


async def test_resume_clears_llm_circuit_via_real_db(
    wired: tuple[Deps, Database, ConfigStore, PromptStore], sent: list[str]
) -> None:
    deps, db, _config_store, _prompt_store = wired
    handler = _commands_handler(deps)

    await db.set_state("panic", "1")
    await db.set_state("stop_until", "123")
    await db.set_state("llm_circuit_until", str(NOW_TS + 1800))
    await db.set_state("llm_error_streak", "5")

    message = _message(chat=_private_chat(), from_user=_user(ADMIN_ID, "Владелец"), text="/resume")
    await handler(message)

    assert await db.get_state("panic") is None
    assert await db.get_state("stop_until") is None
    assert await db.get_state("llm_circuit_until") is None
    assert await db.get_state("llm_error_streak") == "0"
    assert sent == ["Продолжаем. Снято: panic, stop, предохранитель LLM (было 5 ошибок подряд)."]


# --- Порядок роутеров в Dispatcher: команды раньше основного гейта ------------------


async def test_commands_router_registered_before_main_router_in_dispatcher(
    wired: tuple[Deps, Database, ConfigStore, PromptStore],
) -> None:
    """/stop в чате не должен попасть в messages/гейт основного роутера — сборка
    app.py включает build_commands_router(deps) ПЕРВЫМ. Проверяем и порядок
    sub_routers, и реальное распространение события через Dispatcher.feed_update
    (как это делает aiogram в проде) — оба способа из CLAUDE.md."""
    deps, db, _config_store, _prompt_store = wired

    commands_router = build_commands_router(deps)
    main_router = build_router(deps)

    dispatcher = Dispatcher()
    dispatcher.include_router(commands_router)
    dispatcher.include_router(main_router)

    sub_routers = list(dispatcher.sub_routers)
    assert sub_routers.index(commands_router) < sub_routers.index(main_router)

    bot = Bot(token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
    try:
        message = _message(chat=_chat(), from_user=_user(7, "Дима"), text="/stop")
        update = Update(update_id=1, message=message)

        await dispatcher.feed_update(bot, update)

        # Основной роутер (build_router) не должен был отработать: "/stop" не
        # записан в messages, и filter_log (который бы завёл гейт) пуст.
        assert await db.recent_messages(OWN_CHAT_ID, 10) == []
        assert await db.filter_log_summary(0) == []
        # Зато командный роутер отработал: stop_until установлен.
        assert await db.get_state("stop_until") is not None
    finally:
        await bot.session.close()
