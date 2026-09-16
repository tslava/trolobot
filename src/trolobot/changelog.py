"""Парсинг CHANGELOG.md (Keep a Changelog) для команды /changelog.

CLAUDE.md, "Интерфейсы: версии и changelog": единственный источник release-notes —
CHANGELOG.md в корне репозитория, две секции на версию ("### Для чата" и
"### Для владельца"), "## [Unreleased]" в списке версий не участвует. Чистые
функции без I/O — чтение файла делает вызывающий (commands.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Заголовок версии: "## [0.5.0] — 2026-09-16" — тире может быть длинным ("—") или
# обычным дефисом ("-"), с пробелами вокруг или без. "## [Unreleased]" не совпадает —
# у него нет номера версии и даты.
_VERSION_HEADER_RE = re.compile(
    r"^##\s*\[(?P<version>\d+\.\d+\.\d+)\]\s*[—-]\s*(?P<date>\d{4}-\d{2}-\d{2})\s*$",
    re.MULTILINE,
)
# Любой заголовок второго уровня ("## ...") — им ограничивается тело версии, включая
# "## [Unreleased]", если она вдруг не первая.
_ANY_H2_RE = re.compile(r"^##\s", re.MULTILINE)
# Заголовок третьего уровня ("### ...") — им ограничивается тело блока внутри версии.
_ANY_H3_RE = re.compile(r"^###\s", re.MULTILINE)
_FOR_CHAT_RE = re.compile(r"^###\s*Для чата\s*$", re.MULTILINE)
_FOR_OWNER_RE = re.compile(r"^###\s*Для владельца\s*$", re.MULTILINE)
# Ссылки-сноски внизу файла ("[0.5.0]: https://..."). Они идут после последней
# версии, без заголовка "##"/"###" между ними и последним блоком, поэтому их нужно
# отрезать отдельно, а не полагаться на границу следующего заголовка.
_LINK_REF_RE = re.compile(r"^\[[^\]]+\]:\s", re.MULTILINE)


@dataclass(frozen=True)
class Release:
    """Одна версия из CHANGELOG.md. Тексты блоков — без заголовков, strip()."""

    version: str
    date: str
    for_chat: str
    for_owner: str


def _section_text(body: str, header_re: re.Pattern[str]) -> str:
    match = header_re.search(body)
    if match is None:
        return ""
    start = match.end()
    next_h3 = _ANY_H3_RE.search(body, start)
    end = next_h3.start() if next_h3 else len(body)
    return body[start:end].strip()


def parse_changelog(text: str) -> list[Release]:
    """Версии в порядке файла (новые первыми, как их пишет владелец).

    "## [Unreleased]" в списке версий не участвует.
    """
    h2_starts = [m.start() for m in _ANY_H2_RE.finditer(text)]
    releases: list[Release] = []
    for match in _VERSION_HEADER_RE.finditer(text):
        body_start = match.end()
        next_h2 = next((pos for pos in h2_starts if pos > match.start()), None)
        body_end = next_h2 if next_h2 is not None else len(text)
        body = text[body_start:body_end]

        link_ref = _LINK_REF_RE.search(body)
        if link_ref is not None:
            body = body[: link_ref.start()]

        releases.append(
            Release(
                version=match.group("version"),
                date=match.group("date"),
                for_chat=_section_text(body, _FOR_CHAT_RE),
                for_owner=_section_text(body, _FOR_OWNER_RE),
            )
        )
    return releases


def latest_release(text: str) -> Release | None:
    releases = parse_changelog(text)
    return releases[0] if releases else None
