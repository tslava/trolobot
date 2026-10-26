"""Сообщение владельцу в личку, когда провайдер модели отказывает (CLAUDE.md,
"Интерфейсы: тревога владельцу").

Решение владельца 26.10.2026: с 22.10 OpenRouter четыре дня отвечал 402 (кончились
кредиты), бот молчал, а заметили это только по разбору логов. Отказ провайдера
(4xx, кроме таймаута и rate limit) — не случайный сбой, сам он не пройдёт, поэтому
бот пишет владельцу сам: не чаще раза в ``llm.alert_interval_hours`` на код ответа.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol

from aiogram.exceptions import TelegramAPIError

from trolobot.config_models import Config

logger = logging.getLogger(__name__)

# Временные сбои: проходят сами, тревога по ним — шум.
_TRANSIENT_STATUSES = frozenset({408, 429})
_MESSAGE_LIMIT = 200
_HINTS = {
    401: "ключ OpenRouter не принят, проверь OPENROUTER_API_KEY",
    402: "кончились кредиты, пополни на openrouter.ai/settings/credits",
}


def is_refusal(status: int) -> bool:
    """Отказ провайдера, который сам не пройдёт: 4xx, кроме таймаута и rate limit."""
    return 400 <= status < 500 and status not in _TRANSIENT_STATUSES


def render_alert(*, status: int, model: str, message: str, interval_hours: int) -> str:
    hint = _HINTS.get(status, "запрос отклонён")
    lines = [f"OpenRouter отказал: {status}, {hint}.", f"Модель: {model}."]
    if message:
        lines.append(f"Ответ: {message[:_MESSAGE_LIMIT]}")
    lines.append(
        f"Пока это не исправить, бот молчит там, где нужна эта модель. "
        f"Следующее такое сообщение не раньше чем через {interval_hours} ч."
    )
    return "\n".join(lines)


class _SenderLike(Protocol):
    async def send_message(self, chat_id: int, text: str) -> object: ...


class _StateStore(Protocol):
    async def get_state(self, key: str) -> str | None: ...

    async def set_state(self, key: str, value: str) -> None: ...


class OwnerAlerter:
    def __init__(
        self,
        *,
        bot: _SenderLike,
        db: _StateStore,
        admin_user_id: int,
        cfg_getter: Callable[[], Config],
    ) -> None:
        self._bot = bot
        self._db = db
        self._admin_user_id = admin_user_id
        self._cfg_getter = cfg_getter
        # Параллельные вызовы модели получают 402 одновременно — без лока ушло бы
        # несколько одинаковых сообщений.
        self._lock = asyncio.Lock()

    async def provider_refused(self, *, status: int, model: str, message: str, now: int) -> None:
        cfg = self._cfg_getter().llm
        if not cfg.alert_owner or self._admin_user_id == 0 or not is_refusal(status):
            return
        key = f"alert_last:llm_{status}"
        async with self._lock:
            last_raw = await self._db.get_state(key)
            if last_raw is not None and now - int(last_raw) < cfg.alert_interval_hours * 3600:
                return
            text = render_alert(
                status=status,
                model=model,
                message=message,
                interval_hours=cfg.alert_interval_hours,
            )
            try:
                await self._bot.send_message(self._admin_user_id, text)
            except TelegramAPIError as exc:
                # Не отметили отправку — следующий отказ попробует снова.
                logger.warning("owner alert not sent: %s", type(exc).__name__)
                return
            await self._db.set_state(key, str(now))
            logger.info("owner alert sent: llm status=%s model=%s", status, model)
