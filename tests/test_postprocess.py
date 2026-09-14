"""Тесты для trolobot.postprocess: normalize_dashes, strip_emoji, keep_first_emoji, soften.

Чистые функции, без I/O — как и сам модуль (CLAUDE.md, "Интерфейсы: пост-обработка").
"""

from __future__ import annotations

from trolobot.config_models import FiltersConfig
from trolobot.postprocess import (
    Fixed,
    keep_first_emoji,
    normalize_dashes,
    soften,
    strip_emoji,
)

# --------------------------------------------------------------------------- #
# normalize_dashes
# --------------------------------------------------------------------------- #


def test_normalize_dashes_em_dash() -> None:
    assert normalize_dashes("слово — слово") == "слово - слово"


def test_normalize_dashes_en_dash() -> None:
    assert normalize_dashes("слово – слово") == "слово - слово"


def test_normalize_dashes_figure_dash() -> None:
    assert normalize_dashes("слово ‒ слово") == "слово - слово"


def test_normalize_dashes_horizontal_bar() -> None:
    assert normalize_dashes("слово ― слово") == "слово - слово"


def test_normalize_dashes_hyphen_in_word_untouched() -> None:
    assert normalize_dashes("по-русски кто-то") == "по-русски кто-то"


def test_normalize_dashes_no_dash_unchanged() -> None:
    assert normalize_dashes("Бывает, с кем не случается.") == "Бывает, с кем не случается."


def test_normalize_dashes_multiple() -> None:
    assert normalize_dashes("раз — два – три") == "раз - два - три"


# --------------------------------------------------------------------------- #
# strip_emoji
# --------------------------------------------------------------------------- #


def test_strip_emoji_at_end() -> None:
    assert strip_emoji("Бывает 🙂") == "Бывает"


def test_strip_emoji_at_start() -> None:
    assert strip_emoji("🙂 Бывает") == "Бывает"


def test_strip_emoji_in_middle() -> None:
    assert strip_emoji("Бывает 🙂 привет") == "Бывает привет"


def test_strip_emoji_before_punctuation_no_hanging_space() -> None:
    assert strip_emoji("Бывает 🙂, как дела") == "Бывает, как дела"


def test_strip_emoji_after_dot() -> None:
    assert strip_emoji("Бывает. 💩") == "Бывает."


def test_strip_emoji_with_variation_selector() -> None:
    assert strip_emoji("Бывает 👍️") == "Бывает"


def test_strip_emoji_with_skin_tone_modifier() -> None:
    assert strip_emoji("Бывает 👍🏻") == "Бывает"


def test_strip_emoji_multiple() -> None:
    assert strip_emoji("Бывает 🙂🙂") == "Бывает"


def test_strip_emoji_no_emoji_unchanged() -> None:
    assert strip_emoji("Бывает, с кем не случается.") == "Бывает, с кем не случается."


def test_strip_emoji_no_double_space_result() -> None:
    result = strip_emoji("Бывает 🙂 привет")
    assert "  " not in result


# --------------------------------------------------------------------------- #
# keep_first_emoji
# --------------------------------------------------------------------------- #


def test_keep_first_emoji_two() -> None:
    assert keep_first_emoji("Бывает 🙂🙂") == "Бывает 🙂"


def test_keep_first_emoji_far_apart() -> None:
    assert keep_first_emoji("Бывает 🙂 и вот 💩 тоже") == "Бывает 🙂 и вот тоже"


def test_keep_first_emoji_single_unchanged() -> None:
    assert keep_first_emoji("Бывает 🙂") == "Бывает 🙂"


def test_keep_first_emoji_none_unchanged() -> None:
    assert keep_first_emoji("Бывает.") == "Бывает."


# --------------------------------------------------------------------------- #
# soften
# --------------------------------------------------------------------------- #

_CFG = FiltersConfig()  # allowed_emoji по умолчанию, emoji_max_per_reply=1, emoji_recent_window=4


def test_soften_dash_only() -> None:
    fixed = soften("Раньше было проще — теперь не так.", recent_replies=[], cfg=_CFG)
    assert fixed == Fixed(text="Раньше было проще - теперь не так.", fixes=("fix:dash",))


def test_soften_no_fixes_needed() -> None:
    fixed = soften("Бывает, с кем не случается.", recent_replies=[], cfg=_CFG)
    assert fixed == Fixed(text="Бывает, с кем не случается.", fixes=())


def test_soften_emoji_freq_strips_when_recent_has_emoji() -> None:
    recent = ["Все по домам.", "Бывает.", "Ладно.", "Зря 😂"]
    fixed = soften("Бывает 🙂", recent_replies=recent, cfg=_CFG)
    assert fixed.fixes == ("fix:emoji_freq",)
    assert fixed.text == "Бывает"


def test_soften_emoji_freq_outside_window_pass() -> None:
    recent = [
        "Зря 😂",
        "Все по домам разошлись.",
        "Ладно, я в гараже.",
        "Купил торф для рассады.",
        "Спокойной ночи всем.",
    ]
    fixed = soften("Бывает 🙂", recent_replies=recent, cfg=_CFG)
    assert fixed.fixes == ()
    assert fixed.text == "Бывает 🙂"


def test_soften_emoji_count_keeps_first() -> None:
    fixed = soften("Бывает 🙂🙂", recent_replies=[], cfg=_CFG)
    assert fixed.fixes == ("fix:emoji_count",)
    assert fixed.text == "Бывает 🙂"


def test_soften_order_dash_then_emoji_freq() -> None:
    recent = ["Зря 😂"]
    fixed = soften("Раньше было — теперь 🙂", recent_replies=recent, cfg=_CFG)
    assert fixed.fixes == ("fix:dash", "fix:emoji_freq")
    assert fixed.text == "Раньше было - теперь"


def test_soften_emoji_freq_and_count_only_freq_applies() -> None:
    # emoji_freq проверяется первым: если сработал, до emoji_count дело не доходит,
    # даже если в кандидате несколько разрешённых эмодзи.
    recent = ["Зря 😂"]
    fixed = soften("Бывает 🙂🙂", recent_replies=recent, cfg=_CFG)
    assert fixed.fixes == ("fix:emoji_freq",)
    assert fixed.text == "Бывает"


def test_soften_sticker_placeholder_not_counted_as_emoji() -> None:
    recent = ["[стикер #3] Ну началось"]
    fixed = soften("Бывает 🙂", recent_replies=recent, cfg=_CFG)
    assert fixed.fixes == ()
    assert fixed.text == "Бывает 🙂"


def test_soften_window_respects_emoji_recent_window() -> None:
    cfg = FiltersConfig(emoji_recent_window=1)
    # эмодзи было 2 реплики назад, окно = 1 -> не считается.
    recent = ["Зря 😂", "Ладно."]
    fixed = soften("Бывает 🙂", recent_replies=recent, cfg=cfg)
    assert fixed.fixes == ()
    assert fixed.text == "Бывает 🙂"


def test_soften_no_emoji_no_dash_unchanged() -> None:
    fixed = soften("Ключи в гараже были.", recent_replies=["Зря 😂"], cfg=_CFG)
    assert fixed == Fixed(text="Ключи в гараже были.", fixes=())
