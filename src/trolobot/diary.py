"""Дневник дня (CLAUDE.md, "дневник дня").

Повод — живой случай 09.10.2026: через ``/say`` Фёдор сказал «я за грибами», а через
семь минут — «а я под машину», и чат поймал. Реплика лежала в «Твоих последних
репликах», но модель не держит её как факт. ``DiaryExtractor`` — дешёвый вызов LLM
(тот же приём, что ``followup.FollowupChecker``): из итогового текста реплики достаёт
факт о его собственных делах, а ``render_diary`` собирает из фактов дня абзац для
слота ``{diary}`` системного промпта: «держись этого, не противоречь».

Модуль не ходит в БД: запись факта делает вызывающий (``responder.py``).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Sequence

from trolobot.config_models import Config
from trolobot.db import SelfFactRow
from trolobot.llm import LLMClient, LLMError
from trolobot.sanitize import normalize_text
from trolobot.timeutil import local_dt

logger = logging.getLogger(__name__)

_COUNTER_KEY = "diary_calls"
_FACT_MAX_LEN = 80

_LT_RUN_RE = re.compile(r"<{3,}")
_GT_RUN_RE = re.compile(r">{3,}")
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL | re.IGNORECASE)
_SLOT_RE = re.compile(r"\{(reply)\}")

_TODAY_HEADER = "Что ты сегодня уже говорил о себе (держись этого, не противоречь): "
_WEEK_HEADER = "На этой неделе: "
_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def _strip_fake_delimiters(text: str) -> str:
    return _GT_RUN_RE.sub(" ", _LT_RUN_RE.sub(" ", text))


def _strip_code_fence(text: str) -> str:
    match = _CODE_FENCE_RE.match(text)
    if match is not None:
        return match.group(1).strip()
    return text


def _parse_fact(raw: str) -> str | None:
    """``{"fact": "..."|null}`` -> нормализованный факт или None.

    Разбор такой же терпимый, как ``judge._parse_verdict``: срез ```json``` обёрток,
    первая "{" и ``raw_decode``. Не строка, пустая строка и длиннее 80 символов
    (модель пересказала, а не сжала) — None."""
    try:
        candidate = _strip_code_fence(raw.strip())
        start = candidate.find("{")
        if start == -1:
            return None
        data, _end = json.JSONDecoder().raw_decode(candidate, start)
        if not isinstance(data, dict):
            return None
        value = data.get("fact")
        if not isinstance(value, str):
            return None
        fact = normalize_text(_strip_fake_delimiters(value))
        if not fact or len(fact) > _FACT_MAX_LEN:
            return None
        return fact
    except Exception:
        # Ответ модели — недоверенный внешний текст: любой сбой разбора значит
        # «факта нет», а не падение.
        return None


class DiaryExtractor:
    def __init__(
        self, llm: LLMClient, cfg_getter: Callable[[], Config], prompt_template: str
    ) -> None:
        self._llm = llm
        self._cfg_getter = cfg_getter
        self._prompt_template = prompt_template

    async def extract(self, reply_text: str, *, now: int) -> str | None:
        """Факт о делах персонажа из его реплики или None. Любой сбой (пустая модель,
        ``LLMError``, не JSON, слишком длинный факт) — None, ответ в чат от этого
        не зависит."""
        cfg = self._cfg_getter()
        diary_cfg = cfg.behaviour.diary
        model = diary_cfg.model or cfg.llm.judge_model
        if not model:
            return None

        slot_values = {"reply": _strip_fake_delimiters(reply_text).strip()}
        system = _SLOT_RE.sub(lambda m: slot_values[m.group(1)], self._prompt_template)
        try:
            result = await self._llm.call(
                [{"role": "system", "content": system}],
                model=model,
                max_tokens=diary_cfg.max_tokens,
                now=now,
                counter_key=_COUNTER_KEY,
                calls_cap=diary_cfg.daily_cap,
            )
        except LLMError as exc:
            logger.warning("diary llm error: reason=%s", exc.reason)
            return None

        fact = _parse_fact(result.text)
        if fact is None:
            logger.info("diary: факта в реплике нет")
            return None
        logger.info("diary: %s", fact)
        return fact


def _clean(text: str) -> str:
    return _strip_fake_delimiters(text).strip().rstrip(".;")


def render_diary(today: Sequence[SelfFactRow], week: Sequence[SelfFactRow], tz: str) -> str:
    """Блок слота ``{diary}``. Пусто -> "" (абзац со слотом в system.txt пропадает).

    Сегодня — «10:46 пошёл за грибами; 12:39 возится с машиной.», неделя — «пн чинил
    движок; ср купил полки.» (день недели и время локальные по tz). Строки не
    начинаются с "- " (``filters.regex:prompt_leak``)."""
    lines: list[str] = []
    if today:
        parts = [f"{local_dt(row.created_at, tz):%H:%M} {_clean(row.text)}" for row in today]
        lines.append(_TODAY_HEADER + "; ".join(parts) + ".")
    if week:
        parts = [
            f"{_WEEKDAYS[local_dt(row.created_at, tz).weekday()]} {_clean(row.text)}"
            for row in week
        ]
        lines.append(_WEEK_HEADER + "; ".join(parts) + ".")
    return "\n".join(lines)
