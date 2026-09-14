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

_SLOT_RE = re.compile(r"\{(period|chat)\}")

# Ведущие маркеры списка и нумерация, которые модель любит дописывать вопреки
# инструкции. Срезаются: строка пересказа, начинающаяся с "- ", попала бы в
# системный промпт буллетом и filters.regex:prompt_leak считал бы её инструкцией.
_BULLET_RE = re.compile(r"^\s*(?:[-*•–—]+\s+|\d+[.)]\s+)")
# Строка, в которой модель заговорила про формат ответа, а не про чат.
_JSON_RE = re.compile(r"json", re.IGNORECASE)


def _strip_fake_delimiters(text: str) -> str:
    return _GT_RUN_RE.sub(" ", _LT_RUN_RE.sub(" ", text))


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
    ) -> None:
        self._llm = llm
        self._db = db
        self._cfg_getter = cfg_getter
        self._prompt_template = prompt_template
        self._chat_id = chat_id
        self._clock = clock
        self._interval_sec = interval_sec

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
        return ChatMemoryRow(
            id=memory_id, period_start=start, period_end=end, text=text, created_at=now
        )

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
