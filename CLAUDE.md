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
