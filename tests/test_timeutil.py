"""Тесты для trolobot.timeutil: in_window, day_key/week_key, seconds_until.

Всё время — unix seconds (int), таймзона — persona.timezone (Europe/Warsaw по дефолту,
с летним/зимним переходом в 2026 году: 29 марта 01:00->03:00 UTC+1->+2,
25 октября 03:00->02:00 UTC+2->+1).
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from trolobot.timeutil import day_key, in_window, seconds_until, week_key

TZ = "Europe/Warsaw"
_ZONE = ZoneInfo(TZ)


def _ts(y: int, m: int, d: int, hh: int, mm: int, tz: ZoneInfo = _ZONE) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=tz).timestamp())


# --- in_window ---------------------------------------------------------------------

IN_WINDOW_CASES = [
    # (id, hh, mm, window, expected)
    ("inside_middle", 4, 30, ("02:00", "07:00"), True),
    ("start_inclusive", 2, 0, ("02:00", "07:00"), True),
    ("just_before_end", 6, 59, ("02:00", "07:00"), True),
    ("end_exclusive", 7, 0, ("02:00", "07:00"), False),
    ("just_before_start", 1, 59, ("02:00", "07:00"), False),
    ("well_outside", 12, 0, ("02:00", "07:00"), False),
]


@pytest.mark.parametrize(
    "case_id, hh, mm, window, expected", IN_WINDOW_CASES, ids=[c[0] for c in IN_WINDOW_CASES]
)
def test_in_window_boundaries(
    case_id: str, hh: int, mm: int, window: tuple[str, str], expected: bool
) -> None:
    ts = _ts(2026, 9, 10, hh, mm)
    assert in_window(ts, TZ, window) is expected


THROUGH_MIDNIGHT_CASES = [
    # окно ["23:00", "06:00") — через полночь
    ("late_evening_in", 23, 30, True),
    ("start_inclusive", 23, 0, True),
    ("early_morning_in", 3, 0, True),
    ("just_before_end", 5, 59, True),
    ("end_exclusive", 6, 0, False),
    ("just_before_start", 22, 59, False),
    ("midday_out", 12, 0, False),
]


@pytest.mark.parametrize(
    "case_id, hh, mm, expected", THROUGH_MIDNIGHT_CASES, ids=[c[0] for c in THROUGH_MIDNIGHT_CASES]
)
def test_in_window_through_midnight(case_id: str, hh: int, mm: int, expected: bool) -> None:
    ts = _ts(2026, 9, 10, hh, mm)
    assert in_window(ts, TZ, ("23:00", "06:00")) is expected


def test_in_window_empty_window_always_false() -> None:
    window = ("05:00", "05:00")
    for hh, mm in [(5, 0), (0, 0), (12, 0), (23, 59)]:
        ts = _ts(2026, 9, 10, hh, mm)
        assert in_window(ts, TZ, window) is False


def test_in_window_full_day_when_start_before_end_covers_whole_range() -> None:
    # окно ["00:00", "23:59"] — почти весь день, проверяет что интерпретация "start<end" верна
    window = ("00:00", "23:59")
    assert in_window(_ts(2026, 9, 10, 0, 0), TZ, window) is True
    assert in_window(_ts(2026, 9, 10, 23, 58), TZ, window) is True
    assert in_window(_ts(2026, 9, 10, 23, 59), TZ, window) is False


# --- day_key через переход на летнее/зимнее время -----------------------------------------


def test_day_key_stable_across_spring_forward() -> None:
    # 2026-03-29: в Europe/Warsaw 02:00 CET перескакивает на 03:00 CEST (час "пропадает"),
    # но календарный день не должен ломаться в обе стороны перехода.
    before = _ts(2026, 3, 29, 1, 30)  # 01:30 CET, ещё до перехода
    after = _ts(2026, 3, 29, 1, 30) + 3600  # +1 час astronomического времени -> 03:30 CEST
    assert day_key("ambient_count", before, TZ) == "ambient_count:2026-03-29"
    assert day_key("ambient_count", after, TZ) == "ambient_count:2026-03-29"


def test_day_key_stable_across_fall_back() -> None:
    # 2026-10-25: 03:00 CEST откатывается на 02:00 CET, час 02:00-03:00 существует дважды.
    # Оба прохода этого часа должны давать один и тот же ключ дня.
    utc_zone = ZoneInfo("UTC")
    first_pass = _ts(2026, 10, 25, 0, 30, tz=utc_zone)  # 02:30 CEST (первый проход)
    second_pass = first_pass + 3600  # 02:30 CET (второй проход, после перевода стрелок)
    third_pass = first_pass + 2 * 3600  # 03:30 CET, уже точно после перехода

    key_first = day_key("ambient_count", first_pass, TZ)
    key_second = day_key("ambient_count", second_pass, TZ)
    key_third = day_key("ambient_count", third_pass, TZ)

    assert key_first == "ambient_count:2026-10-25"
    assert key_second == "ambient_count:2026-10-25"
    assert key_third == "ambient_count:2026-10-25"


def test_day_key_changes_at_local_midnight_not_utc_midnight() -> None:
    # 23:30 в Варшаве (UTC+2 летом) — ещё локально тот же день, хотя в UTC уже за полночь.
    ts = _ts(2026, 9, 10, 23, 30)
    assert day_key("x", ts, TZ) == "x:2026-09-10"
    ts_next = _ts(2026, 9, 11, 0, 30)
    assert day_key("x", ts_next, TZ) == "x:2026-09-11"


# --- week_key на границе года (ISO week) ------------------------------------------------


def test_week_key_year_boundary_iso_week_53() -> None:
    # По ISO 8601 неделя может "перетекать" в следующий календарный год:
    # 2026-12-31 (четверг) и 2027-01-01 (пятница) обе принадлежат ISO-неделе 2026-W53.
    dec31 = _ts(2026, 12, 31, 12, 0)
    jan1 = _ts(2027, 1, 1, 12, 0)
    assert week_key("spontaneous", dec31, TZ) == "spontaneous:2026-W53"
    assert week_key("spontaneous", jan1, TZ) == "spontaneous:2026-W53"


def test_week_key_ordinary_week() -> None:
    ts = _ts(2026, 9, 10, 12, 0)  # четверг
    assert week_key("spontaneous", ts, TZ) == "spontaneous:2026-W37"


# --- seconds_until -------------------------------------------------------------------------


def test_seconds_until_later_today() -> None:
    now_ts = _ts(2026, 9, 10, 10, 0)
    assert seconds_until(now_ts, TZ, "12:00") == 2 * 3600


def test_seconds_until_already_passed_rolls_to_tomorrow() -> None:
    now_ts = _ts(2026, 9, 10, 10, 0)
    # 08:00 сегодня уже прошло -> ждём 08:00 завтра: 14 часов до полуночи + 8 часов = 22 часа
    assert seconds_until(now_ts, TZ, "08:00") == 22 * 3600


def test_seconds_until_exact_now_is_zero() -> None:
    now_ts = _ts(2026, 9, 10, 10, 0)
    assert seconds_until(now_ts, TZ, "10:00") == 0


def test_seconds_until_just_before_target() -> None:
    now_ts = _ts(2026, 9, 10, 9, 59)
    assert seconds_until(now_ts, TZ, "10:00") == 60


def test_seconds_until_just_after_target_rolls_to_tomorrow() -> None:
    now_ts = _ts(2026, 9, 10, 10, 1)
    assert seconds_until(now_ts, TZ, "10:00") == 24 * 3600 - 60
