"""Словечки из словаря (CLAUDE.md, "словечки из словаря").

Владелец грузит словари файлом в личку (``/words``), их может быть несколько. Персонаж
изредка и к месту употребляет словечко: в некоторые реплики (кубик, дневной потолок,
пауза в несколько реплик) модели предлагается пара слов со значением. И знает значение,
если спросят: слово из словаря, мелькнувшее в разговоре, уходит в промпт со значением.
Новых вызовов модели нет — только подстановка в слот ``{vocab}``.

Словарь — данные владельца (как ``{life}``), а не ввод участников, поэтому живёт в
system. В публичный репозиторий словари не коммитятся: это чужой авторский текст, они
лежат в БД (``vocab_dicts``), в репо только ``vocab.example.yaml``.

Чистая часть (разбор, матчинг, рендер) — без I/O; ``VocabStore`` — тонкая обёртка над
БД по образцу ``stores.PromptStore``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from trolobot.config_models import VocabConfig

if TYPE_CHECKING:
    from trolobot.db import Database, VocabDictRow

logger = logging.getLogger(__name__)

# Знаки ударения (острое и тупое) — на imsiberian.com они в каждом слове.
_ACCENTS_RE = re.compile("[̀́]")
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_SPACES_RE = re.compile(r"\s+")
_LT_RUN_RE = re.compile(r"<{3,}")
_GT_RUN_RE = re.compile(r">{3,}")
# Тире в промпте модель копирует в реплики (CLAUDE.md, "пост-обработка").
_DASH_RE = re.compile(r"\s*[‒–—―]\s*")
_STICKER_PREFIX = "[стикер #"

# Формы слова ищутся по основе и ЗАКРЫТОМУ списку русских окончаний (ревью Codex
# 17.10: «любой хвост до N букв» путал «колбас» с «колба»). Основа короче трёх букв —
# только точное совпадение.
_REFLEXIVE = ("ся", "сь")
_VERB_ENDINGS = ("ть", "ти", "чь")
_ADJ_ENDINGS_WORD = ("ый", "ий", "ой")
_FLEETING = ("ок", "ек", "ец")  # беглая гласная: гомонок -> гомонка, обабок -> обабков
_SOFT_ENDINGS = frozenset("аеиоуыэюяйь")
_NOUN_ENDINGS = frozenset(
    {"", "а", "я", "о", "е", "ы", "и", "у", "ю", "ь", "й", "ой", "ей", "ою", "ею", "ом", "ем"}
    | {"ам", "ям", "ах", "ях", "ами", "ями", "ов", "ев", "ью"}
)
_ADJ_ENDINGS = frozenset(
    {"ый", "ий", "ой", "ая", "яя", "ое", "ее", "ые", "ие", "ого", "его", "ому", "ему", "ую"}
    | {"юю", "ых", "их", "ым", "им", "ыми", "ими", "ом", "ем", "", "а", "о", "ы", "и"}
)
_NOMINAL_ENDINGS = _NOUN_ENDINGS | _ADJ_ENDINGS
# Окончания на основе инфинитива: шорка|л, арга|ет(ся), бурхани|ла, шуруди|.
_VERB_FORMS = frozenset(
    {"", "ть", "ти", "чь", "л", "ла", "ло", "ли", "в", "вши", "я", "й", "йте"}
    | {"ю", "у", "ешь", "ет", "ем", "ете", "ют", "ут"}
)
# Настоящее время, где основа инфинитива меняется: бурхан|ит, греб|у|ешь, ша|ют.
_PRESENT_E = frozenset({"ю", "у", "ешь", "ет", "ем", "ете", "ют", "ут", "й", "йте", "я"})
_PRESENT_I = frozenset({"ю", "у", "ишь", "ит", "им", "ите", "ят", "ат", "ь", "я"})
# Глагольные приставки: «по|бурханил», «на|барагозил», «от|мочили». Отрезаются только
# перед глагольной основой и только если после них остаётся длинное слово.
_VERB_PREFIXES = ("пере", "при", "раз", "рас", "под", "над", "об", "от", "вы", "за", "на", "по")
_PREFIXED_MIN = 5
_MIN_STEM = 3
_PREFIX_KEY = 3

_WORD_MAX = 40
_MEANING_MAX = 300
_EXAMPLE_MAX = 200
_ABOUT_MAX = 200
_NAME_MAX = 40
_FORMS_MAX = 30
_WORDS_MAX = 2000
_ERRORS_SHOWN = 5

_OFFER_HEADER = "Можешь к месту употребить одно из этих словечек"
_OFFER_TAIL = ", только если оно само ложится в реплику; не объясняй его и не бери в кавычки: "
_MEANINGS_HEADER = (
    "Справка: в разговоре мелькнули словечки. Если спросят, что они значат, объясни "
    "своими словами; сам их без вопроса не повторяй: "
)


def normalize(text: str) -> str:
    """Без ударений, в нижнем регистре, «ё» -> «е», пробелы схлопнуты."""
    cleaned = _ACCENTS_RE.sub("", text).lower().replace("ё", "е")
    return _SPACES_RE.sub(" ", cleaned).strip()


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(normalize(text))


def _display(text: str) -> str:
    return _SPACES_RE.sub(" ", _ACCENTS_RE.sub("", text)).strip()


class VocabWord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    word: str = Field(min_length=1, max_length=_WORD_MAX)
    meaning: str = Field(min_length=1, max_length=_MEANING_MAX)
    example: str = Field(default="", max_length=_EXAMPLE_MAX)
    forms: list[str] = Field(default_factory=list, max_length=_FORMS_MAX)

    @field_validator("word", "meaning", "example", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("word")
    @classmethod
    def _has_letters(cls, value: str) -> str:
        if not _tokens(value):
            raise ValueError("в слове нет букв")
        return value

    @field_validator("forms")
    @classmethod
    def _forms(cls, value: list[str]) -> list[str]:
        cleaned = [form.strip() for form in value if form.strip()]
        for form in cleaned:
            if len(form) > _WORD_MAX or not _tokens(form):
                raise ValueError(f"кривая форма «{form[:_WORD_MAX]}»")
        return cleaned


class VocabDict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=_NAME_MAX)
    about: str = Field(default="", max_length=_ABOUT_MAX)
    words: list[VocabWord] = Field(min_length=1, max_length=_WORDS_MAX)

    @field_validator("name", "about", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return " ".join(value.split()) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _unique_words(self) -> VocabDict:
        seen: set[str] = set()
        for item in self.words:
            key = normalize(item.word)
            if key in seen:
                raise ValueError(f"слово «{_display(item.word)}» повторяется")
            seen.add(key)
        return self


def _format_loc(loc: tuple[int | str, ...]) -> str:
    parts: list[str] = []
    for i, part in enumerate(loc):
        if isinstance(part, int) and i > 0 and loc[i - 1] == "words":
            parts.append(f"слово #{part + 1}")
        elif isinstance(part, int):
            parts.append(f"#{part + 1}")
        else:
            parts.append(part)
    return " → ".join(parts)


def parse_vocab(raw: str) -> VocabDict:
    """YAML -> VocabDict. Любая ошибка — ``ValueError`` с текстом для владельца."""
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML не читается: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("ожидался YAML-объект с полями name и words")
    try:
        return VocabDict.model_validate(data)
    except ValidationError as exc:
        lines = []
        for error in exc.errors()[:_ERRORS_SHOWN]:
            loc = _format_loc(tuple(error["loc"]))
            lines.append(f"{loc}: {error['msg']}" if loc else str(error["msg"]))
        more = len(exc.errors()) - _ERRORS_SHOWN
        if more > 0:
            lines.append(f"и ещё {more}")
        raise ValueError("; ".join(lines)) from exc


@dataclass(frozen=True, slots=True)
class VocabEntry:
    word: str  # без ударений, как в словаре
    meaning: str
    example: str
    about: str
    dict_name: str


@dataclass(frozen=True, slots=True)
class _Rule:
    stem: str
    endings: frozenset[str]
    reflexive: bool | None  # None — не глагол; True — форма обязана кончаться на «ся/сь»
    entry: VocabEntry


def _rules(word: str) -> list[tuple[str, frozenset[str], bool | None]]:
    """Основы и допустимые окончания для слова (CLAUDE.md, "словечки из словаря")."""
    base, reflexive = word, False
    for suffix in _REFLEXIVE:
        if base.endswith(suffix) and base[: -len(suffix)].endswith(_VERB_ENDINGS):
            base, reflexive = base[: -len(suffix)], True
            break
    for ending in _VERB_ENDINGS:
        if base.endswith(ending) and len(base) > len(ending) + 2:
            stem = base[: -len(ending)]
            rules: list[tuple[str, frozenset[str], bool | None]] = [(stem, _VERB_FORMS, reflexive)]
            if stem.endswith(("ова", "ева")):
                rules.append(
                    (stem[:-3] + ("у" if stem.endswith("ова") else "ю"), _PRESENT_E, reflexive)
                )
            elif stem.endswith(("и", "е")):
                rules.append((stem[:-1], _PRESENT_I, reflexive))
            elif stem.endswith("я"):
                rules.append((stem[:-1], _PRESENT_E, reflexive))
            elif stem.endswith("ну"):  # бздану|ть -> бздан|и, бздан|ешь
                rules.append((stem[:-1], _PRESENT_E | {"и", "ите"}, reflexive))
            return rules
    rules = []
    if base.endswith(_ADJ_ENDINGS_WORD) and len(base) >= 5:
        rules.append((base[:-2], _NOMINAL_ENDINGS, None))
        if not base.endswith("ой"):  # «шишкобой» — существительное: шишкобо|ев
            return rules
    stem = base[:-1] if base[-1] in _SOFT_ENDINGS else base
    rules.append((stem, _NOMINAL_ENDINGS, None))
    if base.endswith(_FLEETING) and len(base) > 4:
        rules.append((base[:-2] + base[-1], _NOMINAL_ENDINGS - {""}, None))
    return rules


class Vocab:
    """Включённые словари, собранные в один матчер. Повтор слова в двух словарях —
    берётся первое (словари идут по имени)."""

    def __init__(self, dicts: Sequence[VocabDict] = ()) -> None:
        entries: list[VocabEntry] = []
        self._exact: dict[str, VocabEntry] = {}
        self._phrases: dict[tuple[str, ...], VocabEntry] = {}
        self._rules: dict[str, list[_Rule]] = {}
        seen: set[str] = set()
        for vocab_dict in dicts:
            for item in vocab_dict.words:
                key = normalize(item.word)
                if key in seen:
                    continue
                seen.add(key)
                entry = VocabEntry(
                    word=_display(item.word),
                    meaning=_display(item.meaning),
                    example=_display(item.example),
                    about=vocab_dict.about,
                    dict_name=vocab_dict.name,
                )
                entries.append(entry)
                self._index(entry, item)
        self.entries: tuple[VocabEntry, ...] = tuple(entries)
        self._max_phrase = max((len(p) for p in self._phrases), default=0)

    def _index(self, entry: VocabEntry, item: VocabWord) -> None:
        for variant in (item.word, *item.forms):
            tokens = tuple(_tokens(variant))
            if len(tokens) > 1:
                self._phrases.setdefault(tokens, entry)
            elif tokens:
                self._exact.setdefault(tokens[0], entry)
        word_tokens = _tokens(item.word)
        if item.forms or len(word_tokens) != 1:
            return
        for stem, endings, reflexive in _rules(word_tokens[0]):
            if len(stem) >= _MIN_STEM:
                rule = _Rule(stem, endings, reflexive, entry)
                self._rules.setdefault(stem[:_PREFIX_KEY], []).append(rule)
            # Родительный множественного с беглой гласной: евражк|а -> евражек.
            if reflexive is None and len(stem) > _MIN_STEM and stem.endswith("к"):
                for vowel in ("е", "о"):
                    self._exact.setdefault(f"{stem[:-1]}{vowel}к", entry)

    def __bool__(self) -> bool:
        return bool(self.entries)

    def _match_token(self, token: str) -> VocabEntry | None:
        exact = self._exact.get(token)
        if exact is not None:
            return exact
        found = self._match_rules(token, verbs_only=False)
        if found is not None:
            return found
        for prefix in _VERB_PREFIXES:
            if token.startswith(prefix) and len(token) - len(prefix) >= _PREFIXED_MIN:
                found = self._match_rules(token[len(prefix) :], verbs_only=True)
                if found is not None:
                    return found
        return None

    def _match_rules(self, token: str, *, verbs_only: bool) -> VocabEntry | None:
        # Несколько подходящих слов — берётся самая длинная основа («аргамак» против
        # «арга-»). Возвратный и невозвратный глагол с одной основой («шарить» и
        # «шариться») не спорят: форма на «ся/сь» подходит только возвратному.
        best: _Rule | None = None
        for rule in self._rules.get(token[:_PREFIX_KEY], ()):
            if not token.startswith(rule.stem) or (verbs_only and rule.reflexive is None):
                continue
            rest = token[len(rule.stem) :]
            if rule.reflexive:
                if not rest.endswith(_REFLEXIVE):
                    continue
                rest = rest[:-2]
            if rest in rule.endings and (best is None or len(rule.stem) > len(best.stem)):
                best = rule
        return best.entry if best is not None else None

    def find(self, text: str) -> list[VocabEntry]:
        """Слова словаря, упомянутые в ``text``, по порядку первого появления, без повторов."""
        if not self.entries or not text:
            return []
        tokens = _tokens(text)
        found: list[VocabEntry] = []
        seen: set[int] = set()
        i = 0
        while i < len(tokens):
            matched: VocabEntry | None = None
            width = 1
            for size in range(min(self._max_phrase, len(tokens) - i), 1, -1):
                phrase = self._phrases.get(tuple(tokens[i : i + size]))
                if phrase is not None:
                    matched, width = phrase, size
                    break
            if matched is None:
                matched = self._match_token(tokens[i])
            if matched is not None and id(matched) not in seen:
                seen.add(id(matched))
                found.append(matched)
            i += width
        return found


def _speech(replies: Sequence[str]) -> list[str]:
    return [reply for reply in replies if not reply.startswith(_STICKER_PREFIX)]


def offer_allowed(
    *,
    cfg: VocabConfig,
    vocab: Vocab,
    recent_replies: Sequence[str],
    count_today: int,
    rng: random.Random,
) -> bool:
    """Предложить ли модели словечки в этой реплике. Кубик последним: ``rng`` тратится,
    только когда всё остальное позволяет (как ``reactions.pick_reaction``)."""
    if not cfg.enabled or not vocab:
        return False
    if count_today >= cfg.daily_cap:
        return False
    window = _speech(recent_replies)[-cfg.recent_window :] if cfg.recent_window > 0 else []
    if any(vocab.find(reply) for reply in window):
        return False
    return rng.random() < cfg.probability


def pick_offer(
    vocab: Vocab,
    *,
    recent_replies: Sequence[str],
    size: int,
    rng: random.Random,
    exclude: Sequence[VocabEntry] = (),
) -> list[VocabEntry]:
    """``size`` случайных слов, которых не было в ``recent_replies`` и нет в ``exclude``."""
    used = {id(entry) for reply in _speech(recent_replies) for entry in vocab.find(reply)}
    used.update(id(entry) for entry in exclude)
    pool = [entry for entry in vocab.entries if id(entry) not in used]
    return rng.sample(pool, min(size, len(pool)))


def _clean(text: str) -> str:
    cleaned = _GT_RUN_RE.sub(" ", _LT_RUN_RE.sub(" ", text))
    cleaned = _DASH_RE.sub(", ", cleaned)
    return " ".join(cleaned.split()).strip().strip('«»"').rstrip(".;").strip()


def _meaning(text: str) -> str:
    """Значение со строчной: в словарях оно с заглавной («Промозглая, сырая погода»),
    а в промпте идёт посреди фразы. Аббревиатуры («СССР») не трогаем."""
    cleaned = _clean(text)
    if len(cleaned) > 1 and cleaned[0].isupper() and not cleaned[1].isupper():
        return cleaned[0].lower() + cleaned[1:]
    return cleaned


def render_vocab(offer: Sequence[VocabEntry], meanings: Sequence[VocabEntry]) -> str:
    """Блок слота ``{vocab}``: предложение словечек и/или значения мелькнувших.
    Пусто -> "". Без «- » в начале строк (regex:prompt_leak) и без тире."""
    parts: list[str] = []
    if offer:
        abouts = {_clean(entry.about) for entry in offer}
        about = abouts.pop() if len(abouts) == 1 else ""
        items = []
        for entry in offer:
            example = _clean(entry.example)
            meaning = _meaning(entry.meaning)
            detail = f"{meaning}; «{example}»" if example else meaning
            items.append(f"{_clean(entry.word)} ({detail})")
        header = f"{_OFFER_HEADER} ({about})" if about else _OFFER_HEADER
        parts.append(header + _OFFER_TAIL + "; ".join(items) + ".")
    if meanings:
        items = [f"{_clean(entry.word)}: {_meaning(entry.meaning)}" for entry in meanings]
        parts.append(_MEANINGS_HEADER + "; ".join(items) + ".")
    return "\n".join(parts)


class VocabStore:
    """Словари из БД, собранные в ``Vocab``. Источник истины — таблица ``vocab_dicts``;
    в памяти — только разобранные включённые словари, пересобираются после каждой правки."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._lock = asyncio.Lock()
        self._vocab = Vocab()

    def get(self) -> Vocab:
        return self._vocab

    async def load(self) -> None:
        async with self._lock:
            await self._load_locked()

    async def _load_locked(self) -> None:
        dicts: list[VocabDict] = []
        for row in await self._db.vocab_dicts():
            if not row.enabled:
                continue
            try:
                dicts.append(parse_vocab(row.body))
            except ValueError as exc:
                logger.warning("словарь %r не разобрался, пропущен: %s", row.name, exc)
        self._vocab = Vocab(dicts)
        logger.info("vocab: %d dicts, %d words", len(dicts), len(self._vocab.entries))

    async def put(self, raw: str, now: int) -> VocabDict:
        """Разобрать и сохранить словарь (тот же ``name`` — замена). ``ValueError`` наружу."""
        parsed = parse_vocab(raw)
        async with self._lock:
            await self._db.upsert_vocab_dict(parsed.name, raw, now)
            await self._load_locked()
        return parsed

    async def set_enabled(self, name: str, enabled: bool) -> bool:
        async with self._lock:
            changed = await self._db.set_vocab_dict_enabled(name, enabled)
            if changed:
                await self._load_locked()
            return changed

    async def remove(self, name: str) -> bool:
        async with self._lock:
            removed = await self._db.delete_vocab_dict(name)
            if removed:
                await self._load_locked()
            return removed

    async def dicts(self) -> list[tuple[VocabDictRow, VocabDict | None]]:
        result: list[tuple[VocabDictRow, VocabDict | None]] = []
        for row in await self._db.vocab_dicts():
            try:
                parsed: VocabDict | None = parse_vocab(row.body)
            except ValueError:
                parsed = None
            result.append((row, parsed))
        return result


def vocab_over_quota(
    vocab: Vocab, *, cfg: VocabConfig, recent_replies: Sequence[str], count_today: int
) -> bool:
    """Словечек на сегодня хватит: потолок суток выбран или словечко уже звучало в
    последних ``recent_window`` репликах. Тогда выходной фильтр режет реплику с
    непрошеным словечком (``dedup:vocab``) — подсказка в промпте одна лимит не держит
    (ревью Codex 17.10)."""
    if count_today >= cfg.daily_cap:
        return True
    window = _speech(recent_replies)[-cfg.recent_window :] if cfg.recent_window > 0 else []
    return any(vocab.find(reply) for reply in window)
