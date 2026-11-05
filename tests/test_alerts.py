from __future__ import annotations

import asyncio

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import SendMessage

from trolobot.alerts import OwnerAlerter, is_refusal, render_alert
from trolobot.config_models import Config

ADMIN_ID = 42
NOW = 1_793_000_000


class FakeBot:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self.error = error

    async def send_message(self, chat_id: int, text: str) -> object:
        await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        self.sent.append((chat_id, text))
        return object()


class HangingBot:
    """Telegram, который не отвечает: send_message висит, пока его не отменят."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def send_message(self, chat_id: int, text: str) -> object:
        self.started.set()
        await asyncio.Event().wait()
        return object()


class FakeStore:
    def __init__(self) -> None:
        self.state: dict[str, str] = {}

    async def get_state(self, key: str) -> str | None:
        return self.state.get(key)

    async def set_state(self, key: str, value: str) -> None:
        self.state[key] = value


class Clock:
    def __init__(self, now: int = NOW) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def _alerter(
    bot: FakeBot | HangingBot,
    store: FakeStore,
    *,
    cfg: Config | None = None,
    admin_user_id: int = ADMIN_ID,
    clock: Clock | None = None,
) -> OwnerAlerter:
    config = cfg or Config()
    return OwnerAlerter(
        bot=bot,
        db=store,
        admin_user_id=admin_user_id,
        cfg_getter=lambda: config,
        clock=clock or Clock(),
    )


async def _refuse(alerter: OwnerAlerter, status: int = 402, message: str = "") -> None:
    await alerter.provider_refused(status=status, model="m", message=message)
    # Отправка — фоновая задача: дать ей отработать.
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, True),
        (401, True),
        (402, True),
        (403, True),
        (404, True),
        (408, False),
        (429, False),
        (500, False),
        (502, False),
        (200, False),
    ],
)
def test_is_refusal(status: int, expected: bool) -> None:
    assert is_refusal(status) is expected


def test_render_alert_has_hint_model_and_message() -> None:
    text = render_alert(
        status=402, model="anthropic/claude-opus-5", message="x" * 500, interval_hours=24
    )
    assert "402" in text
    assert "openrouter.ai/settings/credits" in text
    assert "anthropic/claude-opus-5" in text
    assert "x" * 200 in text and "x" * 201 not in text
    assert "24 ч" in text


def test_render_alert_without_message_and_unknown_status() -> None:
    text = render_alert(status=404, model="m", message="", interval_hours=6)
    assert "404, запрос отклонён" in text
    assert "Ответ:" not in text


async def test_refusal_sends_once_per_interval_per_status() -> None:
    bot, store, clock = FakeBot(), FakeStore(), Clock()
    alerter = _alerter(bot, store, clock=clock)

    await _refuse(alerter, message="no credits")
    clock.now = NOW + 3600
    await _refuse(alerter, message="no credits")
    assert len(bot.sent) == 1
    assert bot.sent[0][0] == ADMIN_ID
    assert "no credits" in bot.sent[0][1]
    assert store.state["alert_next:llm_402"] == str(NOW + 24 * 3600)

    # Другой код — своя тревога; тот же после интервала — снова.
    await _refuse(alerter, status=401)
    clock.now = NOW + 24 * 3600
    await _refuse(alerter)
    assert len(bot.sent) == 3


async def test_interval_uses_own_clock_not_caller_time() -> None:
    # Хук не получает now вызывающего (у описания фото это дата старого сообщения):
    # пауза между тревогами считается только по часам тревоги.
    bot, store, clock = FakeBot(), FakeStore(), Clock()
    alerter = _alerter(bot, store, clock=clock)
    await _refuse(alerter)
    clock.now = NOW + 60
    await _refuse(alerter)
    assert len(bot.sent) == 1


async def test_transient_status_is_not_alerted() -> None:
    bot, store = FakeBot(), FakeStore()
    alerter = _alerter(bot, store)
    for status in (408, 429, 500, 503):
        await _refuse(alerter, status=status)
    assert bot.sent == []
    assert store.state == {}


async def test_disabled_or_no_admin_sends_nothing() -> None:
    bot, store = FakeBot(), FakeStore()
    cfg = Config()
    cfg.llm.alert_owner = False
    await _refuse(_alerter(bot, store, cfg=cfg))
    await _refuse(_alerter(bot, store, admin_user_id=0))
    assert bot.sent == []


async def test_parallel_refusals_send_one_message() -> None:
    bot, store = FakeBot(), FakeStore()
    alerter = _alerter(bot, store)
    await asyncio.gather(
        *(alerter.provider_refused(status=402, model="m", message="") for _ in range(5))
    )
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(bot.sent) == 1


async def test_hanging_telegram_does_not_block_caller() -> None:
    bot, store = HangingBot(), FakeStore()
    alerter = _alerter(bot, store)
    # Вызов модели ждёт только занятия слота, не отправки.
    await asyncio.wait_for(alerter.provider_refused(status=402, model="m", message=""), 1)
    await asyncio.wait_for(bot.started.wait(), 1)
    # Слот занят до отправки: второй отказ не ждёт и не шлёт повтор.
    await asyncio.wait_for(alerter.provider_refused(status=402, model="m", message=""), 1)
    assert store.state["alert_next:llm_402"] == str(NOW + 24 * 3600)

    # Остановка посреди отправки: повтор через час, а не через сутки.
    await alerter.shutdown()
    assert store.state["alert_next:llm_402"] == str(NOW + 3600)


async def test_send_failure_retries_in_an_hour(caplog: pytest.LogCaptureFixture) -> None:
    error = TelegramNetworkError(method=SendMessage(chat_id=ADMIN_ID, text="x"), message="timeout")
    bot, store, clock = FakeBot(error=error), FakeStore(), Clock()
    alerter = _alerter(bot, store, clock=clock)

    await _refuse(alerter)
    assert store.state["alert_next:llm_402"] == str(NOW + 3600)
    assert "owner alert not sent" in caplog.text

    bot.error = None
    clock.now = NOW + 1800
    await _refuse(alerter)
    assert bot.sent == []
    clock.now = NOW + 3600
    await _refuse(alerter)
    assert len(bot.sent) == 1


async def test_forbidden_also_retries_later() -> None:
    error = TelegramForbiddenError(
        method=SendMessage(chat_id=ADMIN_ID, text="x"), message="Forbidden"
    )
    bot, store = FakeBot(error=error), FakeStore()
    await _refuse(_alerter(bot, store))
    assert store.state["alert_next:llm_402"] == str(NOW + 3600)
