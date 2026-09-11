"""Выходной фильтр — три слоя (PLAN.md, этап 4; CLAUDE.md, "Интерфейсы этапа 4").

Слой 1 (``layer_regex``) и слой 2 (``layer_rules``) — чистые синхронные функции,
0 мс, без I/O. Слой 3 (LLM-судья, ``judge.py``) вызывается через ``check_output``
только если слои 1-2 прошли или включён shadow mode — провал любого слоя означает
молчание, ретраев нет. ``FilterContext``/``FilterVerdict`` — дата-классы контракта,
``check_output`` — единственная точка входа для ``responder.py``.

Нормализация слов для эхо/дедупа/утечки промпта — везде одна и та же: нижний
регистр, ``ё`` -> ``е``, пунктуация вырезается, разбивка по пробелам
(см. ``_normalize_words``). Белый список для "выдуманных заведений" и латиницы —
``places_names`` + ``filters.places_whitelist`` + ``filters.known_places`` (заведения
из CHARACTER.md раздел 7, по каждому слову) + ``filters.polish_words`` (словарь
Фёдора) + районы из карточки + латинские токены, которые сами участники уже
употребили в ``trigger_text``/``context_rows`` (раз человек сам назвал место или
слово — бот не выдумывает, а повторяет). Для ``dedup:polish_freq`` белый список уже —
``places_names``/``places_whitelist``/``known_places`` без ``polish_words``: польские
слова там нарочно считаются латинскими токенами, это и есть цель правила.
"""

from __future__ import annotations

import re
import string
import unicodedata
from dataclasses import dataclass, field
from typing import Protocol, cast

from trolobot.config_models import Config
from trolobot.db import MessageRow
from trolobot.gate_types import PatternsLike
from trolobot.patterns import Patterns

_MAX_LEN = 300

_MARKDOWN_LIST_RE = re.compile(r"(^|\n)\s*[-*•] ")
_MARKDOWN_NUM_RE = re.compile(r"(^|\n)\s*\d+\.\s")
_MARKDOWN_HEADER_RE = re.compile(r"(^|\n)\s*#")

_EMOJI_RANGES = ((0x1F300, 0x1FAFF), (0x2600, 0x27BF))
_EMOJI_CATEGORIES = frozenset({"So", "Sk"})
# Вариационный селектор U+FE0F и цветовые модификаторы кожи U+1F3FB-U+1F3FF, сразу
# следующие за эмодзи (напр. "👍️" или "👍🏻"), — часть того же символа, не
# отдельный эмодзи (CLAUDE.md, правки этапа 4).
_VARIATION_SELECTOR_16 = "\ufe0f"
_SKIN_TONE_LO, _SKIN_TONE_HI = 0x1F3FB, 0x1F3FF

_SENTENCE_SPLIT_RE = re.compile(r"[.!?…]+(?:\s|$)")
# Фрагмент между разделителями [.!?…] считается предложением только от 3 слов
# (CLAUDE.md, "regex:sentences") — рубленая байка из коротких фраз ("Дважды.",
# "Бывает.") не должна ложно резаться как лекция из нескольких предложений.
_MIN_SENTENCE_WORDS = 3

_PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{8,}\d")

# Латинская буква (в т.ч. польские диакритики) — общий алфавит для всех латинских проверок.
_LATIN_LETTERS = "A-Za-zĄąĆćĘęŁłŃńÓóŚśŹźŻż"
_LATIN_TOKEN_FULL_RE = re.compile(rf"[{_LATIN_LETTERS}]+")
# Заглавное латинское слово от трёх букв — грубый прокси "похоже на заведение"
# (PLAN.md, этап 4: регуляркой заведение не распознать, это осознанно грубо).
_VENUE_WORD_RE = re.compile(rf"\b[A-ZĄĆĘŁŃÓŚŹŻ][{_LATIN_LETTERS}]{{2,}}\b")
# Латинский токен от двух букв — для частоты польского (dedup:polish_freq).
_LATIN_TOKEN_2PLUS_RE = re.compile(rf"[{_LATIN_LETTERS}]{{2,}}")

_DISTRICTS = (
    "Wilda",
    "Jeżyce",
    "Stare Miasto",
    "Grunwald",
    "Łazarz",
    "Rataje",
    "Winogrady",
    "Poznań",
    "Kórnik",
    "Puszczykowo",
    "Strzeszyn",
)

_PUNCT_CHARS = string.punctuation + "«»„”“—–…"

_NORMALIZE_STRIP_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SLOT_MARKER_RE = re.compile(r"\{[a-z_]+\}")


def _normalize_words(text: str) -> list[str]:
    """lower, ё->е, вырезать пунктуацию, split по пробелам."""
    lowered = text.lower().replace("ё", "е")
    cleaned = _NORMALIZE_STRIP_RE.sub(" ", lowered)
    return cleaned.split()


def _ngrams(words: list[str], n: int) -> set[tuple[str, ...]]:
    if len(words) < n:
        return set()
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def _jaccard(a: set[tuple[str, ...]], b: set[tuple[str, ...]]) -> float:
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _first_word(text: str) -> str:
    """Первый пробельный токен текста, без обрамляющей пунктуации."""
    stripped = text.lstrip()
    match = re.match(r"\S+", stripped)
    if match is None:
        return ""
    return match.group(0).strip(_PUNCT_CHARS)


def _fold(text: str) -> str:
    return text.strip().casefold().replace("ё", "е")


def _patterns_for(ctx: FilterContext) -> Patterns:
    """Возвращает готовый Patterns вызывающего (``ctx.patterns``), если он задан.

    Иначе собирает новый из ``ctx.cfg`` — медленный путь, годный только для тестов,
    которые не передают ``patterns`` явно. ``check_output`` вызывает это ровно один
    раз за проверку и передаёт результат в оба слоя, чтобы не компилировать один и
    тот же набор регулярок дважды на каждый кандидат.
    """
    if ctx.patterns is not None:
        return cast(Patterns, ctx.patterns)
    return Patterns(ctx.cfg.filters, ctx.cfg.persona.name_triggers, "")


def _latin_tokens_any(text: str) -> set[str]:
    return {m.group(0).casefold() for m in _LATIN_TOKEN_FULL_RE.finditer(text)}


def _words_of(phrases: list[str]) -> set[str]:
    """Полная фраза (casefold) + каждое отдельное слово (casefold) из списка фраз."""
    words: set[str] = set()
    for phrase in phrases:
        folded = phrase.casefold()
        if folded:
            words.add(folded)
        words |= {w.casefold() for w in phrase.split()}
    return words


def _venue_whitelist(ctx: FilterContext) -> set[str]:
    """Белый список для regex:venue и regex:latin (общий, см. докстринг модуля).

    places_names + filters.places_whitelist + filters.known_places (заведения из
    CHARACTER.md раздел 7, многословные названия — по каждому слову) + districts +
    filters.polish_words (словарь Фёдора) + латинские токены, которые сами участники
    уже употребили в trigger_text/context_rows.
    """
    words = _words_of(
        [*ctx.places_names, *ctx.cfg.filters.places_whitelist, *ctx.cfg.filters.known_places]
    )
    words |= {name.casefold() for name in _DISTRICTS}
    words |= {word.casefold() for word in ctx.cfg.filters.polish_words}
    words |= _latin_tokens_any(ctx.trigger_text)
    for row in ctx.context_rows:
        if row.text:
            words |= _latin_tokens_any(row.text)
    return words


def _places_whitelist(ctx: FilterContext) -> set[str]:
    """Более узкий белый список для dedup:polish_freq — места, не польские слова.

    places_names + filters.places_whitelist + filters.known_places, по словам (как
    в _venue_whitelist). filters.polish_words сюда осознанно не входят: это и есть
    латинские токены, чью частоту dedup:polish_freq ограничивает.
    """
    return _words_of(
        [*ctx.places_names, *ctx.cfg.filters.places_whitelist, *ctx.cfg.filters.known_places]
    )


# --------------------------------------------------------------------------- #
# Слой 1 — regex:*
# --------------------------------------------------------------------------- #


def _has_markdown(text: str) -> bool:
    if _MARKDOWN_LIST_RE.search(text) or _MARKDOWN_NUM_RE.search(text):
        return True
    if "**" in text or "```" in text:
        return True
    return _MARKDOWN_HEADER_RE.search(text) is not None


def _is_emoji_char(ch: str) -> bool:
    code_point = ord(ch)
    if any(lo <= code_point <= hi for lo, hi in _EMOJI_RANGES):
        return True
    return unicodedata.category(ch) in _EMOJI_CATEGORIES


def _has_emoji(text: str) -> bool:
    return any(_is_emoji_char(ch) for ch in text)


def _is_emoji_modifier(ch: str) -> bool:
    """U+FE0F (вариационный селектор) или цветовой модификатор кожи U+1F3FB-U+1F3FF,
    сразу следующий за базовым эмодзи ("👍️", "👍🏻") — часть того же символа."""
    if ch == _VARIATION_SELECTOR_16:
        return True
    return _SKIN_TONE_LO <= ord(ch) <= _SKIN_TONE_HI


def _emoji_tokens(text: str) -> list[tuple[str, int]]:
    """Список (базовый символ эмодзи, индекс конца граммемы) для каждого эмодзи в
    тексте. Модификаторы (``_is_emoji_modifier``) сразу после базового символа
    поглощаются тем же токеном и не считаются отдельным эмодзи."""
    tokens: list[tuple[str, int]] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if _is_emoji_char(ch):
            end = i + 1
            while end < n and _is_emoji_modifier(text[end]):
                end += 1
            tokens.append((ch, end))
            i = end
        else:
            i += 1
    return tokens


def _sentence_count(text: str) -> int:
    parts = _SENTENCE_SPLIT_RE.split(text)
    return sum(1 for part in parts if len(part.split()) >= _MIN_SENTENCE_WORDS)


def _starts_name_set(ctx: FilterContext) -> set[str]:
    names: set[str] = set()
    for name in ctx.participant_names:
        folded = _fold(name)
        if not folded:
            continue
        names.add(folded)
        words = folded.split()
        if words:
            names.add(words[0])
    for name in ctx.bot_names:
        folded = _fold(name)
        if folded:
            names.add(folded)
    return names


def _check_starts_name(text: str, ctx: FilterContext) -> bool:
    first = _fold(_first_word(text))
    if not first:
        return False
    return first in _starts_name_set(ctx)


def _check_venue(text: str, whitelist: set[str]) -> bool:
    for match in _VENUE_WORD_RE.finditer(text):
        if match.group(0).casefold() not in whitelist:
            return True
    return False


def _check_muted(text: str, muted_names: list[str]) -> bool:
    for name in muted_names:
        stripped = name.strip()
        if not stripped:
            continue
        if re.search(rf"\b{re.escape(stripped)}\b", text, re.IGNORECASE):
            return True
    return False


def _check_echo(text: str, ctx: FilterContext) -> bool:
    text_grams = _ngrams(_normalize_words(text), 4)
    if not text_grams:
        return False
    for row in ctx.context_rows:
        if row.is_bot or not row.text:
            continue
        row_grams = _ngrams(_normalize_words(row.text), 4)
        if text_grams & row_grams:
            return True
    return False


def _prompt_leak_source_lines(system_prompt: str) -> list[str]:
    """Только инструктивная часть промпта: строки-буллеты "- ..." (после strip) и
    строки, содержащие "JSON" (блок про формат ответа). Биография ("одно пиво за
    вечер", CHARACTER.md раздел 4) предназначена для пересказа персонажем и осознанно
    не считается утечкой, даже если кандидат почти дословно её повторяет.
    """
    return [
        line
        for raw_line in system_prompt.splitlines()
        for line in (raw_line.strip(),)
        if line.startswith("- ") or "JSON" in line
    ]


def _check_prompt_leak(text: str, system_prompt: str) -> bool:
    if not system_prompt:
        return False
    source_lines = _prompt_leak_source_lines(system_prompt)
    if not source_lines:
        return False
    cleaned_prompt = _SLOT_MARKER_RE.sub(" ", "\n".join(source_lines))
    prompt_grams = _ngrams(_normalize_words(cleaned_prompt), 6)
    if not prompt_grams:
        return False
    text_grams = _ngrams(_normalize_words(text), 6)
    return bool(prompt_grams & text_grams)


def _allowed_emoji_set(ctx: FilterContext) -> frozenset[str]:
    return frozenset(ctx.cfg.filters.allowed_emoji)


def _check_emoji_disallowed(text: str, allowed: frozenset[str]) -> bool:
    """regex:emoji — эмодзи вне filters.allowed_emoji."""
    return any(ch not in allowed for ch, _ in _emoji_tokens(text))


def _check_latin_run(text: str, whitelist: set[str]) -> bool:
    run = 0
    for raw_token in text.split():
        token = raw_token.strip(_PUNCT_CHARS)
        if token and _LATIN_TOKEN_FULL_RE.fullmatch(token) and token.casefold() not in whitelist:
            run += 1
            if run >= 2:
                return True
        else:
            run = 0
    return False


def layer_regex(text: str, ctx: FilterContext, patterns: Patterns) -> list[str]:
    """Слой 1: регулярки, 0 мс. Порядок — как в CLAUDE.md, "Интерфейсы этапа 4".

    ``patterns`` собирается один раз в ``check_output`` (``_patterns_for``) и
    передаётся сюда и в ``layer_rules`` — чтобы не компилировать regex дважды на
    каждый кандидат.
    """
    reasons: list[str] = []

    if len(text) > _MAX_LEN:
        reasons.append("regex:length")
    if _has_markdown(text):
        reasons.append("regex:markdown")
    if _check_emoji_disallowed(text, _allowed_emoji_set(ctx)):
        reasons.append("regex:emoji")
    if _sentence_count(text) > 2:
        reasons.append("regex:sentences")
    if _check_starts_name(text, ctx):
        reasons.append("regex:starts_name")
    if _PHONE_RE.search(text):
        reasons.append("regex:phone")

    whitelist = _venue_whitelist(ctx)
    if _check_venue(text, whitelist):
        reasons.append("regex:venue")

    if patterns.topic_stop(text) is not None:
        reasons.append("regex:topic")
    if _check_muted(text, ctx.muted_names):
        reasons.append("regex:muted_name")
    if _check_echo(text, ctx):
        reasons.append("regex:echo")
    if _check_prompt_leak(text, ctx.system_prompt):
        reasons.append("regex:prompt_leak")
    if patterns.model_talk(text) is not None:
        reasons.append("regex:model_talk")
    if _check_latin_run(text, whitelist):
        reasons.append("regex:latin")

    return reasons


# --------------------------------------------------------------------------- #
# Слой 2 — dedup:* / style:*
# --------------------------------------------------------------------------- #


def _check_dedup_jaccard(text: str, recent_replies: list[str]) -> bool:
    words = _normalize_words(text)
    normalized = " ".join(words)
    if not normalized:
        return False

    if len(words) < 3:
        return any(" ".join(_normalize_words(reply)) == normalized for reply in recent_replies)

    shingles = _ngrams(words, 3)
    for reply in recent_replies:
        reply_shingles = _ngrams(_normalize_words(reply), 3)
        if _jaccard(shingles, reply_shingles) >= 0.6:
            return True
    return False


def _check_polish_freq(text: str, ctx: FilterContext) -> bool:
    whitelist = _places_whitelist(ctx)
    text_latin = {m.group(0).casefold() for m in _LATIN_TOKEN_2PLUS_RE.finditer(text)} - whitelist
    if not text_latin:
        return False

    last4 = ctx.recent_replies[-4:]
    last4_latin_sets = [
        {m.group(0).casefold() for m in _LATIN_TOKEN_2PLUS_RE.finditer(reply)} - whitelist
        for reply in last4
    ]

    if any(text_latin & reply_latin for reply_latin in last4_latin_sets):
        return True
    # "раз в 5-6 реплик": хватает любого латинского токена в последних 4 репликах,
    # даже если он не совпадает буквально с токеном кандидата.
    return any(reply_latin for reply_latin in last4_latin_sets)


def _check_question_x2(text: str, recent_replies: list[str]) -> bool:
    if not text.rstrip().endswith("?"):
        return False
    if not recent_replies:
        return False
    return recent_replies[-1].rstrip().endswith("?")


# Стоп-слова для dedup:self_echo (CLAUDE.md, "Интерфейсы этапа 4", правки): 4-грамма,
# состоящая только из этих слов, слишком общая, чтобы считаться самоповтором.
_SELF_ECHO_STOPWORDS = frozenset(
    {
        "я",
        "и",
        "в",
        "на",
        "не",
        "что",
        "это",
        "у",
        "меня",
        "тебя",
        "ты",
        "а",
        "но",
        "да",
        "нет",
        "же",
        "бы",
        "как",
        "так",
        "то",
        "все",
        "всё",
    }
)

_SELF_ECHO_RECENT = 20


def _check_self_echo(text: str, ctx: FilterContext) -> bool:
    """dedup:self_echo — кандидат пересказывает свою же недавнюю байку.

    Нормализованная 4-грамма кандидата (без грамм из одних стоп-слов) сверяется
    со словами последних ``_SELF_ECHO_RECENT`` ``ctx.recent_replies``: если все
    четыре слова граммы встречаются среди слов одной и той же реплики — срез.
    Сравнение по множеству слов, а не по дословному фрагменту (в отличие от
    ``regex:echo``) — цель поймать пересказ той же байки другими словами/порядком,
    не только буквальное повторение.
    """
    grams = _ngrams(_normalize_words(text), 4)
    grams = {gram for gram in grams if set(gram) - _SELF_ECHO_STOPWORDS}
    if not grams:
        return False
    for reply in ctx.recent_replies[-_SELF_ECHO_RECENT:]:
        reply_words = set(_normalize_words(reply))
        if any(set(gram) <= reply_words for gram in grams):
            return True
    return False


def _check_emoji_count(text: str, allowed: frozenset[str], max_per_reply: int) -> bool:
    """style:emoji_count — разрешённых эмодзи в реплике больше emoji_max_per_reply."""
    count = sum(1 for ch, _ in _emoji_tokens(text) if ch in allowed)
    return count > max_per_reply


def _check_emoji_freq(text: str, recent_replies: list[str], window: int) -> bool:
    """style:emoji_freq — в тексте есть эмодзи И хотя бы в одной из последних
    ``window`` recent_replies тоже было эмодзи (правило «не чаще раза из пяти»
    держит выходной фильтр, не промпт, см. CHARACTER.md раздел 3)."""
    if window <= 0 or not _has_emoji(text):
        return False
    return any(_has_emoji(reply) for reply in recent_replies[-window:])


def _check_emoji_position(text: str) -> bool:
    """style:emoji_position — после последнего эмодзи в тексте остаётся что-то,
    кроме пробелов и точек (допускаются "Бывает 🙂" и "Бывает. 💩", но не "🙂 Бывает")."""
    tokens = _emoji_tokens(text)
    if not tokens:
        return False
    _, last_end = tokens[-1]
    remainder = text[last_end:].replace(" ", "").replace(".", "")
    return bool(remainder)


def layer_rules(text: str, ctx: FilterContext, patterns: Patterns) -> list[str]:
    """Слой 2: детерминированные правила, 0 мс. ``patterns`` — см. ``layer_regex``."""
    reasons: list[str] = []

    if _check_dedup_jaccard(text, ctx.recent_replies):
        reasons.append("dedup:jaccard")
    if _check_polish_freq(text, ctx):
        reasons.append("dedup:polish_freq")
    if _check_self_echo(text, ctx):
        reasons.append("dedup:self_echo")

    if patterns.assistant_marker(text) is not None:
        reasons.append("style:assistant")
    if patterns.grumpy(text) is not None:
        reasons.append("style:grumpy")
    if _check_question_x2(text, ctx.recent_replies):
        reasons.append("style:question_x2")
    if text.count("!") > 1:
        reasons.append("style:exclaim")

    allowed_emoji = _allowed_emoji_set(ctx)
    if _check_emoji_count(text, allowed_emoji, ctx.cfg.filters.emoji_max_per_reply):
        reasons.append("style:emoji_count")
    if _check_emoji_freq(text, ctx.recent_replies, ctx.cfg.filters.emoji_recent_window):
        reasons.append("style:emoji_freq")
    if _check_emoji_position(text):
        reasons.append("style:emoji_position")

    return reasons


# --------------------------------------------------------------------------- #
# Контракт: FilterContext / FilterVerdict / check_output
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FilterContext:
    cfg: Config
    recent_replies: list[str]  # последние 50 реплик бота, хронологически
    context_rows: list[MessageRow]  # свежий контекст (context_window)
    places_names: list[str]  # названия из places (этап 5; пока пусто)
    participant_names: list[str] = field(default_factory=list)  # display_name авторов context_rows
    bot_names: list[str] = field(default_factory=list)  # persona.name, display_name, name_triggers
    muted_names: list[str] = field(default_factory=list)  # display_name замьюченных участников
    # Готовый Patterns от вызывающего; None -> собрать из cfg (медленно, только для тестов).
    patterns: PatternsLike | None = None
    system_prompt: str = ""  # тело промпта для проверки утечки
    trigger_text: str = ""  # сообщение-триггер ("" для ambient/spontaneous/morning)
    now: int = 0


@dataclass(frozen=True, slots=True)
class FilterVerdict:
    ok: bool
    reason: str  # первая причина среза или "pass"
    reasons: tuple[str, ...] = ()  # ВСЕ сработавшие причины (для shadow-статистики)


class _JudgeLike(Protocol):
    """Узкий протокол вместо ``judge.Judge`` — этот модуль его не импортирует."""

    async def check(self, *, candidate: str, trigger_text: str, now: int) -> list[str]: ...


async def check_output(
    text: str, ctx: FilterContext, judge: _JudgeLike | None = None
) -> FilterVerdict:
    """Собирает вердикт из трёх слоёв. Слой 3 вызывается только если слои 1-2

    прошли (нечего резать) или включён shadow mode (считаем все слои для
    статистики, но не режем без shadow-исключения из responder.py).
    """
    patterns = _patterns_for(ctx)
    reasons = layer_regex(text, ctx, patterns) + layer_rules(text, ctx, patterns)

    if judge is not None and (not reasons or ctx.cfg.filters.shadow):
        judge_reasons = await judge.check(
            candidate=text, trigger_text=ctx.trigger_text, now=ctx.now
        )
        reasons = reasons + judge_reasons

    ok = not reasons
    reason = reasons[0] if reasons else "pass"
    return FilterVerdict(ok=ok, reason=reason, reasons=tuple(reasons))
