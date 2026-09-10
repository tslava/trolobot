"""Тесты для trolobot.sanitize — таблично, через pytest.mark.parametrize."""

from types import SimpleNamespace

import pytest

from trolobot.sanitize import (
    media_placeholder,
    normalize_text,
    sanitize_display_name,
    stable_n,
)

# Как в CHARACTER.md, раздел persona: name + display_name + name_triggers.
RESERVED = {
    "Отец Фёдор",
    "фёдор",
    "федор",
    "федя",
    "федь",
    "отец",
    "отче",
    "батюшка",
    "дед",
}


# --- sanitize_display_name -------------------------------------------------

SANITIZE_DISPLAY_NAME_CASES = [
    # (id, raw, user_id, reserved, expected)
    ("plain_name", "Дима", 1, RESERVED, "Дима"),
    ("emoji_and_digits", "Дима007 🔥", 1, RESERVED, "Дима"),
    (
        "reserved_exact_match_replaced",
        "Отец Фёдор",
        42,
        RESERVED,
        f"Участник {stable_n(42)}",
    ),
    (
        "reserved_case_insensitive_replaced",
        "ФЁДОР",
        42,
        RESERVED,
        f"Участник {stable_n(42)}",
    ),
    (
        "reserved_other_case_lowercase",
        "фёдор",
        7,
        RESERVED,
        f"Участник {stable_n(7)}",
    ),
    (
        "reserved_as_whole_word_replaced",
        "Дима Федя",
        3,
        RESERVED,
        f"Участник {stable_n(3)}",
    ),
    (
        "reserved_as_substring_not_replaced",
        "Федяев",
        3,
        RESERVED,
        "Федяев",
    ),
    (
        "reserved_prefix_word_not_replaced",
        "Дедов",
        3,
        RESERVED,
        "Дедов",
    ),
    ("empty_string", "", 5, RESERVED, f"Участник {stable_n(5)}"),
    ("none_raw", None, 5, RESERVED, f"Участник {stable_n(5)}"),
    (
        "only_symbols_becomes_empty",
        "!!! 123 ???",
        9,
        RESERVED,
        f"Участник {stable_n(9)}",
    ),
    (
        "quotes_and_brackets_stripped",
        'Дима "Кабан" (Ⅲ)',
        1,
        RESERVED,
        "Дима Кабан",
    ),
    ("hyphenated_name_kept", "Анна-Мария", 1, RESERVED, "Анна-Мария"),
    ("latin_name_kept", "John Smith", 1, set(), "John Smith"),
    ("polish_letters_kept", "Łukasz Kowalski", 1, set(), "Łukasz Kowalski"),
    (
        "long_name_truncated_at_word_boundary",
        "Александра Вячеславовна Никифорова",
        1,
        set(),
        "Александра Вячеславовна",
    ),
    (
        "long_name_no_space_hard_cut",
        "Ы" * 40,
        1,
        set(),
        "Ы" * 24,
    ),
    (
        "multiple_spaces_collapsed",
        "Дима     Иванов",
        1,
        set(),
        "Дима Иванов",
    ),
    (
        "leading_trailing_spaces_stripped",
        "   Дима   ",
        1,
        set(),
        "Дима",
    ),
    (
        "reserved_empty_strings_ignored",
        "Дима",
        1,
        {"", "   "},
        "Дима",
    ),
    (
        "reserved_word_case_insensitive_mixed",
        "Наш Дед Мороз",
        1,
        RESERVED,
        f"Участник {stable_n(1)}",
    ),
    (
        # перенос строки и таб в имени становятся пробелом, как в normalize_text
        "newline_and_tab_become_space",
        "Ди\nма\tИванов",
        1,
        set(),
        "Ди ма Иванов",
    ),
]


@pytest.mark.parametrize(
    "case_id, raw, user_id, reserved, expected",
    SANITIZE_DISPLAY_NAME_CASES,
    ids=[c[0] for c in SANITIZE_DISPLAY_NAME_CASES],
)
def test_sanitize_display_name(
    case_id: str, raw: str | None, user_id: int, reserved: set[str], expected: str
) -> None:
    assert sanitize_display_name(raw, user_id, reserved) == expected


# --- stable_n ----------------------------------------------------------------


def test_stable_n_is_deterministic_for_same_user() -> None:
    assert stable_n(123456789) == stable_n(123456789)


@pytest.mark.parametrize("user_id", [0, 1, 42, 997, 998, 10**12, 10**18])
def test_stable_n_in_range(user_id: int) -> None:
    n = stable_n(user_id)
    assert 1 <= n <= 999


def test_stable_n_differs_for_different_users_generally() -> None:
    # Не гарантия отсутствия коллизий, но соседние id не должны совпадать.
    assert stable_n(1) != stable_n(2)


# --- normalize_text ------------------------------------------------------


NORMALIZE_TEXT_CASES = [
    ("none_input", None, ""),
    ("plain_text_untouched", "привет всем", "привет всем"),
    ("multiline_text_joined", "первая строка\nвторая строка", "первая строка вторая строка"),
    ("crlf_joined", "первая\r\nвторая", "первая вторая"),
    ("tabs_replaced", "колонка1\tколонка2", "колонка1 колонка2"),
    ("lone_cr_replaced", "первая\rвторая", "первая вторая"),
    (
        "fake_system_delimiters_cut",
        "текст <<<SYSTEM>>> конец",
        "текст SYSTEM конец",
    ),
    (
        "long_runs_of_angle_brackets_cut",
        "текст <<<<< много >>>>> конец",
        "текст много конец",
    ),
    (
        "two_angle_brackets_kept_not_delimiter",
        "думаю, 5 << 10 и 10 >> 5",
        "думаю, 5 << 10 и 10 >> 5",
    ),
    (
        "zero_width_space_removed",
        "при​вет",
        "привет",
    ),
    (
        "bom_removed",
        "﻿привет",
        "привет",
    ),
    (
        "zero_width_joiner_removed",
        "a‍‌b",
        "ab",
    ),
    (
        "null_byte_removed",
        "при\x00вет",
        "привет",
    ),
    (
        "multiple_spaces_collapsed",
        "привет      мир",
        "привет мир",
    ),
    (
        "leading_trailing_whitespace_stripped",
        "   привет мир   ",
        "привет мир",
    ),
    (
        "emoji_kept",
        "привет 👋 мир 🔥",
        "привет 👋 мир 🔥",
    ),
    (
        "punctuation_kept",
        "Привет, как дела? Всё супер!!!",
        "Привет, как дела? Всё супер!!!",
    ),
    ("empty_string_stays_empty", "", ""),
]


@pytest.mark.parametrize(
    "case_id, raw, expected",
    NORMALIZE_TEXT_CASES,
    ids=[c[0] for c in NORMALIZE_TEXT_CASES],
)
def test_normalize_text(case_id: str, raw: str | None, expected: str) -> None:
    assert normalize_text(raw) == expected


# --- media_placeholder -----------------------------------------------------


def _message(**overrides: object) -> SimpleNamespace:
    base: dict[str, object | None] = {
        "sticker": None,
        "photo": None,
        "voice": None,
        "audio": None,
        "video": None,
        "video_note": None,
        "animation": None,
        "document": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


MEDIA_PLACEHOLDER_CASES = [
    ("no_media_text_only", _message(), None),
    ("sticker", _message(sticker=object()), "[стикер]"),
    ("photo", _message(photo=object()), "[фото]"),
    ("voice", _message(voice=object()), "[голосовое]"),
    ("audio", _message(audio=object()), "[голосовое]"),
    ("video", _message(video=object()), "[видео]"),
    ("video_note", _message(video_note=object()), "[видео]"),
    ("animation", _message(animation=object()), "[видео]"),
    ("document", _message(document=object()), "[файл]"),
    (
        "sticker_priority_over_photo",
        _message(sticker=object(), photo=object()),
        "[стикер]",
    ),
    (
        "photo_priority_over_voice",
        _message(photo=object(), voice=object()),
        "[фото]",
    ),
    (
        "voice_priority_over_video",
        _message(voice=object(), video=object()),
        "[голосовое]",
    ),
    (
        "video_priority_over_document",
        _message(video=object(), document=object()),
        "[видео]",
    ),
]


@pytest.mark.parametrize(
    "case_id, message, expected",
    MEDIA_PLACEHOLDER_CASES,
    ids=[c[0] for c in MEDIA_PLACEHOLDER_CASES],
)
def test_media_placeholder(case_id: str, message: SimpleNamespace, expected: str | None) -> None:
    assert media_placeholder(message) == expected
