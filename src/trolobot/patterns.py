"""Скомпилированные паттерны гейта и фильтров (CHARACTER.md раздел 6, PLAN.md этап 2).

Всё компилируется один раз в __init__, дальше — чистые методы поиска по готовым
regex-объектам. Реализует gate_types.PatternsLike.
"""

from __future__ import annotations

import re

from trolobot.config_models import FiltersConfig


def _compile_regex_list(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(pattern, re.IGNORECASE) for pattern in patterns]


def _compile_phrase_list(phrases: list[str]) -> list[re.Pattern[str]]:
    """Маркеры ассистента — фразы, экранируются через re.escape при компиляции."""
    return [re.compile(re.escape(phrase), re.IGNORECASE) for phrase in phrases]


def _first_match(patterns: list[re.Pattern[str]], text: str) -> str | None:
    for pattern in patterns:
        if pattern.search(text):
            return pattern.pattern
    return None


class Patterns:
    """Компилирует регулярки фильтров и триггеры один раз, дальше только поиск."""

    def __init__(self, filters: FiltersConfig, name_triggers: list[str], bot_username: str) -> None:
        self._topic_stop = _compile_regex_list(filters.topic_stop)
        self._injection_markers = _compile_regex_list(filters.injection_markers)
        self._logistics = _compile_regex_list(filters.logistics)
        self._urgent = _compile_regex_list(filters.urgent)
        self._places_request = _compile_regex_list(filters.places_request)
        self._model_talk = _compile_regex_list(filters.model_talk)
        self._assistant_markers = _compile_phrase_list(filters.assistant_markers)
        self._grumpy_markers = _compile_regex_list(filters.grumpy_markers)
        # Реквизит и байки (CLAUDE.md, "меньше и разнообразнее", мера 5) —
        # компилируются здесь же, чтобы и промпт (слот {avoid}), и выходной фильтр
        # (dedup:motif / style:story_quota) брали один и тот же готовый набор.
        self._motifs = {
            label: _compile_regex_list(patterns) for label, patterns in filters.motifs.items()
        }
        self._story_markers = _compile_regex_list(filters.story_markers)
        self._name_triggers = [
            re.compile(rf"\b{re.escape(trigger)}\b", re.IGNORECASE) for trigger in name_triggers
        ]
        # "\b" не работает перед "@" (@ не словообразующий символ), поэтому границу
        # слева ставим лукбихайндом "не словообразующий символ перед @", а справа — "\b".
        self._bot_mention = (
            re.compile(rf"(?<!\w)@{re.escape(bot_username)}\b", re.IGNORECASE)
            if bot_username
            else None
        )

    def topic_stop(self, text: str) -> str | None:
        return _first_match(self._topic_stop, text)

    def injection(self, text: str) -> str | None:
        return _first_match(self._injection_markers, text)

    def logistics(self, text: str) -> str | None:
        return _first_match(self._logistics, text)

    def name_trigger(self, text: str) -> str | None:
        return _first_match(self._name_triggers, text)

    def mentions_bot(self, text: str) -> bool:
        if self._bot_mention is None:
            return False
        return self._bot_mention.search(text) is not None

    def urgent(self, text: str) -> bool:
        return _first_match(self._urgent, text) is not None

    def places_request(self, text: str) -> bool:
        return _first_match(self._places_request, text) is not None

    def model_talk(self, text: str) -> str | None:
        return _first_match(self._model_talk, text)

    def assistant_marker(self, text: str) -> str | None:
        return _first_match(self._assistant_markers, text)

    def grumpy(self, text: str) -> str | None:
        return _first_match(self._grumpy_markers, text)

    @property
    def motifs(self) -> dict[str, list[re.Pattern[str]]]:
        """Метка мотива -> скомпилированные регулярки (для motifs.py)."""
        return self._motifs

    @property
    def story_markers(self) -> list[re.Pattern[str]]:
        """Скомпилированные маркеры байки (для motifs.story_count)."""
        return self._story_markers
