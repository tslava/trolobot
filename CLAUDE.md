# trolobot — правила для агентов

Телеграм-бот-персонаж «Отец Фёдор» для одного группового чата. Что он делает и почему —
`PLAN.md` (код) и `CHARACTER.md` (персонаж). Читать оба перед любой задачей.
Этот файл — контракт кода: структура, интерфейсы, конвенции. Он главнее привычек.

## Стек и инструменты

- Python 3.12, `uv` для зависимостей и запуска (`uv sync`, `uv run ...`).
- aiogram 3.x, aiosqlite, httpx, pydantic v2, pydantic-settings, PyYAML.
- Dev: pytest, pytest-asyncio (`asyncio_mode = auto`), ruff (lint + format), mypy (strict).
- Перед добавлением зависимости проверить актуальную версию на PyPI и пиновать `>=major.minor`.
- Никаких фреймворков поверх aiogram, никаких ORM. SQL руками в `db.py`.

## Структура

```
pyproject.toml, uv.lock, Dockerfile, docker-compose.yml, .gitignore, .env.example, README.md
config.yaml            # дефолты поведения, раздел 6 CHARACTER.md
few_shot.yaml          # примеры, раздел 5 CHARACTER.md
prompts/system.txt     # системный промпт, раздел 4 CHARACTER.md, со слотами {age} ...
src/trolobot/
  __init__.py          # __version__
  __main__.py          # python -m trolobot -> app.main()
  settings.py          # Settings из .env
  config_models.py     # pydantic-модели config.yaml с диапазонами
  config.py            # load_config(path, overrides) -> Config
  few_shot.py          # load_few_shot(path) -> list[FewShot]; render_few_shot(items) -> str
  sanitize.py          # чистые функции для имён и текста
  db.py                # Database: connect/migrate + методы-репозиторий
  schema.sql           # вся схема этапа 1 из PLAN.md, включая таблицы этапов 3 и 6
  retention.py         # run_retention(db, cfg, now) и периодический таск
  bot.py               # aiogram Router и хендлеры
  app.py               # сборка: settings, config, db, bot, фоновые таски; main()
tests/                 # pytest, по файлу на модуль: test_sanitize.py, test_db.py ...
data/                  # bot.db, в .gitignore, volume в compose
exports/               # выгрузки Telegram, в .gitignore
```

## Интерфейсы этапа 1

Сигнатуры обязательны — на них параллельно пишутся другие модули.

```python
# settings.py
class Settings(BaseSettings):              # читает .env, env_prefix нет
    bot_token: SecretStr
    admin_user_id: int = 0                 # 0 = не задан
    allowed_chat_id: int = 0               # 0 = discovery mode, см. ниже
    openrouter_api_key: SecretStr | None = None
    google_places_key: SecretStr | None = None
    db_path: Path = Path("data/bot.db")
    config_path: Path = Path("config.yaml")
    few_shot_path: Path = Path("few_shot.yaml")
    prompt_path: Path = Path("prompts/system.txt")
    log_level: str = "INFO"

# config_models.py — секции persona, behaviour, llm, places, filters из CHARACTER.md раздел 6.
# Каждое числовое поле с Field(ge=..., le=...) — на них потом опирается валидация /set.
class Config(BaseModel):
    persona: PersonaConfig
    behaviour: BehaviourConfig
    llm: LlmConfig
    places: PlacesConfig
    filters: FiltersConfig                 # shadow: bool = True

# config.py
def load_config(path: Path, overrides: dict[str, str] | None = None) -> Config
# overrides — плоские ключи "behaviour.daily_cap" -> "3", применяются поверх yaml,
# значение парсится через pydantic. Источник overrides — таблица config_overrides (этап 6).

# few_shot.py
class FewShot(BaseModel):
    name: str
    user: str
    speak: bool
    text: str = ""
def load_few_shot(path: Path) -> list[FewShot]
def render_few_shot(items: list[FewShot]) -> str
# формат рендера — как в CHARACTER.md раздел 5: строка "Имя: текст", затем строка JSON.

# sanitize.py — чистые функции, без I/O
def sanitize_display_name(raw: str | None, user_id: int, reserved: set[str]) -> str
# буквы (любой алфавит), пробельные символы -> пробел, дефис; обрезка до 24 по границе слова.
# Пустое, равное без учёта регистра элементу reserved (имя бота + name_triggers) ИЛИ содержащее
# элемент reserved как отдельное слово («Дима Федя», «Дед Мороз») -> f"Участник {stable_n(user_id)}".
# Часть слова («Федяев») — не считается. Это намеренно: имя участника, в котором есть триггер бота,
# путает и детект обращения, и модель в контексте.
def stable_n(user_id: int) -> int          # детерминированное 1..999 по user_id
def normalize_text(raw: str | None) -> str
# переносы строк и табы -> пробел, схлопнуть повторы пробелов, вырезать "<<<" и ">>>", strip
def media_placeholder(message) -> str | None   # "[фото]" | "[стикер]" | "[голосовое]" | "[видео]" | "[файл]" | None

# db.py
class Database:
    def __init__(self, path: Path) -> None
    async def connect(self) -> None        # открыть, PRAGMA journal_mode=WAL, foreign_keys=ON, migrate()
    async def close(self) -> None
    async def migrate(self) -> None        # schema.sql применяется, если user_version == 0; далее номерные миграции
    async def insert_message(self, *, tg_message_id: int, chat_id: int, user_id: int,
                             display_name: str, text: str, reply_to_tg_message_id: int | None,
                             is_bot: bool, created_at: int) -> int
    async def recent_messages(self, chat_id: int, limit: int) -> list[MessageRow]   # по created_at desc, вернуть asc
    async def get_state(self, key: str) -> str | None
    async def set_state(self, key: str, value: str) -> None
    async def delete_state(self, key: str) -> None
    async def get_overrides(self) -> dict[str, str]
    async def purge_older_than(self, cutoff: int, tz: str = "UTC") -> PurgeStats
    # удаляет messages.created_at < cutoff; night_queue с answered_at < cutoff;
    # pending_replies с done_at < cutoff; обнуляет filter_log.candidate_text где created_at < cutoff;
    # удаляет state-ключи с датой в имени старше cutoff (суффикс :YYYY-MM-DD или :YYYY-Www);
    # дата cutoff для сравнения с суффиксом берётся в tz (persona.timezone) — сутки бота локальные.
# MessageRow, PurgeStats — dataclass'ы в db.py. Все времена — unix seconds, int.
# Один aiosqlite-connection на процесс. Все пишущие методы Database берут внутренний asyncio.Lock,
# многостейтментные операции (purge, будущие read-modify-write счётчиков) — целиком под ним.
# Вызывающий код о сериализации не думает.

# retention.py
async def run_retention(db: Database, retention_days: int, tz: str, now: int) -> PurgeStats
async def retention_loop(db: Database, cfg_getter: Callable[[], Config], interval_sec: int = 3600) -> None
# cfg_getter даёт .behaviour.message_retention_days и .persona.timezone

# bot.py
def build_router(deps: Deps) -> Router
# Deps — dataclass: settings, config_getter, db, bot_user_id, reserved_names.
# Хендлер на все сообщения:
#   chat.id != allowed_chat_id -> ничего не пишем, ничего не логируем (кроме discovery mode).
#   Discovery mode (allowed_chat_id == 0): на каждое сообщение одна строка в лог уровня WARNING
#   с chat_id, chat.title, user_id, display_name, чтобы владелец мог заполнить .env. В БД не пишем.
#   Иначе: sanitize, normalize (или media_placeholder), insert_message, лог INFO "Имя: текст".
#   Ничего не отвечаем. Команды на этапе 1 не обрабатываются.

# app.py
async def main() -> None
# порядок: Settings -> load_config(overrides из БД после connect) -> Database.connect ->
# Bot/Dispatcher -> get_me (bot_user_id, username) -> retention task -> start_polling.
# Graceful shutdown по SIGTERM/SIGINT: отменить таски, закрыть БД.
```

## Интерфейсы этапа 2 — гейт и реплей

Типы гейта уже написаны: `gate_types.py` (Trigger, Verdict, GateMessage, RecentActivity,
GateState, StateChange, Decision, PatternsLike). Локальное время: `timeutil.py`
(local_date, day_key, week_key, in_window, seconds_until). Их не менять без согласования.

```python
# config_models.py — FiltersConfig расширяется списками регулярок из CHARACTER.md раздел 6:
class FiltersConfig(BaseModel):
    shadow: bool = True
    places_whitelist: list[str]
    topic_stop: list[str]          # стоп-лист тем, regex по границам слов
    injection_markers: list[str]   # маркеры команд, гейт 5a
    logistics: list[str]           # логистический фильтр: время, «кто идёт», «я пас», ...
    urgent: list[str]              # «сегодня», «сейчас», «через час», «куда идём», «ты где» (этап 3)
    places_request: list[str]      # «куда сходить», «посоветуй», «где посидеть», ... (этап 5)
    model_talk: list[str]          # маркеры модели, выходной фильтр (этап 4)
    assistant_markers: list[str]   # маркеры ассистента (этап 4)
# Дефолты — из карточки, config.yaml их дублирует. Все регулярки компилируются с re.IGNORECASE.

# patterns.py — компиляция один раз, чистые методы. Реализует PatternsLike.
class Patterns:
    def __init__(self, filters: FiltersConfig, name_triggers: list[str], bot_username: str) -> None
    def topic_stop(self, text) -> str | None      # сработавший паттерн (pattern.pattern) или None
    def injection(self, text) -> str | None
    def logistics(self, text) -> str | None
    def name_trigger(self, text) -> str | None    # триггеры по границам слов, IGNORECASE
    def mentions_bot(self, text) -> bool          # "@username" по границе слова, IGNORECASE
    def urgent(self, text) -> bool
    def places_request(self, text) -> bool
    def model_talk(self, text) -> str | None
    def assistant_marker(self, text) -> str | None
# Границы слов для кириллицы: \b в Python работает с Unicode — достаточно. Пустой список — метод всегда None/False.

# gate.py — чистая функция, порядок шагов ровно как в PLAN.md этап 2 (0 — в хендлере, 1–5, 5a, 6–11)
def should_consider(msg: GateMessage, state: GateState, cfg: Config, patterns: PatternsLike,
                    now: int, rng: random.Random) -> Decision
# Причины: gate:is_bot, gate:panic, gate:stop, gate:muted, gate:topic (+ StateChange topic_cooldown_until),
# gate:topic_cooldown, gate:injection, gate:night_queued (verdict QUEUE_NIGHT), gate:mention_cap,
# gate:mention_chat_cooldown, gate:mention_user_cooldown, gate:night, gate:logistics, gate:not_live,
# gate:ambient_cap, gate:ambient_cooldown, gate:dice; pass:mention / pass:reply / pass:name / pass:ambient.
# Приоритет триггера обращения: reply > mention > name. Счётчики (mention_count, ambient_count) гейт НЕ меняет —
# их инкрементит отправка (этап 3). Единственный StateChange гейта — topic_cooldown_until.
# Живой разговор: len(state.recent) >= min_messages и len({r.user_id}) >= min_people; recent уже отфильтрован
# вызывающим по окну и is_bot=0 и включает текущее сообщение.
# Сутки и окна — через timeutil с cfg.persona.timezone.

# db.py — добавить:
async def insert_filter_log(self, *, trigger_tg_message_id: int | None, candidate_text: str | None,
                            verdict: str, stage: str, reason: str, shadow: bool, created_at: int) -> int
async def muted_user_ids(self) -> frozenset[int]
async def recent_activity(self, chat_id: int, since: int) -> list[RecentActivity]   # is_bot=0, created_at >= since, asc
async def enqueue_night(self, *, tg_message_id: int, user_id: int, display_name: str, text: str, created_at: int) -> int
async def apply_state_changes(self, changes: Iterable[StateChange]) -> None          # под write_lock, одной транзакцией
async def filter_log_summary(self, since: int) -> list[tuple[str, int]]              # (stage:reason, count) desc — для /why

# gate_state.py
async def load_gate_state(db: Database, cfg: Config, msg: GateMessage, now: int) -> GateState
# ключи state: panic ("1"/отсутствует), stop_until, topic_cooldown_until, last_mention_reply_at,
# last_mention_reply_at:<user_id>, last_ambient_at, day_key("mention_count"), day_key("ambient_count").
# recent = db.recent_activity(chat_id, now - live_talk.window_min*60).

# bot.py — после insert_message: собрать GateMessage (reply_to_bot = reply_to_message.from_user.id == bot_user_id;
# is_bot из from_user; text как записан), load_gate_state, should_consider(rng=deps.rng), apply_state_changes,
# затем: DROP -> insert_filter_log(stage="gate"); QUEUE_NIGHT -> enqueue_night + insert_filter_log;
# PASS -> insert_filter_log(verdict="pass", reason="pass:<trigger>") и лог INFO "gate pass". Ответа нет до этапа 3.
# Deps получает patterns: Patterns и rng: random.Random.

# export_parser.py — экспорт Telegram Desktop (result.json): messages[] с type=="message", id, date (ISO без tz —
# трактовать как persona.timezone), from, from_id ("user123"/"channel123"), text (str или список str|{type,text}),
# reply_to_message_id, photo/media_type/sticker_emoji/file. Служебные (type=="service") пропускать.
@dataclass class ExportMessage: tg_message_id: int; user_id: int; display_name: str; text: str;
                                reply_to_tg_message_id: int | None; created_at: int
def parse_export(path: Path, tz: str) -> list[ExportMessage]   # text через normalize_text либо media_placeholder-аналог

# replay.py — `python -m trolobot.replay exports/result.json [--config config.yaml] [--seed 1] [--bot-username x]
#   [--bot-user-id N] [--verbose]`
# Прогоняет гейт по экспорту с in-memory состоянием: для PASS считает, что ответ отправлен (инкремент
# mention_count/ambient_count, last_*_at = created_at) — иначе лимиты не проверить. Ничего не отправляет,
# в БД не пишет. Вывод: по дням таблица «дата | сообщений | pass по триггерам | drop по причинам (топ-5)»,
# в конце итог и средние в сутки; --verbose печатает каждое PASS с текстом сообщения.
```

## Конвенции

- Все времена — unix seconds (`int`), таймзона только при показе и при вычислении «суток»
  через `persona.timezone`.
- Логи — stdout, формат `%(asctime)s %(levelname)s %(name)s %(message)s`, уровень из Settings.
  Секреты в лог не попадают никогда.
- Асинхронность везде, где есть I/O. Чистые функции — синхронные и без зависимостей от aiogram,
  чтобы тесты были простыми.
- Тесты не ходят в сеть и не требуют токенов. БД в тестах — временный файл или `:memory:`.
- `ruff check`, `ruff format --check`, `mypy src`, `pytest` должны быть зелёными перед сдачей.
- Один модуль — одна задача. Не трогать чужие модули, если контракт выше не требует.
- Никаких `print`, никаких голых `except`, никаких `# type: ignore` без причины в комментарии.
- Секреты только через `.env`; `.env.example` содержит все ключи с пустыми значениями.

## Чего не делать

- Не отвечать в чат на этапе 1. Вообще.
- Не добавлять веб-сервер, вебхуки, открытые порты.
- Не хранить сообщения дольше `behaviour.message_retention_days`.
- Не вшивать промпт и few-shot в код — только загрузка из файлов.
