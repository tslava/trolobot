"""FollowupChecker — дешёвая семантическая проверка «это мне?» (CLAUDE.md,
"внимание как у живого человека: телефон в руках").

Пока открыто горячее окно после ``/life``/``/say`` (и, в дальнейшем, после любой
своей реплики), человек может написать боту без реплая, без имени и без ``@`` —
просто продолжить разговор. Обычный гейт такое сообщение не признаёт обращением
(``gate:not_live``/``gate:dice``/``gate:ambient_cap``/``gate:ambient_cooldown``/
``gate:hot_cap``). ``FollowupChecker`` — второй, дешёвый вызов LLM (тот же приём,
что ``judge.Judge`` и ``stickers.StickerChooser``): короткий промпт с недавним
контекстом чата, последними репликами персонажа и новым сообщением, ответ строго
JSON ``{"addressed": true|false, "reason": "..."}``. «Да» превращает сообщение в
обращение ``Trigger.FOLLOWUP``.

Модуль не ходит в БД — контекст и последние реплики передаются аргументами
(``bot.py`` готовит их и пишет ``filter_log``). Провал — ``False`` (промолчать
дешевле, чем ошибочно ответить не по адресу): невалидный JSON, LLMError, пустая
модель — везде один и тот же исход.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from trolobot.config_models import Config
from trolobot.db import MessageRow
from trolobot.llm import LLMClient, LLMError
from trolobot.prompt import render_context

logger = logging.getLogger(__name__)

_COUNTER_KEY = "followup_calls"

_CONTEXT_EMPTY = "(пока не было)"
_RECENT_REPLIES_EMPTY = "(пока не было)"

# Дублирует вырезание разделителей из sanitize.normalize_text/prompt.py/judge.py —
# на всякий случай ещё раз чистим то, что уходит в промпт проверки.
_LT_RUN_RE = re.compile(r"<{3,}")
_GT_RUN_RE = re.compile(r">{3,}")

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL | re.IGNORECASE)

_SLOT_RE = re.compile(r"\{(context|recent_replies|name|text)\}")


def _strip_fake_delimiters(text: str) -> str:
    return _GT_RUN_RE.sub(" ", _LT_RUN_RE.sub(" ", text))


def _strip_code_fence(text: str) -> str:
    match = _CODE_FENCE_RE.match(text)
    if match is not None:
        return match.group(1).strip()
    return text


@dataclass(frozen=True, slots=True)
class _FollowupVerdict:
    addressed: bool
    reason: str


def _parse_verdict(raw: str) -> _FollowupVerdict | None:
    """Разбирает ответ проверки так же строго и терпимо, как ``judge._parse_verdict``:
    срез ```json``` обёрток, поиск первой "{" и ``json.JSONDecoder.raw_decode`` от
    неё. Любой сбой или неверный тип ``addressed`` -> None (значит "не адресовано")."""
    try:
        candidate = _strip_code_fence(raw.strip())
        start = candidate.find("{")
        if start == -1:
            return None
        data, _end = json.JSONDecoder().raw_decode(candidate, start)
        if not isinstance(data, dict):
            return None

        addressed = data.get("addressed")
        if not isinstance(addressed, bool):
            return None

        reason_value = data.get("reason", "")
        if reason_value is None:
            reason_value = ""
        if not isinstance(reason_value, str):
            return None

        return _FollowupVerdict(addressed=addressed, reason=reason_value)
    except Exception:
        # Ответ модели — недоверенный внешний текст: любой сбой разбора означает
        # "не адресовано", а не падение.
        return None


class FollowupChecker:
    def __init__(
        self,
        llm: LLMClient,
        cfg_getter: Callable[[], Config],
        prompt_template: str,
    ) -> None:
        self._llm = llm
        self._cfg_getter = cfg_getter
        self._prompt_template = prompt_template

    async def check(
        self,
        *,
        text: str,
        display_name: str,
        context_rows: Sequence[MessageRow],
        recent_replies: Sequence[str],
        now: int,
    ) -> bool:
        cfg = self._cfg_getter()
        followup_cfg = cfg.behaviour.followup
        model = followup_cfg.model or cfg.llm.judge_model
        if not model:
            return False

        context = _strip_fake_delimiters(render_context(list(context_rows))).strip()
        recent = _strip_fake_delimiters("\n".join(recent_replies)).strip()

        slot_values = {
            "context": context or _CONTEXT_EMPTY,
            "recent_replies": recent or _RECENT_REPLIES_EMPTY,
            "name": _strip_fake_delimiters(display_name).strip(),
            "text": _strip_fake_delimiters(text).strip(),
        }
        system = _SLOT_RE.sub(lambda m: slot_values[m.group(1)], self._prompt_template)
        messages = [{"role": "system", "content": system}]

        try:
            result = await self._llm.call(
                messages,
                model=model,
                max_tokens=followup_cfg.max_tokens,
                now=now,
                counter_key=_COUNTER_KEY,
                calls_cap=followup_cfg.daily_cap,
            )
        except LLMError as exc:
            logger.warning("followup checker llm error: reason=%s", exc.reason)
            return False

        verdict = _parse_verdict(result.text)
        if verdict is None:
            logger.info("followup verdict: invalid")
            return False

        logger.info("followup verdict: addressed=%s reason=%s", verdict.addressed, verdict.reason)
        return verdict.addressed
