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
    name: str = Field(default="Фёдор", description="Имя персонажа")
    display_name: str = Field(default="Отец Фёдор", description="Имя профиля бота в Telegram")
    name_triggers: list[str] = Field(
        default_factory=lambda: [
            "фёдор",
            "федор",
            "федя",
            "федь",
            "отец",
            "отче",
            "батюшка",
            "дед",
        ],
        description="Слова-обращения к персонажу без @, по границам слов",
    )
    birth_date: date = Field(
        default=date(1974, 4, 12), description="Дата рождения; возраст считается на лету"
    )
    arrived_poznan: int = Field(
        default=2006, ge=1950, le=2100, description="Год переезда в Познань"
    )
    district: str = Field(default="Wilda", description="Родной район, который персонаж защищает")
    timezone: str = Field(
        default="Europe/Warsaw", description="Таймзона персонажа для суток и окон времени"
    )

    def age(self, today: date) -> int:
        years = today.year - self.birth_date.year
        if (today.month, today.day) < (self.birth_date.month, self.birth_date.day):
            years -= 1
        return years


class LiveTalkConfig(BaseModel):
    """Что считается «живым разговором» для ambient-реплик."""

    min_messages: int = Field(
        default=3, ge=1, le=50, description="Минимум сообщений в окне для живого разговора"
    )
    min_people: int = Field(
        default=2, ge=1, le=50, description="Минимум разных людей в окне для живого разговора"
    )
    window_min: int = Field(
        default=10, ge=1, le=1440, description="Окно в минутах, за которое считается разговор"
    )


class SpontaneousConfig(BaseModel):
    """«Просто так» — бот пишет сам, без повода, когда в чате тихо."""

    per_week: int = Field(
        default=2, ge=0, le=50, description="Сколько раз в неделю можно написать «просто так»"
    )
    window: tuple[str, str] = Field(
        default=("10:00", "22:00"), description="Окно времени HH:MM-HH:MM для «просто так»"
    )
    min_quiet_hours: int = Field(
        default=3, ge=0, le=24, description="Часов тишины подряд перед репликой «просто так»"
    )

    @field_validator("window")
    @classmethod
    def _validate_window(cls, value: tuple[str, str]) -> tuple[str, str]:
        return (_validate_hhmm(value[0]), _validate_hhmm(value[1]))

    def window_time(self) -> tuple[time, time]:
        return (parse_hhmm(self.window[0]), parse_hhmm(self.window[1]))


class ReplyDelayBucket(BaseModel):
    """Один бакет задержки ответа на обращение."""

    weight: float = Field(ge=0.0, le=1.0, description="Вес бакета при выборе задержки ответа")
    range_sec: tuple[int, int] = Field(description="Диапазон задержки в секундах, [мин, макс]")

    @field_validator("range_sec")
    @classmethod
    def _validate_range(cls, value: tuple[int, int]) -> tuple[int, int]:
        lo, hi = value
        if not (0 <= lo <= hi <= 86400):
            raise ValueError(f"invalid range_sec {value!r}: expected 0 <= min <= max <= 86400")
        return value


class ReactionsConfig(BaseModel):
    """Реакции-эмодзи на чужие сообщения, срезанные гейтом по gate:dice/gate:ambient_cooldown.

    Дёшево: не вызывает модель, не создаёт сообщение, только setMessageReaction —
    эффект присутствия без текстового ответа (CLAUDE.md, "Интерфейсы: реакции").
    ``emoji`` валидируется на уровне ``Config`` (см. ``Config._check_reactions_emoji_allowed``):
    каждый элемент обязан входить в ``filters.allowed_emoji``, иначе ``/set
    behaviour.reactions.emoji`` падает с понятной ошибкой.
    """

    enabled: bool = Field(default=True, description="Включает реакции-эмодзи вместо молчания")
    probability: float = Field(
        default=0.2, ge=0.0, le=1.0, description="Шанс поставить реакцию, когда она возможна"
    )
    cooldown_min: int = Field(
        default=60, ge=0, le=1440, description="Не чаще одной реакции в N минут"
    )
    daily_cap: int = Field(
        default=8, ge=0, le=100, description="Потолок реакций в сутки, отдельно от ambient/mention"
    )
    emoji: list[str] = Field(
        default_factory=lambda: ["👍", "💩"],
        description="Из чего выбирается реакция; каждый элемент — из filters.allowed_emoji",
    )

    @field_validator("emoji")
    @classmethod
    def _validate_non_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("reactions.emoji must not be empty")
        return value


class StickersConfig(BaseModel):
    """Стикеры вместо текста — второй вызов дешёвой моделью (CLAUDE.md, "Интерфейсы:
    стикеры"). Основная модель ничего не знает о каталоге; уже прошедший выходной
    фильтр текст вместе с триггером и каталогом уходит второй, дешёвой модели
    (``model`` или, если пусто, ``llm.judge_model``), которая либо называет номер
    стикера, либо ``null`` — и тогда уходит текст как раньше.
    """

    enabled: bool = Field(default=True, description="Включает отправку стикеров вместо текста")
    min_replies_between: int = Field(
        default=4,
        ge=0,
        le=50,
        description="Текстовых реплик должно пройти после стикера до следующего",
    )
    daily_cap: int = Field(default=1, ge=0, le=50, description="Потолок стикеров в сутки")
    recent_window: int = Field(
        default=10,
        ge=0,
        le=50,
        description="Столько последних использованных стикеров не повторять",
    )
    model: str = Field(
        default="", description="Модель для выбора стикера; пусто -> llm.judge_model"
    )
    max_tokens: int = Field(
        default=60, ge=10, le=300, description="Лимит токенов ответа модели-чузера стикеров"
    )

    @field_validator("model")
    @classmethod
    def _validate_model_id(cls, value: str) -> str:
        if value == "":
            return value
        if not _MODEL_ID_RE.match(value):
            raise ValueError(
                f"invalid model id {value!r}: expected empty string or 'provider/model'"
            )
        return value


class HotWindowConfig(BaseModel):
    """Горячее окно после `/life` и `/say` — полчаса живее обычного (CLAUDE.md,
    "горячее окно после /life и /say"). Вне окна поведение не меняется ни на шаг.
    """

    enabled: bool = Field(default=True, description="Включает горячее окно после /life и /say")
    minutes: int = Field(
        default=30, ge=0, le=720, description="Длительность горячего окна после /life и /say"
    )
    mention_max_delay_sec: int = Field(
        default=120,
        ge=0,
        le=3600,
        description="Потолок задержки ответа на обращение в горячем окне",
    )
    ambient_probability: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Шанс ambient-реплики в горячем окне вместо ambient_probability",
    )
    ambient_cap: int = Field(
        default=8, ge=0, le=50, description="Потолок ambient-реплик за одно горячее окно"
    )
    open_on_any_reply: bool = Field(
        default=False,
        description="Открывать горячее окно после любой реплики, не только /life и /say",
    )


class FollowupConfig(BaseModel):
    """Дешёвая семантическая проверка «это мне или про мою тему?» в горячем окне
    (CLAUDE.md, "внимание как у живого человека"). Сообщения, которые обычный гейт
    не признал прямым обращением, во время горячего окна дополнительно проверяются
    маленькой моделью — «да» превращает их в обращение ``Trigger.FOLLOWUP``.
    """

    enabled: bool = Field(
        default=True, description="Включает дешёвую проверку «это мне?» в горячем окне"
    )
    model: str = Field(
        default="", description="Модель для проверки «это мне?»; пусто -> llm.judge_model"
    )
    max_tokens: int = Field(
        default=60, ge=10, le=300, description="Лимит токенов ответа дешёвой проверки"
    )
    daily_cap: int = Field(
        default=300,
        ge=0,
        le=5000,
        description="Потолок вызовов дешёвой проверки в сутки, свой счётчик",
    )
    context_messages: int = Field(
        default=10, ge=1, le=50, description="Сколько сообщений чата дать дешёвой проверке"
    )
    recent_replies: int = Field(
        default=3, ge=1, le=10, description="Сколько последних реплик Фёдора дать проверке"
    )

    @field_validator("model")
    @classmethod
    def _validate_model_id(cls, value: str) -> str:
        if value == "":
            return value
        if not _MODEL_ID_RE.match(value):
            raise ValueError(
                f"invalid model id {value!r}: expected empty string or 'provider/model'"
            )
        return value


class CheckinConfig(BaseModel):
    """«Вернулся проверить» — раз в ``after_min`` минут после закрытия горячего
    окна один вызов основной модели по всему, что написали после последней
    реплики персонажа (CLAUDE.md, "внимание как у живого человека: вернулся
    проверить"). Не путать с ``followup`` (дешёвая проверка ВНУТРИ окна) —
    здесь окно уже закрыто, проверка идёт основной моделью и реже.
    """

    enabled: bool = Field(default=True, description="Включает периодическую проверку «вернулся»")
    after_min: tuple[int, int] = Field(
        default=(120, 240),
        description="Минут после закрытия горячего окна до проверки, [мин, макс]",
    )
    topic_max_hours: int = Field(
        default=48, ge=1, le=720, description="Часов без реплик бота — тема считается умершей"
    )
    max_messages: int = Field(
        default=40, ge=1, le=200, description="Сколько сообщений после последней реплики брать"
    )
    poll_sec: int = Field(
        default=300, ge=30, le=3600, description="Период фонового цикла checkin_job, сек"
    )
    quiet_min: int = Field(
        default=10,
        ge=0,
        le=180,
        description="Минут тишины в чате перед проверкой «вернулся»; писали позже — отложить",
    )

    @field_validator("after_min")
    @classmethod
    def _validate_after_min(cls, value: tuple[int, int]) -> tuple[int, int]:
        lo, hi = value
        if not (0 <= lo <= hi):
            raise ValueError(f"invalid after_min {value!r}: expected 0 <= min <= max")
        return value


class PresenceConfig(BaseModel):
    """Потолок присутствия (CLAUDE.md, "меньше и разнообразнее"): за локальные сутки
    бот не говорит больше, чем ``max_share`` от числа сообщений людей плюс
    ``free_replies`` в запас. Под потолком проходят только реплаи на самого бота,
    ``/life`` и ``/say`` — остальное (обращения по имени, ambient, «просто так»,
    утренняя реплика, «вернулся проверить») молчит до конца суток.
    """

    enabled: bool = Field(default=True, description="Включает суточный потолок присутствия бота")
    max_share: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        description="Доля реплик бота от сообщений людей за локальные сутки",
    )
    free_replies: int = Field(
        default=2,
        ge=0,
        le=20,
        description="Реплик в сутки сверх доли: на утро пустого чата",
    )

    def allowance(self, human_messages_today: int) -> int:
        """Сколько реплик бот может себе позволить сегодня при таком числе сообщений людей."""
        return int(human_messages_today * self.max_share) + self.free_replies

    def over_cap(self, *, human_messages_today: int, bot_replies_today: int) -> bool:
        """Потолок уже выбран (выключенный потолок — никогда)."""
        if not self.enabled:
            return False
        return bot_replies_today >= self.allowance(human_messages_today)


class ChatMemoryConfig(BaseModel):
    """Долгая память чата (CLAUDE.md, "долгая память чата"): раз в неделю модель
    сжимает прошедшие разговоры в несколько строк, пересказ живёт в БД дольше
    самих сообщений (``message_retention_days``) и подмешивается в системный промпт.
    """

    enabled: bool = Field(default=True, description="Включает пересказы прошедших разговоров")
    period_days: int = Field(
        default=7, ge=1, le=31, description="Длина одного периода пересказа в сутках"
    )
    run_window: tuple[str, str] = Field(
        default=("04:00", "06:00"), description="Окно локального времени HH:MM для прогона памяти"
    )
    in_prompt: int = Field(
        default=8, ge=0, le=52, description="Сколько последних пересказов класть в промпт"
    )
    keep_days: int = Field(
        default=365, ge=7, le=3650, description="Сколько суток хранить пересказы в БД"
    )
    max_messages: int = Field(
        default=600,
        ge=50,
        le=5000,
        description="Потолок сообщений на один пересказ, берутся последние",
    )
    max_chars: int = Field(
        default=700, ge=100, le=3000, description="Потолок длины одного пересказа в символах"
    )
    max_tokens: int = Field(
        default=400, ge=50, le=2000, description="Лимит токенов ответа модели на пересказ"
    )
    model: str = Field(default="", description="Модель для пересказа; пусто -> llm.main_model")
    backfill_periods: int = Field(
        default=4, ge=0, le=12, description="Сколько прошлых периодов догнать при первом прогоне"
    )

    @field_validator("run_window")
    @classmethod
    def _validate_run_window(cls, value: tuple[str, str]) -> tuple[str, str]:
        return (_validate_hhmm(value[0]), _validate_hhmm(value[1]))

    def run_window_time(self) -> tuple[time, time]:
        return (parse_hhmm(self.run_window[0]), parse_hhmm(self.run_window[1]))

    @field_validator("model")
    @classmethod
    def _validate_model_id(cls, value: str) -> str:
        if value == "":
            return value
        if not _MODEL_ID_RE.match(value):
            raise ValueError(
                f"invalid model id {value!r}: expected empty string or 'provider/model'"
            )
        return value


class VisionConfig(BaseModel):
    """Зрение на фото (CLAUDE.md, "Интерфейсы: зрение на фото"). Снимок из чата
    описывается моделью со зрением одной-двумя фразами, и описание становится
    текстом сообщения («[фото: ...]») — дальше гейт и генерация работают с ним как
    с обычным текстом. Стоит денег, поэтому описывается не каждое фото: повод
    (обращение или горячее окно), иногда кубик, и всегда под суточным потолком
    со своим счётчиком ``vision_calls``.
    """

    enabled: bool = Field(default=True, description="Включает описание фото моделью со зрением")
    model: str = Field(
        default="", description="Модель со зрением для описания фото; пусто -> llm.main_model"
    )
    max_tokens: int = Field(
        default=120, ge=20, le=500, description="Лимит токенов ответа модели со зрением"
    )
    daily_cap: int = Field(
        default=20, ge=0, le=500, description="Потолок описаний фото в сутки, свой счётчик"
    )
    ambient_probability: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="Шанс описать фото, присланное без повода (не обращение, не окно)",
    )
    max_width: int = Field(
        default=1024,
        ge=256,
        le=4096,
        description="Берётся самый большой размер фото не шире этого, пикселей",
    )
    max_chars: int = Field(
        default=200, ge=40, le=500, description="Потолок длины описания фото, символов"
    )

    @field_validator("model")
    @classmethod
    def _validate_model_id(cls, value: str) -> str:
        if value == "":
            return value
        if not _MODEL_ID_RE.match(value):
            raise ValueError(
                f"invalid model id {value!r}: expected empty string or 'provider/model'"
            )
        return value


class BehaviourConfig(BaseModel):
    quiet_window: tuple[str, str] = Field(
        default=("02:00", "07:00"), description="Окно полной тишины, включая прямые обращения"
    )
    morning_reply_window: tuple[str, str] = Field(
        default=("07:00", "08:00"),
        description="Окно утренней реплики всем, кто звал ночью; момент случайный",
    )

    live_talk: LiveTalkConfig = Field(
        default_factory=LiveTalkConfig, description="Что считается живым разговором для ambient"
    )
    ambient_probability: float = Field(
        default=0.15, ge=0.0, le=1.0, description="Шанс ambient-реплики внутри живого разговора"
    )
    chat_cooldown_min: int = Field(
        default=25, ge=0, le=1440, description="Минимум минут между ambient-репликами"
    )
    daily_cap: int = Field(
        default=3, ge=0, le=50, description="Суточный лимит ambient и «просто так» вместе"
    )

    spontaneous: SpontaneousConfig = Field(
        default_factory=SpontaneousConfig, description="«Просто так» — бот пишет сам, когда тихо"
    )
    reactions: ReactionsConfig = Field(
        default_factory=ReactionsConfig,
        description="Реакции-эмодзи вместо молчания на срез гейта по кубику",
    )
    stickers: StickersConfig = Field(
        default_factory=StickersConfig, description="Стикеры вместо текста, второй вызов моделью"
    )
    hot_window: HotWindowConfig = Field(
        default_factory=HotWindowConfig,
        description="Горячее окно живее обычного после /life и /say",
    )
    followup: FollowupConfig = Field(
        default_factory=FollowupConfig,
        description="Дешёвая проверка «это мне?» для сообщений без обращения в окне",
    )
    checkin: CheckinConfig = Field(
        default_factory=CheckinConfig,
        description="Периодическая проверка «вернулся» после закрытия горячего окна",
    )
    presence: PresenceConfig = Field(
        default_factory=PresenceConfig,
        description="Суточный потолок присутствия: доля реплик бота от сообщений людей",
    )
    chat_memory: ChatMemoryConfig = Field(
        default_factory=ChatMemoryConfig,
        description="Долгая память чата: пересказы прошедших разговоров по неделям",
    )
    vision: VisionConfig = Field(
        default_factory=VisionConfig,
        description="Описание фото моделью со зрением вместо плейсхолдера «[фото]»",
    )

    # Кулдаун = задержка, не отказ (решение владельца): обращение всегда получает
    # ответ, кулдаун только сдвигает due_at на этапе responder (earliest).
    mention_cooldown_sec: int = Field(
        default=60, ge=0, le=86400, description="Кулдаун ответа на обращение, на человека, сек"
    )
    mention_chat_cooldown_sec: int = Field(
        default=90, ge=0, le=86400, description="Кулдаун ответа на обращение, на чат, сек"
    )
    # Пока ребята наигрываются; потом снизить.
    mention_daily_cap: int = Field(
        default=50, ge=0, le=200, description="Суточный лимит ответов на прямые обращения"
    )
    reply_as_reply_after_sec: int = Field(
        default=300,
        ge=0,
        le=86400,
        description="После скольких секунд отвечать реплаем, а не просто в чат",
    )

    debounce_sec: tuple[int, int] = Field(
        default=(3, 7), description="Диапазон дебаунса: копит очередь сообщений перед ответом"
    )
    reply_delay_buckets: list[ReplyDelayBucket] = Field(
        default_factory=lambda: [
            ReplyDelayBucket(weight=0.6, range_sec=(30, 180)),
            ReplyDelayBucket(weight=0.3, range_sec=(180, 900)),
            ReplyDelayBucket(weight=0.1, range_sec=(900, 3600)),
        ],
        description="Бакеты задержки ответа на обращение, вес и диапазон секунд",
    )
    urgent_max_delay_sec: int = Field(
        default=180,
        ge=0,
        le=86400,
        description="Потолок задержки для срочных сообщений («сегодня», «сейчас»)",
    )
    late_reply_threshold_sec: int = Field(
        default=600, ge=0, le=86400, description="После скольких секунд ответ считается поздним"
    )
    context_window: int = Field(
        default=30, ge=1, le=200, description="Сколько последних сообщений даётся модели в контекст"
    )
    recent_replies_memory: int = Field(
        default=20,
        ge=1,
        le=200,
        description="Сколько последних реплик бота помнится для дедупликации",
    )
    topic_cooldown_min: int = Field(
        default=45,
        ge=0,
        le=1440,
        description="Минуты молчания после срабатывания стоп-листа на входе",
    )
    message_retention_days: int = Field(
        default=30, ge=1, le=3650, description="Сколько дней хранятся сообщения перед удалением"
    )
    min_gap_sec: int = Field(
        default=300,
        ge=0,
        le=3600,
        description="Минимум секунд между любыми двумя сообщениями бота в чате",
    )

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
    provider: str = Field(default="openrouter", description="Провайдер LLM, OpenAI-совместимый API")
    main_model: str = Field(
        default="anthropic/claude-opus-5", description="Id основной модели в OpenRouter"
    )
    judge_model: str = Field(
        default="openai/gpt-5.4-nano",
        description="Маленькая дешёвая модель-судья (выходной фильтр), другой вендор",
    )
    timeout_sec: int = Field(default=30, ge=1, le=120, description="Таймаут запроса к модели, сек")
    max_tokens: int = Field(
        default=200, ge=1, le=4000, description="Лимит токенов ответа основной модели"
    )
    # 50 обращений + 3 ambient + утро + "просто так" ≈ 55 основных вызовов и
    # столько же судьи ≈ 110, потолок 150 с запасом.
    daily_calls_cap: int = Field(
        default=150,
        ge=0,
        le=1000,
        description="Потолок попыток вызова модели в сутки, судья считается",
    )
    daily_budget_usd: float = Field(
        default=2.0, ge=0.0, le=1000.0, description="Суточный бюджет в долларах; сверх — молчит"
    )
    circuit_errors: int = Field(
        default=5, ge=1, le=100, description="Ошибок подряд для срабатывания предохранителя"
    )
    circuit_pause_min: int = Field(
        default=30, ge=0, le=1440, description="Минуты паузы после срабатывания предохранителя"
    )
    # Fallback-цены за 1M токенов: используются, только если провайдер не вернул usage.cost.
    price_in_usd_per_1m: float = Field(
        default=5.0, ge=0.0, description="Fallback-цена входных токенов за 1M, если нет usage.cost"
    )
    price_out_usd_per_1m: float = Field(
        default=25.0,
        ge=0.0,
        description="Fallback-цена выходных токенов за 1M, если нет usage.cost",
    )

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
    min_rating: float = Field(
        default=4.2, ge=0.0, le=5.0, description="Минимальный рейтинг заведения для белого списка"
    )
    min_reviews: int = Field(
        default=50, ge=0, le=100_000, description="Минимум отзывов у заведения для белого списка"
    )
    require_operational: bool = Field(
        default=True, description="Учитывать только действующие (не закрытые) заведения"
    )
    cache_ttl_days: int = Field(
        default=30,
        ge=1,
        le=3650,
        description="Раз во сколько дней перегонять кэш скриптом наполнения",
    )
    max_per_reply: int = Field(
        default=2, ge=0, le=20, description="Максимум заведений, упомянутых в одной реплике"
    )
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
        ],
        description="Поисковые запросы для офлайн-наполнения кэша заведений (places_fill.py)",
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
    r"\bгде (пиво|бар|паб|пивная|посидеть|выпить|нормальн\w+ пиво|вкусн\w+ пиво)",
    r"\bколись,? где\b",
    r"\bкуда (пойти|податься|зайти)\b",
    r"\bпосовет\w+ (бар|паб|место|куда)\b",
    r"\bкакой (бар|паб|пивняк)\b",
    r"\bесть (бар|паб|место)\b",
    r"\bпивняк\b",
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

_GRUMPY_MARKERS_DEFAULT = [
    r"\bя (же|уже|вам уже) (говорил|сказал|написал)\b",
    r"\bразговор закрыт\b",
    r"\bникому не интересно\b",
    r"\bне буду (говорить|рассказывать|отвечать)\b",
    r"\bнеинтересно\b",
    r"\bотстань",
    r"\bхватит (уже|про|об)",
    r"\bсколько можно\b",
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
    "lech",
    "tyskie",
    "żywiec",
]

_REGEX_LIST_FIELDS = (
    "topic_stop",
    "injection_markers",
    "logistics",
    "urgent",
    "places_request",
    "model_talk",
    "grumpy_markers",
)

# Разрешённый набор эмодзи (решение владельца, CHARACTER.md раздел 3/4) — небольшой,
# редко и к месту. Порядок фиксирован. 💩 и 👍 в этом чате значат «одобряю» (локальная
# шутка про 💩), см. prompts/system.txt.
_ALLOWED_EMOJI_DEFAULT = ["🙁", "🙂", "😂", "😀", "🤪", "💩", "👍"]


class FiltersConfig(BaseModel):
    shadow: bool = Field(
        default=True, description="true = фильтры логируют, но не режут ответ (щадящий старт)"
    )
    places_whitelist: list[str] = Field(
        default_factory=lambda: ["Lidl", "OLX", "Biedronka", "Żabka", "Allegro"],
        description="Бытовые бренды, не заведения из CHARACTER.md раздел 7",
    )
    # Разрешённые эмодзи для regex:emoji/style:emoji_* (CHARACTER.md раздел 3/4).
    allowed_emoji: list[str] = Field(
        default_factory=lambda: list(_ALLOWED_EMOJI_DEFAULT),
        description="Разрешённые эмодзи персонажа, небольшой набор, редко и к месту",
    )
    emoji_max_per_reply: int = Field(
        default=1, ge=0, le=3, description="Максимум разрешённых эмодзи в одной реплике"
    )
    # Если хотя бы в одной из последних N реплик было эмодзи — новое режется (style:emoji_freq).
    emoji_recent_window: int = Field(
        default=4, ge=0, le=20, description="Окно последних реплик для проверки частоты эмодзи"
    )
    # Заведения из CHARACTER.md, раздел 7 — белый список regex:venue/regex:latin,
    # см. _KNOWN_PLACES_DEFAULT.
    known_places: list[str] = Field(
        default_factory=lambda: list(_KNOWN_PLACES_DEFAULT),
        description="Заведения из CHARACTER.md раздел 7, белый список regex:venue/regex:latin",
    )
    # Польский словарь Фёдора — тот же белый список, а для dedup:polish_freq (наоборот)
    # это и есть слова, чью частоту правило ограничивает, см. _POLISH_WORDS_DEFAULT.
    polish_words: list[str] = Field(
        default_factory=lambda: list(_POLISH_WORDS_DEFAULT),
        description="Польский словарь персонажа, белый список и предмет dedup:polish_freq",
    )
    topic_stop: list[str] = Field(
        default_factory=lambda: list(_TOPIC_STOP_DEFAULT),
        description="Стоп-лист тем (вход и выход), регулярки по границам слов",
    )
    injection_markers: list[str] = Field(
        default_factory=lambda: list(_INJECTION_MARKERS_DEFAULT),
        description="Маркеры команд управления ботом (входной гейт, шаг 5a)",
    )
    logistics: list[str] = Field(
        default_factory=lambda: list(_LOGISTICS_DEFAULT),
        description="Логистические фразы: время, «кто идёт», «я пас» — не мешать договорённостям",
    )
    urgent: list[str] = Field(
        default_factory=lambda: list(_URGENT_DEFAULT),
        description="Маркеры срочности («сегодня», «сейчас») — ускоряют бакет задержки ответа",
    )
    places_request: list[str] = Field(
        default_factory=lambda: list(_PLACES_REQUEST_DEFAULT),
        description="Маркеры запроса про заведение («куда сходить», «посоветуй»)",
    )
    model_talk: list[str] = Field(
        default_factory=lambda: list(_MODEL_TALK_DEFAULT),
        description="Маркеры разговора о модели/ИИ, которых персонаж не знает (выходной фильтр)",
    )
    assistant_markers: list[str] = Field(
        default_factory=lambda: list(_ASSISTANT_MARKERS_DEFAULT),
        description="Фразы-маркеры ассистента, не в характере персонажа (выходной фильтр)",
    )
    # Сухие/раздражённые формулировки (style:grumpy, выходной фильтр) — добродушный
    # персонаж не отмахивается от вопросов и не злится на повторы (CHARACTER.md раздел 3).
    grumpy_markers: list[str] = Field(
        default_factory=lambda: list(_GRUMPY_MARKERS_DEFAULT),
        description="Сухие/раздражённые формулировки не в характере персонажа (выходной фильтр)",
    )

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
    persona: PersonaConfig = Field(
        default_factory=PersonaConfig,
        description="Кто такой персонаж: имя, возраст, район, таймзона",
    )
    behaviour: BehaviourConfig = Field(
        default_factory=BehaviourConfig, description="Когда и как часто бот пишет и отвечает"
    )
    llm: LlmConfig = Field(
        default_factory=LlmConfig, description="Модель, бюджет и защита от сбоев LLM-провайдера"
    )
    places: PlacesConfig = Field(
        default_factory=PlacesConfig, description="Наполнение и отбор заведений для рекомендаций"
    )
    filters: FiltersConfig = Field(
        default_factory=FiltersConfig, description="Входной и выходной фильтры, белые и стоп-списки"
    )

    @model_validator(mode="after")
    def _check_reactions_emoji_allowed(self) -> "Config":
        """behaviour.reactions.emoji — подмножество filters.allowed_emoji.

        Проверка живёт здесь, а не на ReactionsConfig: только Config видит оба
        поля одновременно. /set behaviour.reactions.emoji с мусором (не входящим
        в allowed_emoji) должен падать с понятной ошибкой, а не тихо позволять
        боту реагировать эмодзи, которого нет в голосе персонажа.
        """
        allowed = set(self.filters.allowed_emoji)
        bad = [e for e in self.behaviour.reactions.emoji if e not in allowed]
        if bad:
            raise ValueError(
                f"behaviour.reactions.emoji: {bad!r} not in "
                f"filters.allowed_emoji {sorted(allowed)!r}"
            )
        return self
