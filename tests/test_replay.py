"""Тесты для trolobot.replay.

ReplayState — чистый класс состояния реплея, тестируется без gate.py/patterns.py.
run_replay() тянет за собой gate.py и patterns.py (пишутся параллельно другими
агентами) — полный прогон включается сам, как только они появятся на диске.
"""

import argparse
import asyncio
import importlib.util
import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from trolobot import replay as replay_module
from trolobot.db import Database
from trolobot.gate_types import GateMessage, StateChange, Trigger
from trolobot.replay import ReplayState, run_replay

TZ = "Europe/Warsaw"

_GATE_AND_PATTERNS_AVAILABLE = (
    importlib.util.find_spec("trolobot.gate") is not None
    and importlib.util.find_spec("trolobot.patterns") is not None
)


def _gate_msg(user_id: int, created_at: int, tg_message_id: int = 1) -> GateMessage:
    return GateMessage(
        chat_id=0,
        tg_message_id=tg_message_id,
        user_id=user_id,
        is_bot=False,
        text="привет",
        reply_to_bot=False,
        created_at=created_at,
    )


# --- ReplayState.recent (окно live_talk) ------------------------------------


def test_recent_window_prunes_old_messages() -> None:
    state = ReplayState(TZ, window_min=10)
    base = 1_000_000

    state.push_recent(user_id=1, created_at=base)
    state.push_recent(user_id=2, created_at=base + 60)
    # +11 минут от первого сообщения -> оно уже вне окна 10 минут.
    state.push_recent(user_id=3, created_at=base + 11 * 60)

    window = state.recent_window(base + 11 * 60)
    assert [activity.user_id for activity in window] == [2, 3]


def test_recent_window_includes_current_message() -> None:
    state = ReplayState(TZ, window_min=10)
    state.push_recent(user_id=1, created_at=1_000_000)
    window = state.recent_window(1_000_000)
    assert len(window) == 1
    assert window[0].user_id == 1


def test_gate_state_reflects_recent_and_counters() -> None:
    state = ReplayState(TZ, window_min=10)
    now = 1_757_000_000  # произвольный, но фиксированный момент
    state.push_recent(user_id=1, created_at=now)

    msg = _gate_msg(user_id=1, created_at=now)
    gate_state = state.gate_state(msg, now)

    assert len(gate_state.recent) == 1
    assert gate_state.mention_count_today == 0
    assert gate_state.ambient_count_today == 0
    assert gate_state.last_mention_reply_at is None
    assert gate_state.last_mention_reply_at_user is None


# --- ReplayState.apply_pass (счётчики и last_*_at) --------------------------


@pytest.mark.parametrize("trigger", [Trigger.MENTION, Trigger.REPLY, Trigger.NAME])
def test_apply_pass_increments_mention_counters(trigger: Trigger) -> None:
    state = ReplayState(TZ, window_min=10)
    now = 1_757_000_000
    msg = _gate_msg(user_id=7, created_at=now)

    state.apply_pass(trigger, msg, now)

    gate_state = state.gate_state(msg, now)
    assert gate_state.mention_count_today == 1
    assert gate_state.ambient_count_today == 0
    assert gate_state.last_mention_reply_at == now
    assert gate_state.last_mention_reply_at_user == now


def test_apply_pass_ambient_increments_ambient_counter_only() -> None:
    state = ReplayState(TZ, window_min=10)
    now = 1_757_000_000
    msg = _gate_msg(user_id=7, created_at=now)

    state.apply_pass(Trigger.AMBIENT, msg, now)

    gate_state = state.gate_state(msg, now)
    assert gate_state.ambient_count_today == 1
    assert gate_state.mention_count_today == 0
    assert gate_state.last_mention_reply_at is None
    assert gate_state.last_mention_reply_at_user is None


def test_apply_pass_counters_are_per_day() -> None:
    state = ReplayState(TZ, window_min=10)
    # 2026-09-10 23:50 и 2026-09-11 00:10 Europe/Warsaw -> разные сутки.
    from datetime import datetime
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(TZ)
    day1 = int(datetime.fromisoformat("2026-09-10T23:50:00").replace(tzinfo=zone).timestamp())
    day2 = int(datetime.fromisoformat("2026-09-11T00:10:00").replace(tzinfo=zone).timestamp())

    msg1 = _gate_msg(user_id=1, created_at=day1)
    msg2 = _gate_msg(user_id=1, created_at=day2)

    state.apply_pass(Trigger.AMBIENT, msg1, day1)
    state.apply_pass(Trigger.AMBIENT, msg2, day2)

    assert state.gate_state(msg1, day1).ambient_count_today == 1
    assert state.gate_state(msg2, day2).ambient_count_today == 1


# --- ReplayState.apply_state_changes -----------------------------------------


def test_apply_state_changes_sets_topic_cooldown() -> None:
    state = ReplayState(TZ, window_min=10)
    state.apply_state_changes((StateChange("topic_cooldown_until", "123456"),))
    assert state.topic_cooldown_until == 123456


def test_apply_state_changes_clears_topic_cooldown() -> None:
    state = ReplayState(TZ, window_min=10)
    state.apply_state_changes((StateChange("topic_cooldown_until", "123456"),))
    state.apply_state_changes((StateChange("topic_cooldown_until", None),))
    assert state.topic_cooldown_until is None


# --- run_replay: полный прогон, требует gate.py и patterns.py ---------------


def _write_synthetic_export(path: Path, tz: str) -> None:
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    start = datetime(2026, 9, 10, 18, 0, 0, tzinfo=ZoneInfo(tz))
    authors = [("Дима", "user111"), ("Аня", "user222")]
    messages = []
    for i in range(6):
        name, from_id = authors[i % 2]
        when = start + timedelta(minutes=i)
        messages.append(
            {
                "id": i + 1,
                "type": "message",
                "date": when.strftime("%Y-%m-%dT%H:%M:%S"),
                "from": name,
                "from_id": from_id,
                "text": f"сообщение номер {i + 1}",
            }
        )
    payload = {"name": "test", "type": "private_group", "id": 1, "messages": messages}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.mark.skipif(
    not _GATE_AND_PATTERNS_AVAILABLE,
    reason="trolobot.gate и/или trolobot.patterns ещё не написаны",
)
def test_run_replay_produces_day_line_and_summary(tmp_path: Path) -> None:
    export_path = tmp_path / "result.json"
    _write_synthetic_export(export_path, TZ)

    config_path = Path(__file__).resolve().parent.parent / "config.yaml"

    args = argparse.Namespace(
        source=export_path,
        config=config_path,
        seed=1,
        bot_username="otec_fedor_bot",
        bot_user_id=0,
        verbose=False,
        generate=False,
        judge=False,
        max_calls=20,
    )

    report = run_replay(args)

    assert "Итого:" in report
    assert "2026-09-10" in report


# --- run_replay --generate: реальная генерация + фильтр поверх PASS -----------


def _write_name_trigger_export(path: Path, tz: str) -> None:
    """Один PASS по имени-триггеру ("федя") — гарантированный, без live-talk/кубика."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    when = datetime(2026, 9, 10, 18, 0, 0, tzinfo=ZoneInfo(tz))
    payload = {
        "name": "test",
        "type": "private_group",
        "id": 1,
        "messages": [
            {
                "id": 1,
                "type": "message",
                "date": when.strftime("%Y-%m-%dT%H:%M:%S"),
                "from": "Дима",
                "from_id": "user111",
                "text": "федя привет как дела",
            }
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_minimal_generate_config(path: Path) -> None:
    path.write_text('llm:\n  main_model: "test/model"\n', encoding="utf-8")


@pytest.mark.skipif(
    not _GATE_AND_PATTERNS_AVAILABLE,
    reason="trolobot.gate и/или trolobot.patterns ещё не написаны",
)
def test_run_replay_generate_prints_reply_and_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123:test-token")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key")

    export_path = tmp_path / "result.json"
    _write_name_trigger_export(export_path, TZ)
    config_path = tmp_path / "config.yaml"
    _write_minimal_generate_config(config_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        body = {
            "choices": [{"message": {"content": '{"speak": true, "text": "Бывает, дед."}'}}],
            "usage": {"cost": 0.0007, "prompt_tokens": 42, "completion_tokens": 7},
        }
        return httpx.Response(200, json=body)

    mock_transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*_args: object, **_kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=mock_transport)

    monkeypatch.setattr(replay_module.httpx, "AsyncClient", fake_async_client)

    args = argparse.Namespace(
        source=export_path,
        config=config_path,
        seed=1,
        bot_username="otec_fedor_bot",
        bot_user_id=0,
        verbose=False,
        generate=True,
        judge=False,
        max_calls=20,
    )

    report = run_replay(args)

    assert "Генерация:" in report
    assert "федя привет как дела" in report
    assert "Бывает, дед." in report
    assert "| pass" in report
    assert "вызовов=1, отправлено бы=1" in report
    assert "потрачено $=0.0007" in report


def test_run_replay_generate_without_api_key_raises_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # cwd без .env: гарантирует, что реальный .env репозитория (со своим ключом) не
    # подмешается — Settings() должна честно увидеть openrouter_api_key=None.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_TOKEN", "123:test-token")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    export_path = tmp_path / "result.json"
    _write_name_trigger_export(export_path, TZ)
    config_path = tmp_path / "config.yaml"
    _write_minimal_generate_config(config_path)

    args = argparse.Namespace(
        source=export_path,
        config=config_path,
        seed=1,
        bot_username="otec_fedor_bot",
        bot_user_id=0,
        verbose=False,
        generate=True,
        judge=False,
        max_calls=20,
    )

    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        run_replay(args)


# --- run_replay --generate --max-calls: потолок по реальным сетевым вызовам --------


def _write_repeated_name_trigger_export(path: Path, tz: str, count: int) -> None:
    """``count`` гарантированных PASS через имя-триггер "федя", разнесённых по времени
    (сам тест обнуляет mention_cooldown_sec/mention_chat_cooldown_sec, чтобы кулдаун
    обращений их не блокировал)."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    start = datetime(2026, 9, 10, 18, 0, 0, tzinfo=ZoneInfo(tz))
    authors = [("Дима", "user111"), ("Аня", "user222")]
    messages = []
    for i in range(count):
        name, from_id = authors[i % 2]
        when = start + timedelta(minutes=2 * i)
        messages.append(
            {
                "id": i + 1,
                "type": "message",
                "date": when.strftime("%Y-%m-%dT%H:%M:%S"),
                "from": name,
                "from_id": from_id,
                "text": f"федя привет как дела номер {i + 1}",
            }
        )
    payload = {"name": "test", "type": "private_group", "id": 1, "messages": messages}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_max_calls_config(path: Path) -> None:
    path.write_text(
        "llm:\n"
        '  main_model: "test/model"\n'
        '  judge_model: "test/judge-model"\n'
        "behaviour:\n"
        "  mention_cooldown_sec: 0\n"
        "  mention_chat_cooldown_sec: 0\n",
        encoding="utf-8",
    )


def test_run_replay_generate_max_calls_counts_real_network_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--max-calls считается по реальным сетевым вызовам (основной + судья), не по
    числу PASS с генерацией.

    Экспорт даёт 6 гарантированных PASS. filters.shadow=true по умолчанию (config
    минимальный, filters не переопределены) -> судья вызывается на каждой генерации,
    прошедшей проверку перед стартом. PASS 1: calls_so_far=0 < 3 -> генерация, счётчик
    в сторе становится 2 (основной + судья). PASS 2: calls_so_far=2 всё ещё < 3 ->
    ещё одна генерация, счётчик становится 4. С PASS 3 по PASS 6: calls_so_far=4 >= 3
    -> генерация не запускается. Итог: 4 реальных сетевых вызова — на один судейский
    вызов больше заявленного потолка 3, это допустимое превышение (см. replay.py).
    """
    monkeypatch.setenv("BOT_TOKEN", "123:test-token")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key")

    export_path = tmp_path / "result.json"
    _write_repeated_name_trigger_export(export_path, TZ, count=6)
    config_path = tmp_path / "config.yaml"
    _write_max_calls_config(config_path)

    call_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        body = {
            "choices": [{"message": {"content": '{"speak": true, "text": "Бывает, дед."}'}}],
            "usage": {"cost": 0.0001, "prompt_tokens": 10, "completion_tokens": 5},
        }
        return httpx.Response(200, json=body)

    mock_transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*_args: object, **_kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=mock_transport)

    monkeypatch.setattr(replay_module.httpx, "AsyncClient", fake_async_client)

    args = argparse.Namespace(
        source=export_path,
        config=config_path,
        seed=1,
        bot_username="otec_fedor_bot",
        bot_user_id=0,
        verbose=False,
        generate=True,
        judge=True,
        max_calls=3,
    )

    report = run_replay(args)

    assert call_count <= 4
    assert "вызовов=" in report
    reported_calls = int(report.split("вызовов=")[1].split(",")[0])
    assert reported_calls == call_count
    assert reported_calls <= 4


# --- run_replay: источник — база самого бота (bot.db), не только экспорт -----------


async def _seed_bot_db(db_path: Path, tz: str) -> None:
    """8 сообщений от двух людей + 1 от бота, на двух разных локальных сутках.

    Владелец не всегда может выгрузить историю из Telegram Desktop (запрет на
    сохранение контента в группе) — вместо этого реплей читает саму базу бота.
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(tz)
    day1 = datetime(2026, 9, 8, 18, 0, 0, tzinfo=zone)
    day2 = datetime(2026, 9, 10, 18, 0, 0, tzinfo=zone)
    authors = [(111, "Дима"), (222, "Аня")]

    db = Database(db_path)
    await db.connect()
    try:
        for i in range(4):
            user_id, name = authors[i % 2]
            await db.insert_message(
                tg_message_id=i + 1,
                chat_id=555,
                user_id=user_id,
                display_name=name,
                text=f"день первый, сообщение {i + 1}",
                reply_to_tg_message_id=None,
                is_bot=False,
                created_at=int((day1 + timedelta(minutes=i)).timestamp()),
            )
        for i in range(4):
            user_id, name = authors[i % 2]
            await db.insert_message(
                tg_message_id=i + 5,
                chat_id=555,
                user_id=user_id,
                display_name=name,
                text=f"день второй, сообщение {i + 1}",
                reply_to_tg_message_id=None,
                is_bot=False,
                created_at=int((day2 + timedelta(minutes=i)).timestamp()),
            )
        await db.insert_message(
            tg_message_id=9,
            chat_id=555,
            user_id=999,
            display_name="Отец Фёдор",
            text="И тебе не хворать",
            reply_to_tg_message_id=None,
            is_bot=True,
            created_at=int((day2 + timedelta(minutes=5)).timestamp()),
        )
    finally:
        await db.close()


def _seed_bot_db_sync(db_path: Path, tz: str) -> None:
    asyncio.run(_seed_bot_db(db_path, tz))


def _db_args(source: Path, **overrides: object) -> argparse.Namespace:
    config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    base: dict[str, object] = {
        "source": source,
        "config": config_path,
        "seed": 1,
        "bot_username": "otec_fedor_bot",
        "bot_user_id": 0,
        "from_db": False,
        "chat_id": None,
        "since": None,
        "until": None,
        "verbose": False,
        "generate": False,
        "judge": False,
        "max_calls": 20,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.mark.skipif(
    not _GATE_AND_PATTERNS_AVAILABLE,
    reason="trolobot.gate и/или trolobot.patterns ещё не написаны",
)
def test_run_replay_from_bot_db_produces_day_line_and_summary(tmp_path: Path) -> None:
    db_path = tmp_path / "bot.db"
    _seed_bot_db_sync(db_path, TZ)

    report = run_replay(_db_args(db_path))

    assert "Итого:" in report
    assert "2026-09-08" in report
    assert "2026-09-10" in report


@pytest.mark.skipif(
    not _GATE_AND_PATTERNS_AVAILABLE,
    reason="trolobot.gate и/или trolobot.patterns ещё не написаны",
)
def test_run_replay_from_bot_db_since_cuts_off_early_messages(tmp_path: Path) -> None:
    db_path = tmp_path / "bot.db"
    _seed_bot_db_sync(db_path, TZ)

    report = run_replay(_db_args(db_path, since=date(2026, 9, 9)))

    assert "2026-09-10" in report
    assert "2026-09-08" not in report


@pytest.mark.skipif(
    not _GATE_AND_PATTERNS_AVAILABLE,
    reason="trolobot.gate и/или trolobot.patterns ещё не написаны",
)
def test_run_replay_from_bot_db_does_not_modify_live_file(tmp_path: Path) -> None:
    """Реплей читает базу бота read-only (sqlite3 URI mode=ro) — живой файл (и его
    WAL) не должен меняться: ни размер, ни mtime."""
    db_path = tmp_path / "bot.db"
    _seed_bot_db_sync(db_path, TZ)

    stat_before = db_path.stat()

    run_replay(_db_args(db_path))

    stat_after = db_path.stat()
    assert stat_before.st_mtime_ns == stat_after.st_mtime_ns
    assert stat_before.st_size == stat_after.st_size
