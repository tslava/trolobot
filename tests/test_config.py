"""load_config: реальный config.yaml, overrides, ошибки, возраст."""

import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from trolobot.config import flatten_config, load_config
from trolobot.config_models import (
    BehaviourConfig,
    Config,
    FiltersConfig,
    LlmConfig,
    PlacesConfig,
    ReplyDelayBucket,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "config.yaml"


def test_load_real_config_without_errors() -> None:
    cfg = load_config(CONFIG_PATH)

    assert isinstance(cfg, Config)
    assert cfg.persona.name == "Фёдор"
    assert cfg.persona.display_name == "Отец Фёдор"
    assert cfg.behaviour.daily_cap == 3
    assert cfg.filters.shadow is True
    assert "Lidl" in cfg.filters.places_whitelist


def test_load_real_config_filters_pattern_lists() -> None:
    cfg = load_config(CONFIG_PATH)
    default_filters = FiltersConfig()

    assert cfg.filters.topic_stop == default_filters.topic_stop
    assert cfg.filters.injection_markers == default_filters.injection_markers
    assert cfg.filters.logistics == default_filters.logistics
    assert cfg.filters.urgent == default_filters.urgent
    assert cfg.filters.places_request == default_filters.places_request
    assert cfg.filters.model_talk == default_filters.model_talk
    assert cfg.filters.assistant_markers == default_filters.assistant_markers
    assert cfg.filters.known_places == default_filters.known_places
    assert cfg.filters.polish_words == default_filters.polish_words

    assert r"\bсектор газа" in cfg.filters.topic_stop
    assert r"\bпис\b" in cfg.filters.topic_stop
    assert r"\b\d{1,2}[:.]\d{2}\b" in cfg.filters.logistics
    assert "конечно!" in cfg.filters.assistant_markers
    assert "LALKA" in cfg.filters.known_places
    assert "Klubokawiarnia LALKA" in cfg.filters.known_places
    assert "działka" in cfg.filters.polish_words
    assert "urząd" in cfg.filters.polish_words


def test_load_real_config_places_queries_match_defaults() -> None:
    # config.yaml дублирует дефолтные queries places_fill.py (CLAUDE.md, "Интерфейсы
    # этапа 5") — эта проверка ловит расхождение, если один из файлов поправили без другого.
    cfg = load_config(CONFIG_PATH)
    default_places = PlacesConfig()

    assert cfg.places.queries == default_places.queries
    assert "restauracja Kórnik" in cfg.places.queries
    assert "Puszczykowo bar" in cfg.places.queries


def test_config_builds_with_defaults_without_yaml() -> None:
    cfg = Config()

    assert cfg.persona.birth_date == date(1974, 4, 12)
    assert cfg.behaviour.ambient_probability == 0.15


def test_override_changes_value_and_coerces_int() -> None:
    cfg = load_config(CONFIG_PATH, {"behaviour.daily_cap": "7"})

    assert cfg.behaviour.daily_cap == 7
    assert isinstance(cfg.behaviour.daily_cap, int)


def test_override_coerces_float() -> None:
    cfg = load_config(CONFIG_PATH, {"behaviour.ambient_probability": "0.42"})

    assert cfg.behaviour.ambient_probability == pytest.approx(0.42)


def test_override_bool_and_list() -> None:
    cfg = load_config(
        CONFIG_PATH,
        {
            "filters.shadow": "false",
            "persona.name_triggers": "[федя, дед]",
        },
    )

    assert cfg.filters.shadow is False
    assert cfg.persona.name_triggers == ["федя", "дед"]


def test_nested_override() -> None:
    cfg = load_config(CONFIG_PATH, {"behaviour.live_talk.min_people": "5"})

    assert cfg.behaviour.live_talk.min_people == 5


def test_invalid_override_value_raises_value_error_with_key() -> None:
    with pytest.raises(ValueError) as exc_info:
        load_config(CONFIG_PATH, {"behaviour.daily_cap": "not-a-number"})

    assert "behaviour.daily_cap" in str(exc_info.value)


def test_invalid_override_out_of_range_raises() -> None:
    with pytest.raises(ValueError) as exc_info:
        load_config(CONFIG_PATH, {"behaviour.ambient_probability": "2.5"})

    assert "behaviour.ambient_probability" in str(exc_info.value)


def test_unknown_override_key_raises_value_error_with_key() -> None:
    with pytest.raises(ValueError) as exc_info:
        load_config(CONFIG_PATH, {"behaviour.no_such_key": "1"})

    assert "behaviour.no_such_key" in str(exc_info.value)


def test_unknown_override_section_raises() -> None:
    with pytest.raises(ValueError) as exc_info:
        load_config(CONFIG_PATH, {"nope.daily_cap": "1"})

    assert "nope.daily_cap" in str(exc_info.value)


def test_reply_delay_bucket_weights_must_sum_to_one() -> None:
    with pytest.raises(ValidationError):
        BehaviourConfig(
            reply_delay_buckets=[
                ReplyDelayBucket(weight=0.5, range_sec=(30, 180)),
                ReplyDelayBucket(weight=0.2, range_sec=(180, 900)),
            ]
        )


def test_reply_delay_bucket_weights_within_tolerance_ok() -> None:
    cfg = BehaviourConfig(
        reply_delay_buckets=[
            ReplyDelayBucket(weight=0.61, range_sec=(30, 180)),
            ReplyDelayBucket(weight=0.3, range_sec=(180, 900)),
            ReplyDelayBucket(weight=0.09, range_sec=(900, 3600)),
        ]
    )
    assert len(cfg.reply_delay_buckets) == 3


def test_persona_age() -> None:
    cfg = load_config(CONFIG_PATH)

    assert cfg.persona.age(date(2026, 9, 10)) == 52
    assert cfg.persona.age(date(2026, 4, 11)) == 51
    assert cfg.persona.age(date(2026, 4, 12)) == 52


def test_flatten_config_roundtrip_keys() -> None:
    cfg = load_config(CONFIG_PATH)
    flat = flatten_config(cfg)

    assert flat["behaviour.daily_cap"] == "3"
    assert flat["persona.name"] == "Фёдор"
    assert flat["filters.shadow"] == "true"


# --- FiltersConfig: валидация регулярок -------------------------------------------

_INVALID_REGEX = "\\bвойн[а-я*"  # незакрытая скобка/класс символов


@pytest.mark.parametrize(
    "field_name",
    ["topic_stop", "injection_markers", "logistics", "urgent", "places_request", "model_talk"],
)
def test_invalid_regex_in_filters_field_raises_with_pattern_text(field_name: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        FiltersConfig(**{field_name: [_INVALID_REGEX]})

    assert _INVALID_REGEX in str(exc_info.value)


def test_invalid_regex_in_real_config_via_override_raises() -> None:
    override_value = f"[{json.dumps(_INVALID_REGEX, ensure_ascii=False)}]"
    with pytest.raises(ValueError) as exc_info:
        load_config(CONFIG_PATH, {"filters.topic_stop": override_value})

    assert _INVALID_REGEX in str(exc_info.value)


# --- LlmConfig: main_model/judge_model — "" или "provider/model" -----------------


def test_llm_model_id_accepts_empty_string() -> None:
    cfg = LlmConfig(main_model="", judge_model="")
    assert cfg.main_model == ""
    assert cfg.judge_model == ""


def test_llm_model_id_accepts_provider_slash_model() -> None:
    cfg = LlmConfig(main_model="openai/gpt-4o-mini", judge_model="anthropic/claude-3.5-haiku")
    assert cfg.main_model == "openai/gpt-4o-mini"


@pytest.mark.parametrize("field_name", ["main_model", "judge_model"])
def test_llm_model_id_rejects_value_without_slash(field_name: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        LlmConfig(**{field_name: "мусор"})

    assert "мусор" in str(exc_info.value)


def test_set_llm_main_model_garbage_via_override_raises() -> None:
    with pytest.raises(ValueError) as exc_info:
        load_config(CONFIG_PATH, {"llm.main_model": "мусор"})

    assert "llm.main_model" in str(exc_info.value)


def test_assistant_markers_are_not_validated_as_regex() -> None:
    # assistant_markers — фразы, а не регулярки: строка с "особыми" символами regex
    # (например незакрытая скобка) не должна валиться на валидаторе.
    cfg = FiltersConfig(assistant_markers=["(это не закрытая скобка"])
    assert cfg.assistant_markers == ["(это не закрытая скобка"]
