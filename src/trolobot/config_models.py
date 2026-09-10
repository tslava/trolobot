"""Pydantic-модели config.yaml (CHARACTER.md, раздел 6).

Диапазоны числовых полей (Field(ge=..., le=...)) разумны для персонажа-бота
в одном чате и позже используются для валидации команды /set.
"""

import re
from datetime import date, time

from pydantic import BaseModel, Field, field_validator, model_validator

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
# OpenRouter model id: "provider/model", например "openai/gpt-4o-mini" или
# "anthropic/claude-3.5-sonnet:beta" — используется и llm.main_model, и llm.judge_model.
_MODEL_ID_RE = re.compile(r"^[\w.-]+/[\w.:-]+$")


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

    # Кулдаун = задержка, не отказ (решение владельца): обращение всегда получает
    # ответ, кулдаун только сдвигает due_at на этапе responder (earliest).
    mention_cooldown_sec: int = Field(default=60, ge=0, le=86400)
    mention_chat_cooldown_sec: int = Field(default=90, ge=0, le=86400)
    # Пока ребята наигрываются; потом снизить.
    mention_daily_cap: int = Field(default=50, ge=0, le=200)
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
    main_model: str = "anthropic/claude-sonnet-5"
    judge_model: str = "openai/gpt-5.4-nano"
    timeout_sec: int = Field(default=30, ge=1, le=120)
    max_tokens: int = Field(default=200, ge=1, le=4000)
    # 50 обращений + 3 ambient + утро + "просто так" ≈ 55 основных вызовов и
    # столько же судьи ≈ 110, потолок 150 с запасом.
    daily_calls_cap: int = Field(default=150, ge=0, le=1000)
    daily_budget_usd: float = Field(default=2.0, ge=0.0, le=1000.0)
    circuit_errors: int = Field(default=5, ge=1, le=100)
    circuit_pause_min: int = Field(default=30, ge=0, le=1440)
    # Fallback-цены за 1M токенов: используются, только если провайдер не вернул usage.cost.
    price_in_usd_per_1m: float = Field(default=2.0, ge=0.0)
    price_out_usd_per_1m: float = Field(default=10.0, ge=0.0)

    @field_validator("main_model", "judge_model")
    @classmethod
    def _validate_model_id(cls, value: str) -> str:
        if value == "":
            return value
        if not _MODEL_ID_RE.match(value):
            raise ValueError(
                f"invalid model id {value!r}: expected empty string or 'provider/model'"
            )
        return value


class PlacesConfig(BaseModel):
    min_rating: float = Field(default=4.2, ge=0.0, le=5.0)
    min_reviews: int = Field(default=50, ge=0, le=100_000)
    require_operational: bool = True
    cache_ttl_days: int = Field(default=30, ge=1, le=3650)
    max_per_reply: int = Field(default=2, ge=0, le=20)
    # Запросы для офлайн-наполнения кэша (places_fill.py, PLAN.md этап 5, п.1).
    # Дефолт — заготовленные запросы из CLAUDE.md, "Интерфейсы этапа 5".
    queries: list[str] = Field(
        default_factory=lambda: [
            "craft beer pub Poznań",
            "cichy pub Poznań",
            "piwo rzemieślnicze Poznań",
            "pub Wilda Poznań",
            "pub Jeżyce Poznań",
            "kawiarnia planszówki Poznań",
            "restauracja Kórnik",
            "Puszczykowo bar",
        ]
    )


_TOPIC_STOP_DEFAULT = [
    r"\bвойн[а-я]*",
    r"\bукраин[а-я]*",
    r"\bросси[а-я]*",
    r"\bрф\b",
    r"\bобстрел[а-я]*",
    r"\bфронт[а-я]*",
    r"\bмобилизац[а-я]*",
    r"\bпутин[а-я]*",
    r"\bзеленск[а-я]*",
    r"\bтрамп(а|у|ом|е|ы)?\b",
    r"\bвыбор(ы|ов|ах|ам)\b",
    r"\bсанкци[а-я]*",
    r"\bпис\b",
    r"\bpis\b",
    r"\bтуск[а-я]*",
    r"\bконфедерац[а-я]*",
    r"\bмигрант[а-я]*",
    r"\bбеженц[а-я]*",
    r"\bизраил[а-я]*",
    r"\bпалестин[а-я]*",
    r"\bсектор газа",
    r"\bцерк[а-я]*",
    r"\bкостёл[а-я]*",
    r"\bкостел[а-я]*",
    r"\bаборт[а-я]*",
    r"\bлгбт\b",
    r"\bнациз[а-я]*",
    r"\bфашис[а-я]*",
]

_INJECTION_MARKERS_DEFAULT = [
    r"забудь (инструкции|правила|всё)",
    r"\bигнорируй (все|всё|предыдущ\w*|инструкци\w*|правил\w*)\b",
    r"\bты теперь (бот|пират|ассистент|помощник|нейросеть|другой человек"
    r"|другой персонаж|играешь роль)\b",
    r"системн\w+ промпт",
    r"повтори за мной",
    r"скажи дословно",
    r"напиши слово",
    r"ignore (previous|all)",
    r"как языковая модель",
    r"\bпредставь,? что ты\b",
    r"\bпритворись\b",
    r"\bиграй роль\b",
]

_LOGISTICS_DEFAULT = [
    r"\b\d{1,2}[:.]\d{2}\b",
    r"\bв (семь|восемь|девять|десять|шесть|пять|четыре|три|два)\b(?! раз)",
    r"\bв час\b(?! пик)",
    r"\bво сколько\b",
    r"\bкто (идёт|идет|будет|со мной)\b",
    r"\bя пас\b",
    r"\bвстречаемся\b",
    r"\bгде (собираемся|встречаемся)\b",
    r"\bкто в (пн|вт|ср|чт|пт|сб|вс|понедельник|вторник|среду|четверг"
    r"|пятницу|субботу|воскресенье|выходные)\b",
    r"\bподтянусь\b",
    r"\bбуду через\b",
]

_URGENT_DEFAULT = [
    r"\bсегодня\b",
    r"\bсейчас\b",
    r"\bчерез час\b",
    r"\bкуда идём\b",
    r"\bкуда идем\b",
    r"\bты где\b",
]

_PLACES_REQUEST_DEFAULT = [
    r"\bкуда сходить\b",
    r"\bпосоветуй",
    r"\bгде посидеть\b",
    r"\bгде выпить\b",
    r"\bкакой бар\b",
    r"\bпаб\b",
    r"\bпивнух",
    r"\bкуда съездить\b",
]

_MODEL_TALK_DEFAULT = [
    r"языковая модель",
    r"нейросет\w*",
    r"инструкци\w*",
    r"промпт\w*",
    r"\bИИ\b",
    r"OpenAI",
    r"Anthropic",
    r"OpenRouter",
]

_ASSISTANT_MARKERS_DEFAULT = [
    "важно отметить",
    "стоит учесть",
    "рекомендую",
    "могу помочь",
    "дай знать",
    "надеюсь это поможет",
    "во-первых",
    "конечно!",
    "отличный вопрос",
    "если у тебя есть вопросы",
]

# Заведения из CHARACTER.md, раздел 7 — белый список для regex:venue/regex:latin.
# Многословные названия матчатся по каждому слову (filters.py разбивает фразу на слова),
# поэтому "LALKA" продублирована отдельно от "Klubokawiarnia LALKA": в чате её называют
# и полным, и сокращённым именем (см. few_shot.yaml).
_KNOWN_PLACES_DEFAULT = [
    "Piwna Stopa",
    "Dom Piwa",
    "Deja Vu",
    "Jeżycówka",
    "Klubokawiarnia LALKA",
    "LALKA",
    "FARBY",
    "Piwnica",
    "Wściekły Chmiel",
    "Lot Chmiela",
    "BRO",
    "Ministerstwo Browaru",
]

# Польский словарь Фёдора (CHARACTER.md, раздел 1: "двести слов, все нужные") — белый
# список для regex:venue/regex:latin, и одновременно то, что dedup:polish_freq
# сознательно СЧИТАЕТ латинскими токенами (это и есть польские слова, частоту которых
# правило ограничивает).
_POLISH_WORDS_DEFAULT = [
    "działka",
    "działki",
    "działce",
    "przegląd",
    "urząd",
    "urzędzie",
    "sklep",
    "piwo",
    "zrobiony",
    "mechanik",
    "sąsiad",
    "pomidory",
    "garaż",
    "grzyby",
    "las",
    "dobra",
    "nie",
    "tak",
    "spoko",
    "pan",
    "pani",
    "dziękuję",
]

_REGEX_LIST_FIELDS = (
    "topic_stop",
    "injection_markers",
    "logistics",
    "urgent",
    "places_request",
    "model_talk",
)


class FiltersConfig(BaseModel):
    shadow: bool = True
    places_whitelist: list[str] = Field(
        default_factory=lambda: ["Lidl", "OLX", "Biedronka", "Żabka", "Allegro"]
    )
    # Заведения из CHARACTER.md, раздел 7 — белый список regex:venue/regex:latin,
    # см. _KNOWN_PLACES_DEFAULT.
    known_places: list[str] = Field(default_factory=lambda: list(_KNOWN_PLACES_DEFAULT))
    # Польский словарь Фёдора — тот же белый список, а для dedup:polish_freq (наоборот)
    # это и есть слова, чью частоту правило ограничивает, см. _POLISH_WORDS_DEFAULT.
    polish_words: list[str] = Field(default_factory=lambda: list(_POLISH_WORDS_DEFAULT))
    topic_stop: list[str] = Field(default_factory=lambda: list(_TOPIC_STOP_DEFAULT))
    injection_markers: list[str] = Field(default_factory=lambda: list(_INJECTION_MARKERS_DEFAULT))
    logistics: list[str] = Field(default_factory=lambda: list(_LOGISTICS_DEFAULT))
    urgent: list[str] = Field(default_factory=lambda: list(_URGENT_DEFAULT))
    places_request: list[str] = Field(default_factory=lambda: list(_PLACES_REQUEST_DEFAULT))
    model_talk: list[str] = Field(default_factory=lambda: list(_MODEL_TALK_DEFAULT))
    assistant_markers: list[str] = Field(default_factory=lambda: list(_ASSISTANT_MARKERS_DEFAULT))

    @field_validator(*_REGEX_LIST_FIELDS)
    @classmethod
    def _validate_regex_list(cls, value: list[str]) -> list[str]:
        for pattern in value:
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"invalid regex pattern {pattern!r}: {exc}") from exc
        return value


class Config(BaseModel):
    persona: PersonaConfig = Field(default_factory=PersonaConfig)
    behaviour: BehaviourConfig = Field(default_factory=BehaviourConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    places: PlacesConfig = Field(default_factory=PlacesConfig)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
