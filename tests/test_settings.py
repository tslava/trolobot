"""Settings читается из окружения, .env не обязателен."""

from pathlib import Path

import pytest

from trolobot.settings import Settings


@pytest.fixture(autouse=True)
def _isolated_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Гарантируем, что рядом нет .env — настройки должны собираться из чистого окружения.
    monkeypatch.chdir(tmp_path)


def test_settings_from_env_minimal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "test-token-123")

    settings = Settings()

    assert settings.bot_token.get_secret_value() == "test-token-123"
    assert settings.admin_user_id == 0
    assert settings.allowed_chat_id == 0
    assert settings.openrouter_api_key is None
    assert settings.google_places_key is None
    assert settings.db_path == Path("data/bot.db")
    assert settings.config_path == Path("config.yaml")
    assert settings.few_shot_path == Path("few_shot.yaml")
    assert settings.prompt_path == Path("prompts/system.txt")
    assert settings.vision_prompt_path == Path("prompts/vision.txt")
    assert settings.changelog_path == Path("CHANGELOG.md")
    assert settings.log_level == "INFO"


def test_settings_from_env_full(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "abc")
    monkeypatch.setenv("ADMIN_USER_ID", "42")
    monkeypatch.setenv("ALLOWED_CHAT_ID", "-1001234567890")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-1")
    monkeypatch.setenv("GOOGLE_PLACES_KEY", "gp-1")
    monkeypatch.setenv("DB_PATH", "/tmp/custom.db")
    monkeypatch.setenv("CHANGELOG_PATH", "/tmp/CHANGELOG.md")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = Settings()

    assert settings.admin_user_id == 42
    assert settings.allowed_chat_id == -1001234567890
    assert settings.openrouter_api_key is not None
    assert settings.openrouter_api_key.get_secret_value() == "sk-or-1"
    assert settings.google_places_key is not None
    assert settings.google_places_key.get_secret_value() == "gp-1"
    assert settings.changelog_path == Path("/tmp/CHANGELOG.md")
    assert settings.db_path == Path("/tmp/custom.db")
    assert settings.log_level == "DEBUG"


def test_settings_empty_env_values_like_env_example(monkeypatch: pytest.MonkeyPatch) -> None:
    # .env.example содержит все ключи с пустыми значениями (KEY=). Пустая строка не должна
    # ломать int-поля или превращаться в SecretStr("") — она должна трактоваться как "не задано".
    monkeypatch.setenv("BOT_TOKEN", "abc")
    monkeypatch.setenv("ADMIN_USER_ID", "")
    monkeypatch.setenv("ALLOWED_CHAT_ID", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("GOOGLE_PLACES_KEY", "")
    monkeypatch.setenv("DB_PATH", "")
    monkeypatch.setenv("LOG_LEVEL", "")

    settings = Settings()

    assert settings.admin_user_id == 0
    assert settings.allowed_chat_id == 0
    assert settings.openrouter_api_key is None
    assert settings.google_places_key is None
    assert settings.db_path == Path("data/bot.db")
    assert settings.log_level == "INFO"


def test_settings_weather_home_point_defaults_to_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Координат домашней точки в репозитории нет: по умолчанию их просто нет."""
    monkeypatch.setenv("BOT_TOKEN", "abc")

    settings = Settings()

    assert settings.weather_latitude is None
    assert settings.weather_longitude is None
    assert settings.weather_home_name == ""
    assert settings.weather_place_prompt_path == Path("prompts/weather_place.txt")


def test_settings_weather_home_point_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "abc")
    monkeypatch.setenv("WEATHER_LATITUDE", "50.0")
    monkeypatch.setenv("WEATHER_LONGITUDE", "10.0")
    monkeypatch.setenv("WEATHER_HOME_NAME", "Город")

    settings = Settings()

    assert settings.weather_latitude == 50.0
    assert settings.weather_longitude == 10.0
    assert settings.weather_home_name == "Город"


def test_settings_empty_weather_env_values_mean_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """.env.example держит ключи пустыми — пустая строка не должна ломать float-поля."""
    monkeypatch.setenv("BOT_TOKEN", "abc")
    monkeypatch.setenv("WEATHER_LATITUDE", "")
    monkeypatch.setenv("WEATHER_LONGITUDE", "")
    monkeypatch.setenv("WEATHER_HOME_NAME", "")

    settings = Settings()

    assert settings.weather_latitude is None
    assert settings.weather_longitude is None
    assert settings.weather_home_name == ""


def test_settings_missing_bot_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOT_TOKEN", raising=False)

    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError, обязательное поле
        Settings()
