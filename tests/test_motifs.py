"""motifs.py: реквизит персонажа и квота баек (CLAUDE.md, "меньше и разнообразнее")."""

from __future__ import annotations

import re

from trolobot.config_models import FiltersConfig
from trolobot.motifs import motifs_in, render_avoid, story_count, used_motifs
from trolobot.patterns import Patterns


def _patterns() -> Patterns:
    return Patterns(FiltersConfig(), ["федя"], "")


def _motifs() -> dict[str, list[re.Pattern[str]]]:
    return _patterns().motifs


def _story_markers() -> list[re.Pattern[str]]:
    return _patterns().story_markers


# --- motifs_in ---------------------------------------------------------------------


def test_motifs_in_finds_labels_in_config_order() -> None:
    text = "В гараже сидел, потом жена позвала в теплицу."

    assert motifs_in(text, _motifs()) == ["жена", "теплица", "гараж"]


def test_motifs_in_empty_text_is_empty() -> None:
    assert motifs_in("", _motifs()) == []


def test_motifs_in_nothing_matched_is_empty() -> None:
    assert motifs_in("Тихо прошли выходные.", _motifs()) == []


def test_motifs_in_is_case_insensitive() -> None:
    assert motifs_in("ГАРАЖ закрыт.", _motifs()) == ["гараж"]


def test_motifs_in_word_boundary_for_wife() -> None:
    """«жена/жене/женой» — мотив, «женился» — нет (регулярка по границам слов)."""
    assert motifs_in("Жена сказала.", _motifs()) == ["жена"]
    assert motifs_in("Женой доволен.", _motifs()) == ["жена"]
    assert "жена" not in motifs_in("Сосед женился в мае.", _motifs())


def test_motifs_in_nineties_digits_form() -> None:
    assert motifs_in("Это было в 95-м.", _motifs()) == ["девяностые"]


# --- used_motifs -------------------------------------------------------------------


def test_used_motifs_only_looks_at_window_tail() -> None:
    replies = ["Жена сказала.", "Теплица протекла.", "Тихо прошли.", "Ключи нашлись."]

    assert used_motifs(replies, 2, _motifs()) == []
    assert used_motifs(replies, 4, _motifs()) == ["жена", "теплица"]


def test_used_motifs_zero_window_is_empty() -> None:
    assert used_motifs(["Жена сказала."], 0, _motifs()) == []


def test_used_motifs_deduplicates_labels() -> None:
    replies = ["Жена сказала.", "Жене не понравилось."]

    assert used_motifs(replies, 10, _motifs()) == ["жена"]


def test_used_motifs_skips_sticker_records() -> None:
    """«[стикер #N] надпись» — картинка, а не реплика персонажа: мотивов в ней нет."""
    replies = ["[стикер #3] Жена сказала", "Тихо прошли."]

    assert used_motifs(replies, 10, _motifs()) == []


# --- story_count -------------------------------------------------------------------


def test_story_count_counts_replies_not_matches() -> None:
    replies = ["Помню, в девяностых так же было.", "Тихо прошли.", "Как-то раз повезло."]

    assert story_count(replies, 10, _story_markers()) == 2


def test_story_count_respects_window() -> None:
    replies = ["Помню, было дело.", "Тихо прошли.", "Ключи нашлись."]

    assert story_count(replies, 2, _story_markers()) == 0
    assert story_count(replies, 3, _story_markers()) == 1


def test_story_count_skips_sticker_records() -> None:
    assert story_count(["[стикер #1] помню такое"], 10, _story_markers()) == 0


def test_story_count_zero_window_is_zero() -> None:
    assert story_count(["Помню, было дело."], 0, _story_markers()) == 0


# --- render_avoid ------------------------------------------------------------------


def test_render_avoid_empty_when_nothing_to_say() -> None:
    assert render_avoid([], no_story=False) == ""


def test_render_avoid_declines_known_labels() -> None:
    text = render_avoid(["жена", "теплица", "гараж"], no_story=False)

    assert "уже поминал: жену, теплицу, гараж" in text
    assert "другая деталь или вовсе без байки" in text


def test_render_avoid_unknown_label_goes_as_is() -> None:
    assert "рыбалку" not in render_avoid(["рыбалка"], no_story=False)
    assert "рыбалка" in render_avoid(["рыбалка"], no_story=False)


def test_render_avoid_no_story_only() -> None:
    text = render_avoid([], no_story=True)

    assert text.startswith("Байку сейчас не рассказывай")
    assert "поминал" not in text


def test_render_avoid_both_parts() -> None:
    text = render_avoid(["гараж"], no_story=True)

    assert "уже поминал: гараж" in text
    assert "Байку сейчас не рассказывай" in text


def test_render_avoid_has_no_typographic_dash() -> None:
    """Модель копирует типографику промпта — тире в нём быть не должно."""
    text = render_avoid(["жена"], no_story=True)

    assert "—" not in text
    assert "–" not in text
