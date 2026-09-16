"""Парсинг CHANGELOG.md (CLAUDE.md, "Интерфейсы: версии и changelog")."""

from __future__ import annotations

from pathlib import Path

import trolobot
from trolobot.changelog import Release, latest_release, parse_changelog

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_CHANGELOG = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_trolobot_version_is_0_5_0() -> None:
    assert trolobot.__version__ == "0.5.0"


def test_parse_real_changelog_latest_is_0_5_0_with_date_and_nonempty_blocks() -> None:
    releases = parse_changelog(REAL_CHANGELOG)

    assert releases[0].version == "0.5.0"
    assert releases[0].date == "2026-09-16"
    assert releases[0].for_chat.strip() != ""
    assert releases[0].for_owner.strip() != ""


def test_parse_real_changelog_order_is_newest_first() -> None:
    releases = parse_changelog(REAL_CHANGELOG)

    versions = [r.version for r in releases]
    assert versions == ["0.5.0", "0.4.0", "0.3.0", "0.2.0", "0.1.0"]


def test_parse_real_changelog_skips_unreleased() -> None:
    releases = parse_changelog(REAL_CHANGELOG)

    assert "Unreleased" not in [r.version for r in releases]
    assert all(r.version != "" for r in releases)


def test_latest_release_matches_first_of_parse() -> None:
    releases = parse_changelog(REAL_CHANGELOG)

    assert latest_release(REAL_CHANGELOG) == releases[0]


def test_latest_release_none_for_empty_text() -> None:
    assert latest_release("") is None


def test_parse_changelog_accepts_em_dash_and_hyphen() -> None:
    text = (
        "## [Unreleased]\n\n"
        "### Для чата\n\n### Для владельца\n\n"
        "## [1.2.0] — 2026-01-02\n\n"
        "### Для чата\n\nЕмдэш.\n\n### Для владельца\n\nЕмдэш-владелец.\n\n"
        "## [1.1.0] - 2026-01-01\n\n"
        "### Для чата\n\nДефис.\n\n### Для владельца\n\nДефис-владелец.\n"
    )

    releases = parse_changelog(text)

    assert [r.version for r in releases] == ["1.2.0", "1.1.0"]
    assert releases[0].for_chat == "Емдэш."
    assert releases[1].for_chat == "Дефис."


def test_parse_changelog_release_missing_blocks_returns_empty_strings() -> None:
    text = "## [1.0.0] — 2026-01-01\n\nБез разделов вовсе.\n"

    releases = parse_changelog(text)

    assert releases == [Release(version="1.0.0", date="2026-01-01", for_chat="", for_owner="")]


def test_parse_changelog_strips_trailing_link_references() -> None:
    text = (
        "## [1.0.0] — 2026-01-01\n\n"
        "### Для чата\n\nТекст.\n\n"
        "### Для владельца\n\nОписание.\n\n"
        "[1.0.0]: https://example.com/releases/tag/v1.0.0\n"
    )

    releases = parse_changelog(text)

    assert releases[0].for_owner == "Описание."
    assert "https://example.com" not in releases[0].for_owner


def test_parse_changelog_ignores_unreleased_entirely() -> None:
    text = (
        "## [Unreleased]\n\n"
        "### Для чата\n\nНе должно попасть в результат.\n\n"
        "### Для владельца\n\nТоже не должно.\n\n"
        "## [1.0.0] — 2026-01-01\n\n"
        "### Для чата\n\nРелиз.\n\n### Для владельца\n\nРелиз-владелец.\n"
    )

    releases = parse_changelog(text)

    assert len(releases) == 1
    assert releases[0].version == "1.0.0"
    assert "Не должно" not in releases[0].for_chat
