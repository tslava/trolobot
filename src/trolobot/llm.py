"""LLMClient — единственная точка вызова модели во всём проекте (CLAUDE.md, этап 3).

Порядок проверок и побочных эффектов в call() — контракт: circuit -> calls_cap ->
budget -> increment llm_calls (до запроса) -> POST. Ретраев нет ни на одном слое
(PLAN.md, этап 3, «Защита бюджета»).

Зависимость от БД сведена к протоколу _StateStore (get_state/set_state/increment_state/
add_state_float), а не к конкретному Database: increment_state/add_state_float пишутся
параллельно другим агентом, а тесты этого модуля не должны от них зависеть — им достаточно
структурного соответствия сигнатуре.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from trolobot.config_models import Config
from trolobot.timeutil import day_key

logger = logging.getLogger(__name__)

_URL = "https://openrouter.ai/api/v1/chat/completions"
_LOG_BODY_LIMIT = 200
_TEMPERATURE = 0.8


class _StateStore(Protocol):
    """Подмножество методов Database, нужное LLMClient. См. db.py: get_state/set_state
    уже есть; increment_state/add_state_float — по контракту CLAUDE.md, этап 3."""

    async def get_state(self, key: str) -> str | None: ...

    async def set_state(self, key: str, value: str) -> None: ...

    async def increment_state(self, key: str, by: int = 1) -> int: ...

    async def add_state_float(self, key: str, by: float) -> float: ...


@dataclass(frozen=True, slots=True)
class LLMResult:
    text: str
    cost_usd: float
    prompt_tokens: int
    completion_tokens: int


class LLMError(Exception):
    """reason — один из "llm:timeout" | "llm:http" | "llm:budget" | "llm:calls_cap" |
    "llm:circuit_open" | "llm:empty"."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _truncate_body(text: str) -> str:
    return text[:_LOG_BODY_LIMIT]


class LLMClient:
    def __init__(
        self,
        api_key: str,
        cfg_getter: Callable[[], Config],
        db: _StateStore,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._cfg_getter = cfg_getter
        self._db = db
        self._http = (
            http if http is not None else httpx.AsyncClient(timeout=cfg_getter().llm.timeout_sec)
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def call(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        max_tokens: int,
        now: int,
    ) -> LLMResult:
        if not model:
            # Пустая модель — ошибка конфигурации (main_model ещё не выбран), а не
            # сбой вызова: пусть решает вызывающий (Responder), ретраев тут нет.
            raise ValueError("LLMClient.call: model must not be empty")

        cfg = self._cfg_getter()
        tz = cfg.persona.timezone

        circuit_until_raw = await self._db.get_state("llm_circuit_until")
        if circuit_until_raw is not None and int(circuit_until_raw) > now:
            raise LLMError("llm:circuit_open")

        calls_key = day_key("llm_calls", now, tz)
        calls_raw = await self._db.get_state(calls_key)
        calls_count = int(calls_raw) if calls_raw is not None else 0
        if calls_count >= cfg.llm.daily_calls_cap:
            raise LLMError("llm:calls_cap")

        spent_key = day_key("llm_spent_usd", now, tz)
        spent_raw = await self._db.get_state(spent_key)
        spent = float(spent_raw) if spent_raw is not None else 0.0
        if spent >= cfg.llm.daily_budget_usd:
            raise LLMError("llm:budget")

        # Считается по попыткам, не по успехам: инкремент строго до запроса.
        await self._db.increment_state(calls_key, by=1)

        body = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": _TEMPERATURE,
            "usage": {"include": True},
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "X-Title": "trolobot",
        }

        try:
            response = await self._http.post(
                _URL, json=body, headers=headers, timeout=cfg.llm.timeout_sec
            )
        except httpx.TimeoutException:
            logger.warning("llm timeout: model=%s timeout_sec=%s", model, cfg.llm.timeout_sec)
            await self._register_error(now, cfg)
            raise LLMError("llm:timeout") from None
        except httpx.HTTPError as exc:
            logger.warning("llm transport error: model=%s error=%s", model, type(exc).__name__)
            await self._register_error(now, cfg)
            raise LLMError("llm:http") from None

        if not (200 <= response.status_code < 300):
            logger.warning(
                "llm http error: status=%s body=%s",
                response.status_code,
                _truncate_body(response.text),
            )
            await self._register_error(now, cfg)
            raise LLMError("llm:http")

        try:
            data: Any = response.json()
        except ValueError:
            logger.warning(
                "llm invalid json: status=%s body=%s",
                response.status_code,
                _truncate_body(response.text),
            )
            await self._register_error(now, cfg)
            raise LLMError("llm:http") from None

        try:
            text = str(data["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError):
            logger.warning(
                "llm malformed response: status=%s body=%s",
                response.status_code,
                _truncate_body(response.text),
            )
            await self._register_error(now, cfg)
            raise LLMError("llm:http") from None

        usage = data.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        cost_raw = usage.get("cost")
        if cost_raw is None:
            # Fallback-стоимость: используется, только если провайдер не вернул
            # usage.cost. Цены из cfg.llm.price_in_usd_per_1m/price_out_usd_per_1m
            # (за 1M токенов), меняются через /set без рестарта.
            cost = (
                prompt_tokens * cfg.llm.price_in_usd_per_1m
                + completion_tokens * cfg.llm.price_out_usd_per_1m
            ) / 1_000_000
            logger.debug(
                "llm usage.cost missing, using price fallback: model=%s prompt_tokens=%s "
                "completion_tokens=%s cost_usd=%.6f",
                model,
                prompt_tokens,
                completion_tokens,
                cost,
            )
        else:
            cost = float(cost_raw)

        # Реальный запрос уже состоялся и стоил денег независимо от того, что
        # оказалось в content — считаем трату всегда.
        await self._db.add_state_float(spent_key, cost)

        if not text:
            # Пустой ответ — не сбой сети, серию ошибок (streak) не трогаем.
            raise LLMError("llm:empty")

        await self._db.set_state("llm_error_streak", "0")
        logger.info(
            "llm call ok: model=%s prompt_tokens=%s completion_tokens=%s cost_usd=%.6f",
            model,
            prompt_tokens,
            completion_tokens,
            cost,
        )
        return LLMResult(
            text=text,
            cost_usd=cost,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    async def _register_error(self, now: int, cfg: Config) -> None:
        streak = await self._db.increment_state("llm_error_streak", by=1)
        if streak >= cfg.llm.circuit_errors:
            circuit_until = now + cfg.llm.circuit_pause_min * 60
            await self._db.set_state("llm_circuit_until", str(circuit_until))
            logger.warning(
                "llm circuit opened: streak=%s pause_min=%s until=%s",
                streak,
                cfg.llm.circuit_pause_min,
                circuit_until,
            )
