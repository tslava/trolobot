"""Локальное время бота. Всё хранится в unix seconds, сутки и окна считаются в persona.timezone."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def local_dt(ts: int, tz: str) -> datetime:
    return datetime.fromtimestamp(ts, ZoneInfo(tz))


def local_date(ts: int, tz: str) -> date:
    return local_dt(ts, tz).date()


def day_start(ts: int, tz: str) -> int:
    """Unix-время локальной полуночи тех суток, в которые попал ts.

    Нужен счётчикам «за сутки», которые считаются не по state-ключу с суффиксом даты
    (``day_key``), а прямо по таблицам (messages/bot_replies): потолок присутствия,
    CLAUDE.md, "меньше и разнообразнее".
    """
    zone = ZoneInfo(tz)
    return int(datetime.combine(local_date(ts, tz), time(0, 0), tzinfo=zone).timestamp())


def day_key(prefix: str, ts: int, tz: str) -> str:
    """Ключ state со суточным суффиксом: 'ambient_count:2026-09-10'."""
    return f"{prefix}:{local_date(ts, tz).isoformat()}"


def week_key(prefix: str, ts: int, tz: str) -> str:
    """Ключ state с недельным суффиксом по ISO: 'spontaneous_count:2026-W37'."""
    year, week, _ = local_date(ts, tz).isocalendar()
    return f"{prefix}:{year}-W{week:02d}"


def in_window(ts: int, tz: str, window: tuple[str, str]) -> bool:
    """Попадает ли момент в окно ["HH:MM", "HH:MM") по локальному времени.

    Начало включительно, конец исключительно. Окно через полночь ("23:00", "06:00")
    поддерживается. Пустое окно (start == end) — всегда False.
    """
    start, end = parse_hhmm(window[0]), parse_hhmm(window[1])
    now = local_dt(ts, tz).time().replace(tzinfo=None)
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end


def seconds_until(ts: int, tz: str, hhmm: str) -> int:
    """Секунд от ts до ближайшего наступления hhmm по локальному времени.

    Если hhmm сегодня уже прошло — до завтрашнего. Ровно сейчас — 0.
    """
    zone = ZoneInfo(tz)
    now = local_dt(ts, tz)
    target = datetime.combine(now.date(), parse_hhmm(hhmm), tzinfo=zone)
    if target < now:
        target = datetime.combine(now.date() + timedelta(days=1), parse_hhmm(hhmm), tzinfo=zone)
    return int((target - now).total_seconds())
