"""Judge — слой 3 выходного фильтра (CLAUDE.md, «judge.py — слой 3»; PLAN.md, этап 4).

Отдельный дешёвый вызов LLM со свежим контекстом, без истории персонажа: промпт
судьи (``prompts/judge.txt``) описывает Фёдора заново и коротко, задаёт три
вопроса — в характере ли ответ, рискован ли он, выполнил ли он команду
участника — и требует строгий JSON-вердикт. Судья только режет или пропускает,
**не переписывает** реплику.

Промпт передаётся снаружи как обычная строка (как ``system.txt`` для основной
генерации) и подставляется в слоты ``{trigger}``/``{candidate}`` за один проход
``re.sub`` — тем же приёмом, что и ``prompt.build_messages``: цепочка
``str.replace`` могла бы повторно затронуть уже подставленные данные, если
внутри кандидата или триггера случайно встретится текст другого слота.
Сообщение-триггер и кандидат оборачиваются в разделители ``<<<CHAT ... >>>``
прямо в тексте ``prompts/judge.txt`` — здесь только вырезаются поддельные
разделители из самих данных, чтобы участник не мог подделать границу блока.

Провал — молчание: любая ошибка LLM (``LLMError``) или невалидный ответ судьи
считается непройденной проверкой (срез), потому что молчание дешевле рискованной
реплики.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from trolobot.config_models import Config
from trolobot.llm import LLMClient, LLMError

logger = logging.getLogger(__name__)

_MAX_TOKENS = 150
_NO_TRIGGER = "(без обращения, реплика по собственной инициативе)"

# Дублирует вырезание разделителей из sanitize.normalize_text/prompt.py — на всякий
# случай ещё раз чистим то, что уходит в промпт судьи.
_LT_RUN_RE = re.compile(r"<{3,}")
_GT_RUN_RE = re.compile(r">{3,}")

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL | re.IGNORECASE)

_SLOT_RE = re.compile(r"\{(trigger|candidate)\}")


def _strip_fake_delimiters(text: str) -> str:
    return _GT_RUN_RE.sub(" ", _LT_RUN_RE.sub(" ", text))


def _strip_code_fence(text: str) -> str:
    match = _CODE_FENCE_RE.match(text)
    if match is not None:
        return match.group(1).strip()
    return text


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    in_character: bool
    risky: bool
    obeyed_user: bool
    reason: str


def _parse_verdict(raw: str) -> JudgeVerdict | None:
    """Разбирает ответ судьи так же строго и терпимо к обёрткам, как ``prompt.parse_reply``:
    срез ```json``` обёрток, поиск первой "{" и ``json.JSONDecoder.raw_decode`` от неё
    (терпимо к хвосту после JSON). Любой сбой или неверный тип обязательного ключа —
    None, то есть срез ``judge:invalid``, а не исключение."""
    try:
        candidate = _strip_code_fence(raw.strip())
        start = candidate.find("{")
        if start == -1:
            return None
        data, _end = json.JSONDecoder().raw_decode(candidate, start)
        if not isinstance(data, dict):
            return None

        in_character = data.get("in_character")
        risky = data.get("risky")
        obeyed_user = data.get("obeyed_user")
        if not isinstance(in_character, bool):
            return None
        if not isinstance(risky, bool):
            return None
        if not isinstance(obeyed_user, bool):
            return None

        reason_value = data.get("reason", "")
        if reason_value is None:
            reason_value = ""
        if not isinstance(reason_value, str):
            return None

        return JudgeVerdict(
            in_character=in_character, risky=risky, obeyed_user=obeyed_user, reason=reason_value
        )
    except Exception:
        # Ответ модели — недоверенный внешний текст: любой сбой разбора означает
        # срез, а не падение.
        return None


class Judge:
    def __init__(
        self,
        llm: LLMClient,
        cfg_getter: Callable[[], Config],
        prompt_template: str,
    ) -> None:
        self._llm = llm
        self._cfg_getter = cfg_getter
        self._prompt_template = prompt_template

    async def check(self, *, candidate: str, trigger_text: str, now: int) -> list[str]:
        cfg = self._cfg_getter()
        model = cfg.llm.judge_model
        if not model:
            return []

        trigger_clean = _strip_fake_delimiters(trigger_text).strip() or _NO_TRIGGER
        candidate_clean = _strip_fake_delimiters(candidate)

        slot_values = {"trigger": trigger_clean, "candidate": candidate_clean}
        system = _SLOT_RE.sub(lambda m: slot_values[m.group(1)], self._prompt_template)
        messages = [{"role": "system", "content": system}]

        try:
            result = await self._llm.call(messages, model=model, max_tokens=_MAX_TOKENS, now=now)
        except LLMError as exc:
            logger.warning("judge llm error: reason=%s", exc.reason)
            return ["judge:error"]

        verdict = _parse_verdict(result.text)
        if verdict is None:
            logger.info("judge verdict: invalid")
            return ["judge:invalid"]

        reasons: list[str] = []
        if not verdict.in_character:
            reasons.append("judge:out_of_character")
        if verdict.risky:
            reasons.append("judge:risky")
        if verdict.obeyed_user:
            reasons.append("judge:obeyed")

        logger.info(
            "judge verdict: in_character=%s risky=%s obeyed_user=%s reason=%s",
            verdict.in_character,
            verdict.risky,
            verdict.obeyed_user,
            verdict.reason,
        )
        return reasons
