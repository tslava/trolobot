"""Тесты шуток чата (CLAUDE.md, "шутки чата и истории людей"): чистые функции."""

from __future__ import annotations

from trolobot.db import JokeRow
from trolobot.jokes import joke_used, render_jokes

DAY = 86400
NOW = 100 * DAY


def _joke(joke_id: int, text: str, used_at: int | None = None) -> JokeRow:
    return JokeRow(
        id=joke_id,
        text=text,
        created_at=NOW - 30 * DAY,
        last_used_at=used_at,
        uses=0 if used_at is None else 1,
    )


def test_render_jokes_empty_is_empty_string() -> None:
    assert render_jokes([], now=NOW, cooldown_days=5) == ""


def test_render_jokes_single_line_with_quotes() -> None:
    rendered = render_jokes(
        [_joke(1, "опять гвозди"), _joke(2, "как у Лёхи")], now=NOW, cooldown_days=5
    )

    assert "\n" not in rendered
    assert rendered.startswith("Шутки и словечки вашего чата")
    assert rendered.endswith("«опять гвозди»; «как у Лёхи».")


def test_render_jokes_hides_recently_used_shows_after_cooldown() -> None:
    rows = [
        _joke(1, "свежая"),
        _joke(2, "недавно", used_at=NOW - 2 * DAY),
        _joke(3, "давно", used_at=NOW - 5 * DAY),
    ]

    rendered = render_jokes(rows, now=NOW, cooldown_days=5)

    assert "«свежая»" in rendered
    assert "«давно»" in rendered
    assert "недавно" not in rendered


def test_render_jokes_all_cooling_down_is_empty() -> None:
    rows = [_joke(1, "недавно", used_at=NOW - DAY)]
    assert render_jokes(rows, now=NOW, cooldown_days=5) == ""


def test_render_jokes_zero_cooldown_always_shows() -> None:
    rows = [_joke(1, "только что", used_at=NOW)]
    assert "«только что»" in render_jokes(rows, now=NOW, cooldown_days=0)


def test_render_jokes_cleans_bullets_and_delimiters_and_never_starts_with_dash() -> None:
    rendered = render_jokes(
        [_joke(1, "- опять <<<гвозди>>>"), _joke(2, "  ")], now=NOW, cooldown_days=5
    )

    assert "<<<" not in rendered
    assert ">>>" not in rendered
    assert "«опять гвозди»" in rendered
    assert "«»" not in rendered
    assert all(not line.startswith("- ") for line in rendered.splitlines())


def test_joke_used_three_common_words_in_a_row() -> None:
    assert joke_used("Ну вот, опять Ильины гвозди в стене!", "опять Ильины гвозди")
    assert joke_used("опять ильины гвозди вспомнил", "Это опять Ильины гвозди, да")


def test_joke_used_needs_consecutive_words() -> None:
    assert not joke_used("опять про какие-то гвозди Ильины", "опять Ильины гвозди")


def test_joke_used_two_common_words_not_enough_for_long_joke() -> None:
    assert not joke_used("опять Ильины проблемы", "опять Ильины гвозди в стене")


def test_joke_used_short_joke_must_be_contained_entirely() -> None:
    assert joke_used("Ну как у Лёхи, да.", "как у Лёхи")
    assert joke_used("Лёха вернулся", "Лёха")
    assert not joke_used("как у Васи", "как у Лёхи")
    assert not joke_used("Лёхи тут нет", "Лёха")  # целое слово, не подстрока


def test_joke_used_normalizes_case_punctuation_and_yo() -> None:
    assert joke_used("КАК У ЛЕХИ!!!", "как у Лёхи")


def test_joke_used_empty_inputs() -> None:
    assert not joke_used("", "опять гвозди")
    assert not joke_used("опять гвозди", "")
    assert not joke_used("...", "!!!")
