"""Тесты для trolobot.morph — падежные формы слова-обращения.

Главное здесь не полнота склонения, а отсутствие ложных срабатываний: «федерация»,
«дедлайн», «Федяев», «отечество», «батюшкам» не должны совпадать ни с одним
триггером из persona.name_triggers, иначе бот начнёт лезть в чужие разговоры.
"""

from __future__ import annotations

import re

import pytest

from trolobot.morph import name_forms, name_pattern

# Все триггеры из PersonaConfig.name_triggers по умолчанию — «фёдор» и «федор»
# держатся отдельными словами, буква «ё» в коде не нормализуется.
TRIGGERS = ["фёдор", "федор", "федя", "федь", "отец", "отче", "батюшка", "дед"]


def _matches(text: str) -> bool:
    return any(
        re.search(name_pattern(trigger), text, re.IGNORECASE) is not None for trigger in TRIGGERS
    )


# --- name_forms ---------------------------------------------------------------

FORMS_CASES = [
    ("fyodor_yo", "фёдор", ["фёдор", "фёдора", "фёдору", "фёдором", "фёдоре", "фёдоры"]),
    ("fyodor_e", "федор", ["федор", "федора", "федору", "федором", "федоре", "федоры"]),
    ("fedya", "федя", ["федя", "феди", "феде", "федю", "федей"]),
    ("fed_soft", "федь", ["федь"]),
    ("otec", "отец", ["отец", "отца", "отцу", "отцом", "отце"]),
    ("otche", "отче", ["отче"]),
    ("batyushka", "батюшка", ["батюшка", "батюшки", "батюшке", "батюшку", "батюшкой"]),
    ("ded", "дед", ["дед", "деда", "деду", "дедом", "деде", "деды"]),
]


@pytest.mark.parametrize("case_id, word, expected", FORMS_CASES, ids=[c[0] for c in FORMS_CASES])
def test_name_forms(case_id: str, word: str, expected: list[str]) -> None:
    assert name_forms(word) == expected


def test_name_forms_are_unique() -> None:
    for trigger in TRIGGERS:
        forms = name_forms(trigger)
        assert len(forms) == len(set(forms)), trigger


def test_name_forms_all_match_their_pattern() -> None:
    """Формы и регулярка — одно и то же множество: каждая форма обязана матчиться."""
    for trigger in TRIGGERS:
        pattern = re.compile(name_pattern(trigger), re.IGNORECASE)
        for form in name_forms(trigger):
            assert pattern.search(form) is not None, (trigger, form)


def test_empty_word_is_literal() -> None:
    assert name_forms("") == [""]


def test_latin_word_falls_back_to_exact_match() -> None:
    """Латиница ничем не склоняется: поведение как до morph — точное слово."""
    assert name_forms("fedor") == ["fedor"]
    assert re.search(name_pattern("fedor"), "fedora linux", re.IGNORECASE) is None


def test_special_characters_are_escaped() -> None:
    """Триггер приходит из config.yaml: регулярка не должна ломаться на точке."""
    pattern = name_pattern("о.тец")
    assert re.search(pattern, "о.тец", re.IGNORECASE) is not None
    assert re.search(pattern, "оxтец", re.IGNORECASE) is None


# --- матчинг текста -----------------------------------------------------------

MATCH_CASES = [
    # падежи — должны совпадать
    ("fedora_genitive", "играю с внуком Федора", True),
    ("fyodoru_dative", "передай Фёдору привет", True),
    ("fede", "Феде бы это понравилось", True),
    ("fedyu", "зови Федю", True),
    ("fedorom", "с Федором вчера виделись", True),
    ("otca", "спроси у отца", True),
    ("otcu", "отцу бы такое зашло", True),
    ("otcom", "с отцом всё понятно", True),
    ("dedom", "с дедом на рыбалку", True),
    ("deda", "у деда спроси", True),
    ("batyushki", "у батюшки спроси", True),
    ("batyushku", "позови батюшку", True),
    # именительные — как раньше
    ("fedya_nominative", "Федя, ты где", True),
    ("ded_nominative", "дед ты спишь", True),
    ("otec_nominative", "отец лайкнул сообщение выше", True),
    # ложные срабатывания — не должны совпадать никогда
    ("federaciya", "федерация профсоюзов", False),
    ("dedline", "дедлайн горит", False),
    ("fedyaev", "Федяев написал письмо", False),
    ("otechestvo", "отечество наше", False),
    ("otchestvo", "имя отчество фамилия", False),
    ("batyushkam", "батюшкам и матушкам", False),
    ("otcovskiy", "отцовский гараж", False),
    ("dedushka", "дедушка Мороз", False),
    ("federalnyy", "федеральный закон", False),
    ("fedorovich", "Иван Фёдорович", False),
]


@pytest.mark.parametrize(
    "case_id, text, expect_match", MATCH_CASES, ids=[c[0] for c in MATCH_CASES]
)
def test_name_pattern_matching(case_id: str, text: str, expect_match: bool) -> None:
    assert _matches(text) is expect_match
