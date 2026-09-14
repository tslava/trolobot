"""Настройки процесса, читаются из .env / переменных окружения."""

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Секреты и пути. env_prefix нет — имена переменных совпадают с полями."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    bot_token: SecretStr
    admin_user_id: int = 0
    allowed_chat_id: int = 0
    openrouter_api_key: SecretStr | None = None
    google_places_key: SecretStr | None = None
    db_path: Path = Path("data/bot.db")
    config_path: Path = Path("config.yaml")
    few_shot_path: Path = Path("few_shot.yaml")
    prompt_path: Path = Path("prompts/system.txt")
    judge_prompt_path: Path = Path("prompts/judge.txt")
    sticker_prompt_path: Path = Path("prompts/sticker.txt")
    stickers_path: Path = Path("stickers.yaml")
    log_level: str = "INFO"
