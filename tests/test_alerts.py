from __future__ import annotations

import asyncio

import pytest
from aiogram.exceptions import TelegramForbiddenError
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


class FakeStore:
    def __init__(self) -> None:
        self.state: dict[str, str] = {}

    async def get_state(self, key: str) -> str | None:
        return self.state.get(key)

    async def set_state(self, key: str, value: str) -> None:
        self.state[key] = value


def _alerter(
    bot: FakeBot, store: FakeStore, *, cfg: Config | None = None, admin_user_id: int = ADMIN_ID
) -> OwnerAlerter:
    config = cfg or Config()
    return OwnerAlerter(bot=bot, db=store, admin_user_id=admin_user_id, cfg_getter=lambda: config)


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
    bot, store = FakeBot(), FakeStore()
    alerter = _alerter(bot, store)

    await alerter.provider_refused(status=402, model="m", message="no credits", now=NOW)
    await alerter.provider_refused(status=402, model="m", message="no credits", now=NOW + 3600)
    assert len(bot.sent) == 1
    assert bot.sent[0][0] == ADMIN_ID
    assert "no credits" in bot.sent[0][1]
    assert store.state["alert_last:llm_402"] == str(NOW)

    # Другой код — своя тревога; тот же после интервала — снова.
    await alerter.provider_refused(status=401, model="m", message="", now=NOW + 3600)
    await alerter.provider_refused(status=402, model="m", message="", now=NOW + 24 * 3600)
    assert len(bot.sent) == 3


async def test_transient_status_is_not_alerted() -> None:
    bot, store = FakeBot(), FakeStore()
    alerter = _alerter(bot, store)
    for status in (408, 429, 500, 503):
        await alerter.provider_refused(status=status, model="m", message="", now=NOW)
    assert bot.sent == []
    assert store.state == {}


async def test_disabled_or_no_admin_sends_nothing() -> None:
    bot, store = FakeBot(), FakeStore()
    cfg = Config()
    cfg.llm.alert_owner = False
    await _alerter(bot, store, cfg=cfg).provider_refused(status=402, model="m", message="", now=NOW)
    await _alerter(bot, store, admin_user_id=0).provider_refused(
        status=402, model="m", message="", now=NOW
    )
    assert bot.sent == []


async def test_parallel_refusals_send_one_message() -> None:
    bot, store = FakeBot(), FakeStore()
    alerter = _alerter(bot, store)
    await asyncio.gather(
        *(alerter.provider_refused(status=402, model="m", message="", now=NOW) for _ in range(5))
    )
    assert len(bot.sent) == 1


async def test_send_failure_is_logged_and_retried_next_time(
    caplog: pytest.LogCaptureFixture,
) -> None:
    error = TelegramForbiddenError(
        method=SendMessage(chat_id=ADMIN_ID, text="x"), message="Forbidden"
    )
    bot, store = FakeBot(error=error), FakeStore()
    alerter = _alerter(bot, store)

    await alerter.provider_refused(status=402, model="m", message="", now=NOW)
    assert "alert_last:llm_402" not in store.state
    assert "owner alert not sent" in caplog.text

    bot.error = None
    await alerter.provider_refused(status=402, model="m", message="", now=NOW + 60)
    assert len(bot.sent) == 1
