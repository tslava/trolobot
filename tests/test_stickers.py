"""Тесты trolobot.stickers: каталог, меню, разбор ответа чузера, бюджет, сам чузер.

LLM — настоящий ``LLMClient`` с ``httpx.MockTransport`` (по образцу
``test_judge.py``): проверяем и реальную сборку запроса, и разбор ответа.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import yaml

from trolobot.config_models import Config, StickersConfig
from trolobot.llm import LLMClient
from trolobot.stickers import (
    Sticker,
    StickerCatalog,
    StickerChooser,
    load_catalog,
    parse_choice,
    recent_sticker_ids,
    render_sticker_menu,
    sticker_allowed,
)

STICKER_PROMPT_PATH = Path("prompts/sticker.txt")
NOW = 1_768_003_200


class FakeStore:
    """Минимальная in-memory подделка Database (см. tests/test_llm.py/test_judge.py)."""

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


def _choice_response(sticker: int | None) -> httpx.Response:
    content = json.dumps({"sticker": sticker})
    body = {
        "choices": [{"message": {"content": content}}],
        "usage": {"cost": 0.0001, "prompt_tokens": 10, "completion_tokens": 5},
    }
    return httpx.Response(200, json=body)


def _fail_handler(_request: httpx.Request) -> httpx.Response:
    raise AssertionError("LLM не должен был вызываться")


def _chooser(
    handler: Handler, cfg: Config, catalog: StickerCatalog
) -> tuple[StickerChooser, LLMClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm = LLMClient(api_key="sk-test", cfg_getter=lambda: cfg, db=FakeStore(), http=http)
    prompt = STICKER_PROMPT_PATH.read_text(encoding="utf-8")
    return StickerChooser(llm, lambda: cfg, catalog, prompt), llm


def _cfg(judge_model: str = "openrouter/small") -> Config:
    cfg = Config()
    cfg.llm.judge_model = judge_model
    return cfg


def _catalog(*, count: int = 3, disabled: set[int] | None = None) -> StickerCatalog:
    disabled = disabled or set()
    stickers = [
        Sticker(
            id=i,
            file_id=f"FILE{i}",
            emoji="😂",
            text=f"Текст {i}",
            when="когда уместно",
            enabled=i not in disabled,
        )
        for i in range(1, count + 1)
    ]
    return StickerCatalog(set_name="test_set", stickers=stickers)


# --- load_catalog -----------------------------------------------------------


def test_load_catalog_missing_file_returns_empty(tmp_path: Path) -> None:
    catalog = load_catalog(tmp_path / "no-such-stickers.yaml")
    assert catalog.set_name == ""
    assert catalog.stickers == []


def test_load_catalog_empty_file_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "stickers.yaml"
    path.write_text("", encoding="utf-8")
    catalog = load_catalog(path)
    assert catalog.stickers == []


def test_load_catalog_no_stickers_key_returns_empty_list(tmp_path: Path) -> None:
    path = tmp_path / "stickers.yaml"
    path.write_text("set_name: x\n", encoding="utf-8")
    catalog = load_catalog(path)
    assert catalog.set_name == "x"
    assert catalog.stickers == []


def test_load_catalog_valid_file(tmp_path: Path) -> None:
    path = tmp_path / "stickers.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "set_name": "my_set",
                "stickers": [
                    {
                        "id": 1,
                        "file_id": "ABC",
                        "emoji": "😂",
                        "text": "Ну ты даёшь",
                        "when": "удивление",
                        "enabled": True,
                    }
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    catalog = load_catalog(path)
    assert catalog.set_name == "my_set"
    assert len(catalog.stickers) == 1
    assert catalog.stickers[0].file_id == "ABC"


def test_load_catalog_broken_yaml_raises_value_error(tmp_path: Path) -> None:
    path = tmp_path / "stickers.yaml"
    path.write_text("set_name: [unterminated\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_catalog(path)


def test_load_catalog_not_a_mapping_raises_value_error(tmp_path: Path) -> None:
    path = tmp_path / "stickers.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_catalog(path)


def test_load_catalog_invalid_schema_raises_value_error(tmp_path: Path) -> None:
    path = tmp_path / "stickers.yaml"
    # sticker без обязательного text -> ValidationError, это ValueError.
    path.write_text(
        yaml.safe_dump({"set_name": "x", "stickers": [{"id": 1, "file_id": "ABC"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_catalog(path)


# --- render_sticker_menu -----------------------------------------------------


def test_render_sticker_menu_skips_disabled_and_formats_with_when() -> None:
    catalog = _catalog(count=2, disabled={2})
    menu = render_sticker_menu(catalog.stickers)
    assert menu == "1: «Текст 1» — когда уместно"


def test_render_sticker_menu_without_when_omits_dash() -> None:
    sticker = Sticker(id=5, file_id="F", text="Просто текст", when="", enabled=True)
    menu = render_sticker_menu([sticker])
    assert menu == "5: «Просто текст»"


def test_render_sticker_menu_empty_list_is_empty_string() -> None:
    assert render_sticker_menu([]) == ""


# --- parse_choice -------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "мусор не json",
        '```json\n{"sticker": true}\n```',
        json.dumps({"sticker": "1"}),
        json.dumps({"sticker": 99}),
        json.dumps({"nope": 1}),
        "",
        "{",
        json.dumps({"sticker": 1.5}),
    ],
)
def test_parse_choice_garbage_returns_none(raw: str) -> None:
    assert parse_choice(raw, {1, 2, 3}) is None


def test_parse_choice_null_returns_none() -> None:
    assert parse_choice(json.dumps({"sticker": None}), {1, 2, 3}) is None


def test_parse_choice_valid_id_returns_it() -> None:
    assert parse_choice(json.dumps({"sticker": 2}), {1, 2, 3}) == 2


def test_parse_choice_code_fence_wrapped() -> None:
    raw = "```json\n" + json.dumps({"sticker": 3}) + "\n```"
    assert parse_choice(raw, {1, 2, 3}) == 3


def test_parse_choice_tolerates_trailing_text() -> None:
    raw = json.dumps({"sticker": 1}) + "\n\nвот мой ответ"
    assert parse_choice(raw, {1, 2, 3}) == 1


# --- recent_sticker_ids -------------------------------------------------------


def test_recent_sticker_ids_extracts_tagged_texts_within_window() -> None:
    texts = [
        "[стикер #1] раз",
        "просто текст",
        "[стикер #2] два",
        "[стикер #3] три",
    ]
    assert recent_sticker_ids(texts, 2) == {2, 3}


def test_recent_sticker_ids_ignores_non_tagged() -> None:
    assert recent_sticker_ids(["привет", "как дела"], 5) == set()


def test_recent_sticker_ids_empty_texts() -> None:
    assert recent_sticker_ids([], 5) == set()


# --- sticker_allowed -----------------------------------------------------------


def _stickers_cfg(**overrides: object) -> StickersConfig:
    return StickersConfig.model_validate(overrides)


def test_sticker_allowed_disabled() -> None:
    cfg = _stickers_cfg(enabled=False)
    assert sticker_allowed(cfg=cfg, replies_since=100, count_today=0) is False


def test_sticker_allowed_min_replies_not_reached() -> None:
    cfg = _stickers_cfg(min_replies_between=4)
    assert sticker_allowed(cfg=cfg, replies_since=3, count_today=0) is False


def test_sticker_allowed_daily_cap_reached() -> None:
    cfg = _stickers_cfg(daily_cap=5)
    assert sticker_allowed(cfg=cfg, replies_since=100, count_today=5) is False


def test_sticker_allowed_true_when_all_checks_pass() -> None:
    cfg = _stickers_cfg(min_replies_between=4, daily_cap=5)
    assert sticker_allowed(cfg=cfg, replies_since=4, count_today=4) is True


# --- StickerChooser ------------------------------------------------------------


async def test_chooser_picks_sticker_from_valid_response() -> None:
    cfg = _cfg()
    catalog = _catalog(count=3)
    handler, calls = _counting_handler(lambda _req: _choice_response(2))
    chooser, llm = _chooser(handler, cfg, catalog)
    try:
        sticker = await chooser.choose(
            reply_text="Бывает.", trigger_text="ну ты и жук", exclude_ids=set(), now=NOW
        )
    finally:
        await llm.aclose()

    assert sticker is not None
    assert sticker.id == 2
    assert len(calls) == 1


async def test_chooser_returns_none_on_null() -> None:
    cfg = _cfg()
    catalog = _catalog(count=3)
    handler, _calls = _counting_handler(lambda _req: _choice_response(None))
    chooser, llm = _chooser(handler, cfg, catalog)
    try:
        sticker = await chooser.choose(
            reply_text="Бывает.", trigger_text="как сам", exclude_ids=set(), now=NOW
        )
    finally:
        await llm.aclose()

    assert sticker is None


async def test_chooser_returns_none_on_invalid_id() -> None:
    cfg = _cfg()
    catalog = _catalog(count=3)
    handler, _calls = _counting_handler(lambda _req: _choice_response(999))
    chooser, llm = _chooser(handler, cfg, catalog)
    try:
        sticker = await chooser.choose(
            reply_text="Бывает.", trigger_text="как сам", exclude_ids=set(), now=NOW
        )
    finally:
        await llm.aclose()

    assert sticker is None


async def test_chooser_returns_none_on_llm_error() -> None:
    cfg = _cfg()
    cfg.llm.daily_calls_cap = 0  # гарантированный LLMError без похода в сеть
    catalog = _catalog(count=3)
    chooser, llm = _chooser(_fail_handler, cfg, catalog)
    try:
        sticker = await chooser.choose(
            reply_text="Бывает.", trigger_text="как сам", exclude_ids=set(), now=NOW
        )
    finally:
        await llm.aclose()

    assert sticker is None


async def test_chooser_skips_call_when_no_candidates() -> None:
    cfg = _cfg()
    catalog = _catalog(count=2, disabled={1, 2})  # все выключены
    chooser, llm = _chooser(_fail_handler, cfg, catalog)
    try:
        sticker = await chooser.choose(
            reply_text="Бывает.", trigger_text="как сам", exclude_ids=set(), now=NOW
        )
    finally:
        await llm.aclose()

    assert sticker is None


async def test_chooser_skips_call_when_all_excluded() -> None:
    cfg = _cfg()
    catalog = _catalog(count=2)
    chooser, llm = _chooser(_fail_handler, cfg, catalog)
    try:
        sticker = await chooser.choose(
            reply_text="Бывает.", trigger_text="как сам", exclude_ids={1, 2}, now=NOW
        )
    finally:
        await llm.aclose()

    assert sticker is None


async def test_chooser_returns_none_when_no_model_configured() -> None:
    cfg = _cfg(judge_model="")
    catalog = _catalog(count=2)
    chooser, llm = _chooser(_fail_handler, cfg, catalog)
    try:
        sticker = await chooser.choose(
            reply_text="Бывает.", trigger_text="как сам", exclude_ids=set(), now=NOW
        )
    finally:
        await llm.aclose()

    assert sticker is None


async def test_chooser_uses_behaviour_model_override() -> None:
    cfg = _cfg(judge_model="openrouter/small")
    cfg.behaviour.stickers.model = "vision/own"
    catalog = _catalog(count=1)
    handler, calls = _counting_handler(lambda _req: _choice_response(1))
    chooser, llm = _chooser(handler, cfg, catalog)
    try:
        await chooser.choose(
            reply_text="Бывает.", trigger_text="как сам", exclude_ids=set(), now=NOW
        )
    finally:
        await llm.aclose()

    assert len(calls) == 1
    payload = json.loads(calls[0].content)
    assert payload["model"] == "vision/own"
