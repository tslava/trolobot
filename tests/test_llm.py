from __future__ import annotations

import json
import logging
from collections.abc import Callable

import httpx
import pytest

from trolobot.config_models import Config
from trolobot.llm import LLMClient, LLMError, LLMResult
from trolobot.timeutil import day_key

API_KEY = "sk-super-secret-key-do-not-log"
NOW = 1_768_003_200  # 2026-01-10 00:00 UTC -> Europe/Warsaw дата тоже 2026-01-10
URL = "https://openrouter.ai/api/v1/chat/completions"


class FakeStore:
    """In-memory подделка Database, реализует протокол _StateStore структурно."""

    def __init__(self) -> None:
        self.state: dict[str, str] = {}

    async def get_state(self, key: str) -> str | None:
        return self.state.get(key)

    async def set_state(self, key: str, value: str) -> None:
        self.state[key] = value

    async def increment_state(self, key: str, by: int = 1) -> int:
        current = int(self.state.get(key, "0"))
        new_value = current + by
        self.state[key] = str(new_value)
        return new_value

    async def add_state_float(self, key: str, by: float) -> float:
        current = float(self.state.get(key, "0"))
        new_value = current + by
        self.state[key] = str(new_value)
        return new_value


Handler = Callable[[httpx.Request], httpx.Response]


def _counting_handler(inner: Handler) -> tuple[Handler, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return inner(request)

    return handler, calls


def _ok_response(
    *, content: str = "Привет", cost: float | None = 0.001, prompt: int = 50, completion: int = 20
) -> httpx.Response:
    body: dict[str, object] = {
        "choices": [{"message": {"content": content}}],
    }
    if cost is not None:
        body["usage"] = {"cost": cost, "prompt_tokens": prompt, "completion_tokens": completion}
    return httpx.Response(200, json=body)


def _client(handler: Handler, cfg: Config, store: FakeStore, api_key: str = API_KEY) -> LLMClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LLMClient(api_key=api_key, cfg_getter=lambda: cfg, db=store, http=http)


MESSAGES = [{"role": "system", "content": "ты Фёдор"}, {"role": "user", "content": "привет"}]


async def test_successful_call_returns_result_and_updates_state() -> None:
    cfg = Config()
    store = FakeStore()
    handler, calls = _counting_handler(lambda _req: _ok_response(content="  Привет!  "))
    client = _client(handler, cfg, store)
    try:
        result = await client.call(MESSAGES, model="openrouter/foo", max_tokens=100, now=NOW)
    finally:
        await client.aclose()

    assert result == LLMResult(
        text="Привет!", cost_usd=0.001, prompt_tokens=50, completion_tokens=20
    )

    calls_key = day_key("llm_calls", NOW, cfg.persona.timezone)
    spent_key = day_key("llm_spent_usd", NOW, cfg.persona.timezone)
    assert store.state[calls_key] == "1"
    assert float(store.state[spent_key]) == pytest.approx(0.001)
    assert store.state["llm_error_streak"] == "0"

    assert len(calls) == 1
    request = calls[0]
    assert request.url == URL
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert request.headers["x-title"] == "trolobot"
    payload = json.loads(request.content)
    assert payload["model"] == "openrouter/foo"
    assert payload["messages"] == MESSAGES
    assert payload["max_tokens"] == 100
    assert payload["temperature"] == 0.8
    assert payload["usage"] == {"include": True}


async def test_timeout_sets_error_streak_to_one() -> None:
    cfg = Config()
    store = FakeStore()

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    client = _client(handler, cfg, store)
    try:
        with pytest.raises(LLMError) as excinfo:
            await client.call(MESSAGES, model="m", max_tokens=50, now=NOW)
    finally:
        await client.aclose()

    assert excinfo.value.reason == "llm:timeout"
    assert store.state["llm_error_streak"] == "1"
    calls_key = day_key("llm_calls", NOW, cfg.persona.timezone)
    assert store.state[calls_key] == "1"  # инкрементится даже если запрос упал


async def test_five_errors_open_circuit_and_sixth_call_skips_network() -> None:
    cfg = Config()  # circuit_errors=5, circuit_pause_min=30 по дефолту
    store = FakeStore()
    handler, calls = _counting_handler(lambda _req: httpx.Response(500, text="boom"))
    client = _client(handler, cfg, store)
    try:
        for _ in range(5):
            with pytest.raises(LLMError) as excinfo:
                await client.call(MESSAGES, model="m", max_tokens=50, now=NOW)
            assert excinfo.value.reason == "llm:http"

        assert store.state["llm_error_streak"] == "5"
        circuit_until = int(store.state["llm_circuit_until"])
        assert circuit_until == NOW + cfg.llm.circuit_pause_min * 60
        assert len(calls) == 5

        with pytest.raises(LLMError) as excinfo:
            await client.call(MESSAGES, model="m", max_tokens=50, now=NOW + 1)
        assert excinfo.value.reason == "llm:circuit_open"
        # Счётчик запросов транспорта не растёт: 6-й вызов не ушёл в сеть.
        assert len(calls) == 5
    finally:
        await client.aclose()


async def test_calls_cap_blocks_before_request() -> None:
    cfg = Config()
    cfg.llm.daily_calls_cap = 1
    store = FakeStore()
    calls_key = day_key("llm_calls", NOW, cfg.persona.timezone)
    store.state[calls_key] = "1"
    handler, calls = _counting_handler(lambda _req: _ok_response())
    client = _client(handler, cfg, store)
    try:
        with pytest.raises(LLMError) as excinfo:
            await client.call(MESSAGES, model="m", max_tokens=50, now=NOW)
    finally:
        await client.aclose()

    assert excinfo.value.reason == "llm:calls_cap"
    assert store.state[calls_key] == "1"  # не выросло
    assert len(calls) == 0  # запроса не было


async def test_budget_blocks_before_request() -> None:
    cfg = Config()
    cfg.llm.daily_budget_usd = 1.0
    store = FakeStore()
    spent_key = day_key("llm_spent_usd", NOW, cfg.persona.timezone)
    store.state[spent_key] = "1.0"
    handler, calls = _counting_handler(lambda _req: _ok_response())
    client = _client(handler, cfg, store)
    try:
        with pytest.raises(LLMError) as excinfo:
            await client.call(MESSAGES, model="m", max_tokens=50, now=NOW)
    finally:
        await client.aclose()

    assert excinfo.value.reason == "llm:budget"
    assert len(calls) == 0


async def test_http_500_maps_to_llm_http() -> None:
    cfg = Config()
    store = FakeStore()
    handler, _calls = _counting_handler(
        lambda _req: httpx.Response(500, text="internal server error")
    )
    client = _client(handler, cfg, store)
    try:
        with pytest.raises(LLMError) as excinfo:
            await client.call(MESSAGES, model="m", max_tokens=50, now=NOW)
    finally:
        await client.aclose()

    assert excinfo.value.reason == "llm:http"


async def test_invalid_json_body_maps_to_llm_http() -> None:
    cfg = Config()
    store = FakeStore()
    handler, _calls = _counting_handler(
        lambda _req: httpx.Response(200, content=b"not a json body")
    )
    client = _client(handler, cfg, store)
    try:
        with pytest.raises(LLMError) as excinfo:
            await client.call(MESSAGES, model="m", max_tokens=50, now=NOW)
    finally:
        await client.aclose()

    assert excinfo.value.reason == "llm:http"


async def test_missing_usage_cost_with_zero_tokens_falls_back_to_zero() -> None:
    cfg = Config()
    store = FakeStore()
    handler, _calls = _counting_handler(lambda _req: _ok_response(cost=None))
    client = _client(handler, cfg, store)
    try:
        result = await client.call(MESSAGES, model="m", max_tokens=50, now=NOW)
    finally:
        await client.aclose()

    assert result.cost_usd == 0.0
    spent_key = day_key("llm_spent_usd", NOW, cfg.persona.timezone)
    assert store.state[spent_key] == "0.0"


async def test_missing_usage_cost_uses_price_fallback() -> None:
    """Провайдер вернул usage (токены), но без cost — используется fallback-цена
    из cfg.llm.price_in_usd_per_1m/price_out_usd_per_1m."""
    cfg = Config()
    cfg.llm.price_in_usd_per_1m = 5.0
    cfg.llm.price_out_usd_per_1m = 25.0
    store = FakeStore()

    def handler(_request: httpx.Request) -> httpx.Response:
        body = {
            "choices": [{"message": {"content": "Привет"}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 200},  # без "cost"
        }
        return httpx.Response(200, json=body)

    client = _client(handler, cfg, store)
    try:
        result = await client.call(MESSAGES, model="m", max_tokens=50, now=NOW)
    finally:
        await client.aclose()

    # 1000 * 5.0 / 1e6 + 200 * 25.0 / 1e6 = 0.005 + 0.005 = 0.01
    assert result.cost_usd == pytest.approx(0.01)
    assert result.prompt_tokens == 1000
    assert result.completion_tokens == 200
    spent_key = day_key("llm_spent_usd", NOW, cfg.persona.timezone)
    assert float(store.state[spent_key]) == pytest.approx(0.01)


async def test_present_usage_cost_ignores_price_fallback() -> None:
    """Если провайдер вернул usage.cost — fallback-цены не используются вовсе."""
    cfg = Config()
    cfg.llm.price_in_usd_per_1m = 999.0
    cfg.llm.price_out_usd_per_1m = 999.0
    store = FakeStore()
    handler, _calls = _counting_handler(
        lambda _req: _ok_response(cost=0.001, prompt=1000, completion=200)
    )
    client = _client(handler, cfg, store)
    try:
        result = await client.call(MESSAGES, model="m", max_tokens=50, now=NOW)
    finally:
        await client.aclose()

    assert result.cost_usd == pytest.approx(0.001)


async def test_timeout_is_read_from_config_on_every_call() -> None:
    """cfg.llm.timeout_sec передаётся в каждый POST заново — /set меняет таймаут
    без рестарта и без пересоздания http-клиента."""
    cfg = Config()
    cfg.llm.timeout_sec = 5
    store = FakeStore()
    handler, _calls = _counting_handler(lambda _req: _ok_response())
    client = _client(handler, cfg, store)

    captured_timeouts: list[object] = []
    original_post = client._http.post

    async def recording_post(*args: object, **kwargs: object) -> httpx.Response:
        captured_timeouts.append(kwargs.get("timeout"))
        return await original_post(*args, **kwargs)  # type: ignore[arg-type]

    client._http.post = recording_post  # type: ignore[method-assign]
    try:
        await client.call(MESSAGES, model="m", max_tokens=50, now=NOW)
        cfg.llm.timeout_sec = 77
        await client.call(MESSAGES, model="m", max_tokens=50, now=NOW)
    finally:
        await client.aclose()

    assert captured_timeouts == [5, 77]


async def test_empty_content_raises_llm_empty_without_touching_streak() -> None:
    cfg = Config()
    store = FakeStore()
    handler, _calls = _counting_handler(lambda _req: _ok_response(content="   "))
    client = _client(handler, cfg, store)
    try:
        with pytest.raises(LLMError) as excinfo:
            await client.call(MESSAGES, model="m", max_tokens=50, now=NOW)
    finally:
        await client.aclose()

    assert excinfo.value.reason == "llm:empty"
    assert "llm_error_streak" not in store.state
    calls_key = day_key("llm_calls", NOW, cfg.persona.timezone)
    assert store.state[calls_key] == "1"  # попытка всё равно засчитана


async def test_api_key_never_appears_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    cfg = Config()
    store = FakeStore()
    handler, _calls = _counting_handler(
        lambda _req: httpx.Response(500, text="server exploded, ping the on-call")
    )
    client = _client(handler, cfg, store)
    caplog.set_level(logging.DEBUG)
    try:
        with pytest.raises(LLMError):
            await client.call(MESSAGES, model="m", max_tokens=50, now=NOW)
    finally:
        await client.aclose()

    for record in caplog.records:
        assert API_KEY not in record.getMessage()


async def test_empty_model_raises_value_error_without_side_effects() -> None:
    cfg = Config()
    store = FakeStore()
    handler, calls = _counting_handler(lambda _req: _ok_response())
    client = _client(handler, cfg, store)
    try:
        with pytest.raises(ValueError):
            await client.call(MESSAGES, model="", max_tokens=50, now=NOW)
    finally:
        await client.aclose()

    assert store.state == {}
    assert len(calls) == 0


async def test_creates_own_http_client_when_none_injected() -> None:
    cfg = Config()
    store = FakeStore()
    client = LLMClient(api_key=API_KEY, cfg_getter=lambda: cfg, db=store)
    try:
        pass  # конструктор не упал и не потребовал http — этого достаточно
    finally:
        await client.aclose()


async def test_call_delegates_to_call_raw_and_counts_budget() -> None:
    """``call`` — тонкая обёртка над ``call_raw`` (CLAUDE.md, "Интерфейсы: стикеры"):
    тот же запрос, тот же учёт llm_calls/llm_spent_usd."""
    cfg = Config()
    store = FakeStore()
    handler, calls = _counting_handler(lambda _req: _ok_response(content="Привет!"))
    client = _client(handler, cfg, store)
    try:
        result = await client.call(MESSAGES, model="openrouter/foo", max_tokens=100, now=NOW)
    finally:
        await client.aclose()

    assert result.text == "Привет!"
    calls_key = day_key("llm_calls", NOW, cfg.persona.timezone)
    spent_key = day_key("llm_spent_usd", NOW, cfg.persona.timezone)
    assert store.state[calls_key] == "1"
    assert float(store.state[spent_key]) == pytest.approx(0.001)
    assert len(calls) == 1


async def test_call_raw_accepts_vision_content_and_counts_budget() -> None:
    """``call_raw`` принимает content-массив (текст + картинка, stickers_fill.py) —
    тот же учёт бюджета, что у обычного текстового ``call``."""
    cfg = Config()
    store = FakeStore()
    handler, calls = _counting_handler(lambda _req: _ok_response(content='{"text": "надпись"}'))
    client = _client(handler, cfg, store)
    vision_messages: list[dict[str, object]] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "распознай"},
                {"type": "image_url", "image_url": {"url": "data:image/webp;base64,AAAA"}},
            ],
        }
    ]
    try:
        result = await client.call_raw(
            vision_messages, model="vision/model", max_tokens=120, now=NOW
        )
    finally:
        await client.aclose()

    assert result.text == '{"text": "надпись"}'
    calls_key = day_key("llm_calls", NOW, cfg.persona.timezone)
    assert store.state[calls_key] == "1"
    assert len(calls) == 1
    payload = json.loads(calls[0].content)
    assert payload["messages"] == vision_messages
    assert payload["model"] == "vision/model"


async def test_custom_counter_key_is_isolated_from_llm_calls() -> None:
    """counter_key (CLAUDE.md, "внимание как у живого человека") пишет свой
    суточный счётчик вместо общего llm_calls — followup-чекер не должен есть
    бюджет вызовов основной модели."""
    cfg = Config()
    store = FakeStore()
    handler, calls = _counting_handler(lambda _req: _ok_response())
    client = _client(handler, cfg, store)
    try:
        result = await client.call(
            MESSAGES, model="m", max_tokens=50, now=NOW, counter_key="followup_calls"
        )
    finally:
        await client.aclose()

    assert result.text == "Привет"
    followup_key = day_key("followup_calls", NOW, cfg.persona.timezone)
    llm_calls_key = day_key("llm_calls", NOW, cfg.persona.timezone)
    assert store.state[followup_key] == "1"
    assert llm_calls_key not in store.state
    assert len(calls) == 1


async def test_custom_calls_cap_blocks_before_request_independent_of_daily_calls_cap() -> None:
    cfg = Config()
    cfg.llm.daily_calls_cap = 1000  # общий потолок далеко не достигнут
    store = FakeStore()
    calls_key = day_key("followup_calls", NOW, cfg.persona.timezone)
    store.state[calls_key] = "3"
    handler, calls = _counting_handler(lambda _req: _ok_response())
    client = _client(handler, cfg, store)
    try:
        with pytest.raises(LLMError) as excinfo:
            await client.call(
                MESSAGES,
                model="m",
                max_tokens=50,
                now=NOW,
                counter_key="followup_calls",
                calls_cap=3,
            )
    finally:
        await client.aclose()

    assert excinfo.value.reason == "llm:calls_cap"
    assert store.state[calls_key] == "3"  # не выросло
    assert len(calls) == 0


async def test_default_counter_key_and_cap_unchanged() -> None:
    """Без явных counter_key/calls_cap поведение — как раньше: общий llm_calls и
    cfg.llm.daily_calls_cap."""
    cfg = Config()
    cfg.llm.daily_calls_cap = 1
    store = FakeStore()
    calls_key = day_key("llm_calls", NOW, cfg.persona.timezone)
    store.state[calls_key] = "1"
    handler, calls = _counting_handler(lambda _req: _ok_response())
    client = _client(handler, cfg, store)
    try:
        with pytest.raises(LLMError) as excinfo:
            await client.call(MESSAGES, model="m", max_tokens=50, now=NOW)
    finally:
        await client.aclose()

    assert excinfo.value.reason == "llm:calls_cap"
    assert len(calls) == 0


async def test_call_raw_respects_circuit_and_caps_same_as_call() -> None:
    cfg = Config()
    cfg.llm.daily_calls_cap = 0
    store = FakeStore()
    handler, calls = _counting_handler(lambda _req: _ok_response())
    client = _client(handler, cfg, store)
    try:
        with pytest.raises(LLMError) as excinfo:
            await client.call_raw(MESSAGES, model="m", max_tokens=50, now=NOW)
    finally:
        await client.aclose()

    assert excinfo.value.reason == "llm:calls_cap"
    assert len(calls) == 0
