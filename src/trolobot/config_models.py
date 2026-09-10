"""Pydantic-модели config.yaml (CHARACTER.md, раздел 6).

Диапазоны числовых полей (Field(ge=..., le=...)) разумны для персонажа-бота
в одном чате и позже используются для валидации команды /set.
"""

import re
from datetime import date, time

from pydantic import BaseModel, Field, field_validator, model_validator

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _validate_hhmm(value: str) -> str:
    if not _HHMM_RE.match(value):
        raise ValueError(f"invalid time {value!r}, expected HH:MM")
    return value


def parse_hhmm(value: str) -> time:
    """ "HH:MM" -> datetime.time."""
    hour_str, minute_str = value.split(":")
    return time(hour=int(hour_str), minute=int(minute_str))


class PersonaConfig(BaseModel):
    name: str = "Фёдор"
    display_name: str = "Отец Фёдор"
    name_triggers: list[str] = Field(
        default_factory=lambda: ["фёдор", "федор", "федя", "федь", "отец", "отче", "батюшка", "дед"]
    )
    birth_date: date = date(1974, 4, 12)
    arrived_poznan: int = Field(default=2006, ge=1950, le=2100)
    district: str = "Wilda"
    timezone: str = "Europe/Warsaw"

    def age(self, today: date) -> int:
        years = today.year - self.birth_date.year
        if (today.month, today.day) < (self.birth_date.month, self.birth_date.day):
            years -= 1
        return years


class LiveTalkConfig(BaseModel):
    """Что считается «живым разговором» для ambient-реплик."""

    min_messages: int = Field(default=3, ge=1, le=50)
    min_people: int = Field(default=2, ge=1, le=50)
    window_min: int = Field(default=10, ge=1, le=1440)


class SpontaneousConfig(BaseModel):
    """«Просто так» — бот пишет сам, без повода, когда в чате тихо."""

    per_week: int = Field(default=2, ge=0, le=50)
    window: tuple[str, str] = ("10:00", "22:00")
    min_quiet_hours: int = Field(default=3, ge=0, le=24)

    @field_validator("window")
    @classmethod
    def _validate_window(cls, value: tuple[str, str]) -> tuple[str, str]:
        return (_validate_hhmm(value[0]), _validate_hhmm(value[1]))

    def window_time(self) -> tuple[time, time]:
        return (parse_hhmm(self.window[0]), parse_hhmm(self.window[1]))


class ReplyDelayBucket(BaseModel):
    """Один бакет задержки ответа на обращение."""

    weight: float = Field(ge=0.0, le=1.0)
    range_sec: tuple[int, int]

    @field_validator("range_sec")
    @classmethod
    def _validate_range(cls, value: tuple[int, int]) -> tuple[int, int]:
        lo, hi = value
        if not (0 <= lo <= hi <= 86400):
            raise ValueError(f"invalid range_sec {value!r}: expected 0 <= min <= max <= 86400")
        return value


class BehaviourConfig(BaseModel):
    quiet_window: tuple[str, str] = ("02:00", "07:00")
    morning_reply_window: tuple[str, str] = ("07:00", "08:00")

    live_talk: LiveTalkConfig = Field(default_factory=LiveTalkConfig)
    ambient_probability: float = Field(default=0.15, ge=0.0, le=1.0)
    chat_cooldown_min: int = Field(default=25, ge=0, le=1440)
    daily_cap: int = Field(default=3, ge=0, le=50)

    spontaneous: SpontaneousConfig = Field(default_factory=SpontaneousConfig)

    mention_cooldown_sec: int = Field(default=180, ge=0, le=86400)
    mention_chat_cooldown_sec: int = Field(default=300, ge=0, le=86400)
    mention_daily_cap: int = Field(default=20, ge=0, le=200)
    reply_as_reply_after_sec: int = Field(default=300, ge=0, le=86400)

    debounce_sec: tuple[int, int] = (3, 7)
    reply_delay_buckets: list[ReplyDelayBucket] = Field(
        default_factory=lambda: [
            ReplyDelayBucket(weight=0.6, range_sec=(30, 180)),
            ReplyDelayBucket(weight=0.3, range_sec=(180, 900)),
            ReplyDelayBucket(weight=0.1, range_sec=(900, 3600)),
        ]
    )
    urgent_max_delay_sec: int = Field(default=180, ge=0, le=86400)
    late_reply_threshold_sec: int = Field(default=600, ge=0, le=86400)
    context_window: int = Field(default=30, ge=1, le=200)
    recent_replies_memory: int = Field(default=20, ge=1, le=200)
    topic_cooldown_min: int = Field(default=45, ge=0, le=1440)
    message_retention_days: int = Field(default=30, ge=1, le=3650)

    @field_validator("quiet_window", "morning_reply_window")
    @classmethod
    def _validate_windows(cls, value: tuple[str, str]) -> tuple[str, str]:
        return (_validate_hhmm(value[0]), _validate_hhmm(value[1]))

    @field_validator("debounce_sec")
    @classmethod
    def _validate_debounce(cls, value: tuple[int, int]) -> tuple[int, int]:
        lo, hi = value
        if not (0 <= lo <= hi <= 600):
            raise ValueError(f"invalid debounce_sec {value!r}: expected 0 <= min <= max <= 600")
        return value

    @model_validator(mode="after")
    def _check_bucket_weights(self) -> "BehaviourConfig":
        total = sum(bucket.weight for bucket in self.reply_delay_buckets)
        if abs(total - 1.0) > 0.01:
            raise ValueError(
                f"reply_delay_buckets weights must sum to 1.0 (tolerance 0.01), got {total:.4f}"
            )
        return self

    def quiet_window_time(self) -> tuple[time, time]:
        return (parse_hhmm(self.quiet_window[0]), parse_hhmm(self.quiet_window[1]))

    def morning_reply_window_time(self) -> tuple[time, time]:
        return (parse_hhmm(self.morning_reply_window[0]), parse_hhmm(self.morning_reply_window[1]))


class LlmConfig(BaseModel):
    provider: str = "openrouter"
    main_model: str = ""
    judge_model: str = ""
    timeout_sec: int = Field(default=30, ge=1, le=120)
    max_tokens: int = Field(default=200, ge=1, le=4000)
    daily_calls_cap: int = Field(default=60, ge=0, le=1000)
    daily_budget_usd: float = Field(default=2.0, ge=0.0, le=1000.0)
    circuit_errors: int = Field(default=5, ge=1, le=100)
    circuit_pause_min: int = Field(default=30, ge=0, le=1440)


class PlacesConfig(BaseModel):
    min_rating: float = Field(default=4.2, ge=0.0, le=5.0)
    min_reviews: int = Field(default=50, ge=0, le=100_000)
    require_operational: bool = True
    cache_ttl_days: int = Field(default=30, ge=1, le=3650)
    max_per_reply: int = Field(default=2, ge=0, le=20)


class FiltersConfig(BaseModel):
    shadow: bool = True
    places_whitelist: list[str] = Field(
        default_factory=lambda: ["Lidl", "OLX", "Biedronka", "Żabka", "Allegro"]
    )


class Config(BaseModel):
    persona: PersonaConfig = Field(default_factory=PersonaConfig)
    behaviour: BehaviourConfig = Field(default_factory=BehaviourConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    places: PlacesConfig = Field(default_factory=PlacesConfig)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
