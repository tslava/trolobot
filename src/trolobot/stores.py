"""Состояние, которое меняется на горячую: конфиг (этап 6) и промпт/few-shot версии.

ConfigStore держит текущий Config (yaml + config_overrides из БД) и пересобранный
Patterns; PromptStore держит активную версию системного промпта и few-shot,
подгружая их из БД, с сидом из файлов на первом старте. Оба переживают рестарт
через БД — источник истины после сида всегда она, не файлы.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import yaml

from trolobot.config import flatten_config, load_config
from trolobot.config_models import Config
from trolobot.db import Database
from trolobot.few_shot import FewShot, render_few_shot
from trolobot.patterns import Patterns

_ALLOWED_PREFIXES = ("behaviour.", "llm.", "places.", "filters.")


def _rebuild_patterns(cfg: Config, bot_username: str) -> Patterns:
    return Patterns(cfg.filters, cfg.persona.name_triggers, bot_username)


def _first_error_message(exc: ValueError, key: str) -> str:
    """Понятный текст ошибки: убирает обёртку load_config и дублирование ключа.

    load_config при ValidationError отдаёт "invalid config: loc1: msg1; loc2: msg2";
    при неизвестном ключе/невалидном YAML — плоский текст без такой обёртки. В обоих
    случаях берём первое сообщение и, если оно само начинается с key, не повторяем его.
    """
    message = str(exc)
    prefix = "invalid config: "
    if message.startswith(prefix):
        message = message[len(prefix) :]
    first = message.split("; ")[0]
    if first.startswith(f"{key}: "):
        first = first[len(key) + 2 :]
    return first


def _parse_few_shot_yaml(body_yaml: str) -> list[FewShot]:
    """Парсер few-shot из текста YAML — то же, что load_few_shot, но без чтения файла."""
    loaded = yaml.safe_load(body_yaml)
    if loaded is None:
        return []
    if not isinstance(loaded, list):
        raise ValueError("few-shot body must contain a YAML list")
    return [FewShot.model_validate(item) for item in loaded]


class ConfigStore:
    """Config.yaml + config_overrides из БД, с горячим /set и пересобираемым Patterns."""

    def __init__(self, path: Path, db: Database) -> None:
        self._path = path
        self._db = db
        self._bot_username = ""
        self._overrides: dict[str, str] = {}
        self.current: Config = Config()
        self._patterns = _rebuild_patterns(self.current, self._bot_username)
        # Сериализует load/set/unset: без лока два параллельных /set (или /set и
        # рестарт-load) могли бы прочитать один и тот же снимок _overrides, оба
        # пройти валидацию и один из override'ов потерять при записи self.current.
        self._lock = asyncio.Lock()

    async def load(self) -> Config:
        async with self._lock:
            return await self._load_locked()

    async def _load_locked(self) -> Config:
        """Тело load() без захвата self._lock — для вызова из set()/unset(), уже
        держащих его. asyncio.Lock не реентерабелен: повторный load() внутри
        set() дедлокнул бы корутину."""
        overrides = await self._db.get_overrides()
        cfg = load_config(self._path, overrides)
        self._overrides = overrides
        self.current = cfg
        self._patterns = _rebuild_patterns(cfg, self._bot_username)
        return cfg

    def get(self) -> Config:
        return self.current

    def patterns(self) -> Patterns:
        return self._patterns

    def set_bot_username(self, username: str) -> None:
        self._bot_username = username
        self._patterns = _rebuild_patterns(self.current, self._bot_username)

    async def set(
        self, key: str, raw_value: str, changed_by: int, now: int
    ) -> tuple[str | None, str]:
        async with self._lock:
            if key.startswith("persona."):
                raise ValueError("persona меняется только в config.yaml")
            if not key.startswith(_ALLOWED_PREFIXES):
                raise ValueError(f"неизвестный раздел: {key!r}")

            candidate_overrides = dict(self._overrides)
            candidate_overrides[key] = raw_value
            try:
                load_config(self._path, candidate_overrides)
            except ValueError as exc:
                raise ValueError(f"{key}: {_first_error_message(exc, key)}") from exc

            old = await self._db.set_override(key, raw_value, changed_by, now)
            await self._load_locked()
            new = flatten_config(self.current).get(key, raw_value)
            return (old, new)

    async def unset(self, key: str, changed_by: int, now: int) -> str | None:
        async with self._lock:
            await self._db.delete_override(key, changed_by, now)
            await self._load_locked()
            return flatten_config(self.current).get(key)

    def flat(self) -> list[tuple[str, str, bool]]:
        return [
            (key, value, key in self._overrides)
            for key, value in flatten_config(self.current).items()
        ]


class PromptStore:
    """Активные версии системного промпта и few-shot, с сидом из файлов при старте."""

    def __init__(self, db: Database, prompt_path: Path, few_shot_path: Path) -> None:
        self._db = db
        self._prompt_path = prompt_path
        self._few_shot_path = few_shot_path
        self._prompt_version = 0
        self._prompt_body = ""
        self._few_shot_version = 0
        self._few_shot_body_yaml = ""
        # Сериализует load/rollback_prompt/add_example/remove_example: без лока два
        # параллельных /ex add могли бы оба прочитать один и тот же self.examples()
        # и один добавленный пример потерять при записи новой версии few-shot.
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        """Сид версии из файла, только если файл не совпадает ни с одной сохранённой версией.

        Раньше тело файла сравнивалось только с активной версией в БД — после
        `/rollback 1` файл на диске (тело v2) отличается от новой активной (v1),
        и следующий `load()` (например, при рестарте) засеял бы v3 из файла и тем
        самым отменил откат. Сравнение со ВСЕМИ сохранёнными версиями (после strip)
        устраняет это: если тело файла совпадает с любой существующей версией,
        новая не заводится, активной остаётся та, что активна в БД сейчас.
        """
        async with self._lock:
            await self._load_locked()

    async def _load_locked(self) -> None:
        now = int(time.time())

        file_prompt = self._prompt_path.read_text(encoding="utf-8")
        existing_prompt_bodies = await self._db.prompt_version_bodies()
        stripped_prompt_bodies = {body.strip() for body in existing_prompt_bodies}
        if not existing_prompt_bodies or file_prompt.strip() not in stripped_prompt_bodies:
            version = await self._db.add_prompt_version(file_prompt, "seed from file", now)
            self._prompt_version = version
            self._prompt_body = file_prompt
        else:
            active_prompt = await self._db.active_prompt()
            assert active_prompt is not None  # есть версии -> есть активная
            self._prompt_version, self._prompt_body = active_prompt

        file_few_shot = self._few_shot_path.read_text(encoding="utf-8")
        existing_few_shot_bodies = await self._db.few_shot_version_bodies()
        stripped_few_shot_bodies = {body.strip() for body in existing_few_shot_bodies}
        if not existing_few_shot_bodies or file_few_shot.strip() not in stripped_few_shot_bodies:
            version = await self._db.add_few_shot_version(file_few_shot, "seed from file", now)
            self._few_shot_version = version
            self._few_shot_body_yaml = file_few_shot
        else:
            active_few_shot = await self._db.active_few_shot()
            assert active_few_shot is not None  # есть версии -> есть активная
            self._few_shot_version, self._few_shot_body_yaml = active_few_shot

    def system_prompt(self) -> str:
        return self._prompt_body

    def prompt_version(self) -> int:
        return self._prompt_version

    def few_shot_text(self) -> str:
        return render_few_shot(self.examples())

    def few_shot_version(self) -> int:
        return self._few_shot_version

    async def rollback_prompt(self, version: int) -> bool:
        async with self._lock:
            ok = await self._db.activate_prompt(version)
            if ok:
                # Перечитываем активную версию напрямую из БД, а не через полный
                # load(): load() уже само по себе не заводит версию при откате
                # (сравнивает файл со ВСЕМИ сохранёнными версиями, не только с
                # активной), но незачем лишний раз читать файл с диска ради
                # переключения на уже известное тело версии.
                active_prompt = await self._db.active_prompt()
                if active_prompt is not None:
                    self._prompt_version, self._prompt_body = active_prompt
            return ok

    async def add_example(self, name: str, user: str, text: str, now: int) -> int:
        async with self._lock:
            examples = self.examples()
            examples.append(FewShot(name=name, user=user, speak=True, text=text))
            body_yaml = yaml.safe_dump(
                [item.model_dump(mode="json") for item in examples],
                allow_unicode=True,
                sort_keys=False,
            )
            version = await self._db.add_few_shot_version(body_yaml, "/ex add by owner", now)
            self._few_shot_version = version
            self._few_shot_body_yaml = body_yaml
            return version

    async def remove_example(self, index: int, now: int) -> int:
        async with self._lock:
            return await self._remove_example_locked(index, now)

    async def _remove_example_locked(self, index: int, now: int) -> int:
        examples = self.examples()
        if index < 1 or index > len(examples):
            raise ValueError(f"индекс {index} вне диапазона: всего примеров {len(examples)}")
        del examples[-index]
        body_yaml = yaml.safe_dump(
            [item.model_dump(mode="json") for item in examples],
            allow_unicode=True,
            sort_keys=False,
        )
        version = await self._db.add_few_shot_version(body_yaml, "/ex rm by owner", now)
        self._few_shot_version = version
        self._few_shot_body_yaml = body_yaml
        return version

    def examples(self) -> list[FewShot]:
        return _parse_few_shot_yaml(self._few_shot_body_yaml)
