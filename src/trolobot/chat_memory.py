"""Долгая память чата (CLAUDE.md, "долгая память чата").

Сообщения живут ``behaviour.message_retention_days`` (по умолчанию 30 суток), а
персонаж должен помнить, о чём говорили месяцы назад. Раз в неделю (шаг —
``chat_memory.period_days``) один вызов модели сжимает прошедший период в
несколько строк; пересказ лежит в таблице ``chat_memory`` дольше самих сообщений
(``keep_days``) и подмешивается в системный промпт слотом ``{chat_memory}``.

Это осознанное решение владельца по приватности: пересказ чужих разговоров
хранится дольше самих разговоров. Компенсация — сообщения замьюченных участников
в пересказ не попадают вовсе (``exclude_user_ids`` в ``messages_between``).

Границы периодов — всегда локальная полночь ``persona.timezone``, и шаг
считается в календарных сутках, а не в ``period_days * 86400``: при переходе на
летнее/зимнее время сутки длятся 23 или 25 часов, и арифметика по секундам
увела бы границу периода с полуночи на 23:00/01:00, а дальше — всё дальше.
``_add_days`` пересчитывает через локальную дату и потому от DST не зависит.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from trolobot.config_models import Config
from trolobot.db import ChatMemoryRow, Database
from trolobot.llm import LLMClient, LLMError
from trolobot.sanitize import normalize_text
from trolobot.timeutil import in_window, local_date

logger = logging.getLogger(__name__)

# Меньше этого числа строк за период — пересказывать нечего, модель не зовём.
_MIN_LINES = 5

# Цикл job() по образцу retention.retention_loop: раз в час проверить, не пора ли.
_JOB_INTERVAL_SEC = 3600
# После упавшей итерации — короткая пауза вместо обычной, чтобы не крутить busy-loop
# и при этом не ждать целый час (тот же приём, что в responder.checkin_job).
_ERROR_RETRY_SEC = 60

_HEADER = "Что было в чате раньше, по неделям (твои воспоминания; упоминай только к слову):"

# Данные людей уходят внутри <<<CHAT ... >>>, поддельные разделители из них
# вырезаются — тот же приём, что в prompt.py/judge.py/followup.py.
_LT_RUN_RE = re.compile(r"<{3,}")
_GT_RUN_RE = re.compile(r">{3,}")

_SLOT_RE = re.compile(r"\{(period|chat|limit)\}")

# Шутки и истории людей (CLAUDE.md, "шутки чата и истории людей"): потолки длины
# из контракта, токены ответа — шутки из контракта, истории — с запасом на JSON.
_JOKE_MAX_LEN = 80
_JOKES_MAX_TOKENS = 200
_THREAD_MAX_LEN = 120
_THREADS_MAX_TOKENS = 300
_THREADS_MAX_PER_PERIOD = 5
_EXISTING_JOKES_LOOKUP = 200
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL | re.IGNORECASE)

# Ведущие маркеры списка и нумерация, которые модель любит дописывать вопреки
# инструкции. Срезаются: строка пересказа, начинающаяся с "- ", попала бы в
# системный промпт буллетом и filters.regex:prompt_leak считал бы её инструкцией.
_BULLET_RE = re.compile(r"^\s*(?:[-*•–—]+\s+|\d+[.)]\s+)")
# Строка, в которой модель заговорила про формат ответа, а не про чат.
_JSON_RE = re.compile(r"json", re.IGNORECASE)


def _strip_fake_delimiters(text: str) -> str:
    return _GT_RUN_RE.sub(" ", _LT_RUN_RE.sub(" ", text))


def _parse_json_object(raw: str) -> dict[str, object] | None:
    """Терпимый разбор ответа модели (как ``judge._parse_verdict``): срез ```json```
    обёрток, первая "{" и ``raw_decode``. Любой сбой -> None."""
    try:
        candidate = raw.strip()
        match = _CODE_FENCE_RE.match(candidate)
        if match is not None:
            candidate = match.group(1).strip()
        start = candidate.find("{")
        if start == -1:
            return None
        data, _end = json.JSONDecoder().raw_decode(candidate, start)
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        # Ответ модели — недоверенный внешний текст: любой сбой разбора значит
        # «ничего не нашли», а не падение.
        return None


def _clean_note(raw: object, max_len: int) -> str | None:
    """Строка из ответа модели -> заметка для БД/промпта: без разделителей, буллета,
    переносов; пусто или длиннее ``max_len`` (модель пересказала, а не сжала) -> None."""
    if not isinstance(raw, str):
        return None
    cleaned = normalize_text(_BULLET_RE.sub("", _strip_fake_delimiters(raw))).strip()
    if not cleaned or len(cleaned) > max_len:
        return None
    return cleaned


def _local_midnight(ts: int, tz: str) -> int:
    """Начало локальных суток, в которые попадает ``ts``."""
    zone = ZoneInfo(tz)
    day = local_date(ts, tz)
    return int(datetime(day.year, day.month, day.day, tzinfo=zone).timestamp())


def _add_days(ts: int, days: int, tz: str) -> int:
    """``ts`` (локальная полночь) плюс ``days`` календарных суток — снова полночь.

    Именно календарных: в сутки перехода на летнее время 23 часа, на зимнее — 25,
    и прибавление ``days * 86400`` сдвинуло бы границу периода с полуночи.
    """
    zone = ZoneInfo(tz)
    day = local_date(ts, tz) + timedelta(days=days)
    return int(datetime(day.year, day.month, day.day, tzinfo=zone).timestamp())


def format_period(period_start: int, period_end: int, tz: str) -> str:
    """«08.09–14.09.2026»: конец периода исключительный, показываем последние сутки.

    Публичная: тем же форматом периода подписывает пересказы ``/memory list``
    в commands.py."""
    start_day = local_date(period_start, tz)
    # period_end исключителен и приходится на полночь следующих суток — последний
    # день периода это period_end минус секунда.
    end_day = local_date(max(period_end - 1, period_start), tz)
    return f"{start_day.strftime('%d.%m')}–{end_day.strftime('%d.%m.%Y')}"


def render_chat_memory(rows: Sequence[ChatMemoryRow], tz: str) -> str:
    """Блок долгой памяти для системного промпта. Пусто -> "" (весь абзац исчезает).

    Строки пересказа идут с отступом в два пробела и НИКОГДА не начинаются с
    «- »: ``filters.regex:prompt_leak`` считает инструктивной частью промпта
    именно строки-буллеты, и воспоминание, начинающееся с дефиса, срезало бы
    ответ персонажа как утечку промпта.
    """
    if not rows:
        return ""
    lines = [_HEADER]
    for row in rows:
        lines.append(f"{format_period(row.period_start, row.period_end, tz)}:")
        for line in row.text.splitlines():
            cleaned = line.strip()
            if cleaned:
                lines.append(f"  {cleaned}")
    return "\n".join(lines)


def _clean_summary(raw: str, max_chars: int) -> str:
    """Ответ модели -> тело пересказа: по строке на воспоминание, без мусора.

    Пустые строки выбрасываются, ведущие буллеты и нумерация срезаются (см.
    ``render_chat_memory``), строки, в которых модель заговорила про JSON вместо
    чата, выбрасываются целиком. Обрезка до ``max_chars`` — по границе строки:
    оборванная на полуслове фраза в промпте выглядит хуже, чем на одну меньше.
    """
    kept: list[str] = []
    total = 0
    for line in raw.splitlines():
        cleaned = normalize_text(_BULLET_RE.sub("", line))
        if not cleaned or _JSON_RE.search(cleaned):
            continue
        extra = len(cleaned) + (1 if kept else 0)
        if total + extra > max_chars:
            break
        kept.append(cleaned)
        total += extra
    return "\n".join(kept)


class ChatMemorizer:
    def __init__(
        self,
        llm: LLMClient,
        db: Database,
        cfg_getter: Callable[[], Config],
        prompt_template: str,
        chat_id: int,
        clock: Callable[[], int] = lambda: int(time.time()),
        interval_sec: int = _JOB_INTERVAL_SEC,
        jokes_prompt: str = "",
        threads_prompt: str = "",
    ) -> None:
        self._llm = llm
        self._db = db
        self._cfg_getter = cfg_getter
        self._prompt_template = prompt_template
        self._chat_id = chat_id
        self._clock = clock
        self._interval_sec = interval_sec
        self._jokes_prompt = jokes_prompt
        self._threads_prompt = threads_prompt

    async def summarize_period(self, start: int, end: int, *, now: int) -> ChatMemoryRow | None:
        """Один вызов модели на период ``[start, end)``; строка в БД или None.

        None (без вызова модели) — если за период набралось меньше ``_MIN_LINES``
        строк: пересказывать нечего, а вызов стоит денег. None (после вызова) —
        если модель ответила пустотой или упала: пропуск периода лучше, чем
        мусорная строка в системном промпте навсегда.
        """
        cfg = self._cfg_getter()
        memory_cfg = cfg.behaviour.chat_memory
        model = memory_cfg.model or cfg.llm.main_model
        if not model:
            logger.warning("chat memory: модель не задана, пересказ пропущен")
            return None

        muted = await self._db.muted_user_ids()
        messages = await self._db.messages_between(
            self._chat_id, start, end, exclude_user_ids=muted
        )
        bot_replies = await self._db.bot_replies_between(start, end)

        bot_name = cfg.persona.name
        lines: list[tuple[int, str]] = [
            (row.created_at or 0, f"{row.display_name}: {row.text}") for row in messages if row.text
        ]
        lines.extend((row.created_at, f"{bot_name}: {row.text}") for row in bot_replies if row.text)
        lines.sort(key=lambda item: item[0])
        tail = [line for _ts, line in lines[-memory_cfg.max_messages :]]
        if len(tail) < _MIN_LINES:
            logger.info(
                "chat memory: период %s пропущен, строк %d",
                format_period(start, end, cfg.persona.timezone),
                len(tail),
            )
            return None

        chat_block = _strip_fake_delimiters("\n".join(tail))
        slot_values = {
            "period": format_period(start, end, cfg.persona.timezone),
            "chat": chat_block,
        }
        prompt = _SLOT_RE.sub(lambda m: slot_values[m.group(1)], self._prompt_template)

        try:
            result = await self._llm.call(
                [{"role": "system", "content": prompt}],
                model=model,
                max_tokens=memory_cfg.max_tokens,
                now=now,
            )
        except LLMError as exc:
            logger.warning("chat memory llm error: reason=%s", exc.reason)
            return None

        text = _clean_summary(result.text, memory_cfg.max_chars)
        if not text:
            logger.warning(
                "chat memory: пустой пересказ периода %s",
                format_period(start, end, cfg.persona.timezone),
            )
            return None

        memory_id = await self._db.insert_chat_memory(
            period_start=start, period_end=end, text=text, created_at=now
        )
        logger.info(
            "chat memory: период %s пересказан, строк %d, символов %d",
            format_period(start, end, cfg.persona.timezone),
            len(tail),
            len(text),
        )
        # Два дополнительных вызова по тем же строкам периода. Пересказ уже сохранён,
        # поэтому любой их сбой (кроме отмены) пересказ не ломает.
        try:
            await self._extract_jokes(cfg, model, start, end, chat_block, now)
            await self._extract_threads(cfg, model, start, end, chat_block, muted, now)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("chat memory: извлечение шуток/историй упало")
        return ChatMemoryRow(
            id=memory_id, period_start=start, period_end=end, text=text, created_at=now
        )

    async def _call_extra(
        self,
        template: str,
        *,
        model: str,
        max_tokens: int,
        period: str,
        chat_block: str,
        limit: int,
        now: int,
        what: str,
    ) -> dict[str, object] | None:
        slot_values = {"period": period, "chat": chat_block, "limit": str(limit)}
        prompt = _SLOT_RE.sub(lambda m: slot_values[m.group(1)], template)
        try:
            result = await self._llm.call(
                [{"role": "system", "content": prompt}],
                model=model,
                max_tokens=max_tokens,
                now=now,
            )
        except LLMError as exc:
            logger.warning("chat memory %s llm error: reason=%s", what, exc.reason)
            return None
        data = _parse_json_object(result.text)
        if data is None:
            logger.warning("chat memory %s: ответ модели не JSON", what)
        return data

    async def _extract_jokes(
        self, cfg: Config, model: str, start: int, end: int, chat_block: str, now: int
    ) -> None:
        jokes_cfg = cfg.behaviour.jokes
        if not (jokes_cfg.enabled and self._jokes_prompt and jokes_cfg.per_period > 0):
            return
        data = await self._call_extra(
            self._jokes_prompt,
            model=model,
            max_tokens=_JOKES_MAX_TOKENS,
            period=format_period(start, end, cfg.persona.timezone),
            chat_block=chat_block,
            limit=jokes_cfg.per_period,
            now=now,
            what="jokes",
        )
        raw_items = data.get("jokes") if data is not None else None
        if not isinstance(raw_items, list):
            return
        known = {row.text.casefold() for row in await self._db.jokes(_EXISTING_JOKES_LOOKUP)}
        saved = 0
        for raw in raw_items:
            joke = _clean_note(raw, _JOKE_MAX_LEN)
            if joke is None or joke.casefold() in known:
                continue
            known.add(joke.casefold())
            await self._db.insert_joke(text=joke, created_at=now)
            saved += 1
            if saved >= jokes_cfg.per_period:
                break
        logger.info("chat memory: шуток сохранено %d", saved)

    async def _extract_threads(
        self,
        cfg: Config,
        model: str,
        start: int,
        end: int,
        chat_block: str,
        muted: frozenset[int],
        now: int,
    ) -> None:
        if not (cfg.behaviour.callback.enabled and self._threads_prompt):
            return
        data = await self._call_extra(
            self._threads_prompt,
            model=model,
            max_tokens=_THREADS_MAX_TOKENS,
            period=format_period(start, end, cfg.persona.timezone),
            chat_block=chat_block,
            limit=_THREADS_MAX_PER_PERIOD,
            now=now,
            what="threads",
        )
        raw_items = data.get("threads") if data is not None else None
        if not isinstance(raw_items, list):
            return
        bot_names = {
            name.casefold() for name in (cfg.persona.name, cfg.persona.display_name) if name
        }
        saved = 0
        for item in raw_items[:_THREADS_MAX_PER_PERIOD]:
            if not isinstance(item, dict):
                continue
            name = _clean_note(item.get("name"), _JOKE_MAX_LEN)
            text = _clean_note(item.get("text"), _THREAD_MAX_LEN)
            if name is None or text is None or name.casefold() in bot_names:
                continue
            # Только точное совпадение display_name среди сообщений периода: имя из
            # ответа модели — не повод привязать историю к чужому человеку.
            user_id = await self._db.user_id_by_display_name(self._chat_id, name, start)
            if user_id is None or user_id in muted:
                continue
            await self._db.insert_people_thread(
                user_id=user_id, display_name=name, text=text, created_at=now
            )
            saved += 1
        logger.info("chat memory: историй сохранено %d", saved)

    async def run_due(self, *, now: int) -> list[ChatMemoryRow]:
        """Пересказывает все периоды, целиком уместившиеся до начала текущих суток.

        Период всегда кончается на границе локальных суток, поэтому «сегодня»
        не пересказывается никогда — только завершённые сутки. Пересказов ещё
        нет -> догоняем ``backfill_periods`` периодов назад, но не раньше самого
        первого сообщения чата. Повторный запуск в том же окне ничего не делает:
        ``last_chat_memory_end`` уже равен концу последнего периода.
        """
        cfg = self._cfg_getter()
        memory_cfg = cfg.behaviour.chat_memory
        tz = cfg.persona.timezone
        period_days = memory_cfg.period_days

        end = _local_midnight(now, tz)
        start = await self._db.last_chat_memory_end()
        if start is None:
            first_at = await self._db.first_message_at(self._chat_id)
            if first_at is None:
                return []
            start = max(
                _add_days(end, -period_days * memory_cfg.backfill_periods, tz),
                _local_midnight(first_at, tz),
            )

        created: list[ChatMemoryRow] = []
        while True:
            period_end = _add_days(start, period_days, tz)
            if period_end > end:
                break
            row = await self.summarize_period(start, period_end, now=now)
            if row is not None:
                created.append(row)
            start = period_end
        return created

    async def job(self) -> None:
        """Бесконечный цикл по образцу ``retention.retention_loop``: раз в час
        смотрит, попадает ли текущий момент в ``chat_memory.run_window``, и если
        да — зовёт ``run_due``. Второй заход внутри того же окна безопасен:
        ``run_due`` уже ничего не найдёт. Отмена пробрасывается, любая другая
        ошибка логируется и цикл живёт дальше."""
        while True:
            try:
                cfg = self._cfg_getter()
                memory_cfg = cfg.behaviour.chat_memory
                now = self._clock()
                if memory_cfg.enabled and in_window(
                    now, cfg.persona.timezone, memory_cfg.run_window
                ):
                    await self.run_due(now=now)
            except asyncio.CancelledError:
                logger.info("chat memory job: cancelled, stopping")
                raise
            except Exception:
                logger.exception("chat memory job: iteration failed")
                await asyncio.sleep(_ERROR_RETRY_SEC)
                continue
            await asyncio.sleep(self._interval_sec)
