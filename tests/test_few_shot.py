"""few_shot.yaml: загрузка и рендер в формат промпта."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from trolobot.few_shot import FewShot, load_few_shot, render_few_shot

REPO_ROOT = Path(__file__).resolve().parents[1]
FEW_SHOT_PATH = REPO_ROOT / "few_shot.yaml"


def test_load_real_few_shot_without_errors() -> None:
    items = load_few_shot(FEW_SHOT_PATH)

    assert len(items) == 17
    assert all(isinstance(item, FewShot) for item in items)
    assert any(item.speak is False for item in items)
    assert any(item.speak is True for item in items)


def test_render_two_examples_matches_expected_format() -> None:
    items = [
        FewShot(
            name="Дима",
            user="опять дедлайн перенесли, третий раз",
            speak=True,
            text=(
                "У нас в девяносто восьмом тоже переносили. Дважды. "
                "Потом контору закрыли, и вопрос снялся."
            ),
        ),
        FewShot(name="Аня", user="всем доброе утро", speak=False),
    ]

    expected = (
        "Дима: опять дедлайн перенесли, третий раз\n"
        '{"speak": true, "text": "У нас в девяносто восьмом тоже переносили. Дважды. '
        'Потом контору закрыли, и вопрос снялся."}'
        "\n\n"
        "Аня: всем доброе утро\n"
        '{"speak": false, "text": ""}'
    )

    assert render_few_shot(items) == expected


def test_speak_true_requires_non_empty_text() -> None:
    with pytest.raises(ValidationError):
        FewShot(name="X", user="y", speak=True, text="")


def test_speak_false_requires_empty_text() -> None:
    with pytest.raises(ValidationError):
        FewShot(name="X", user="y", speak=False, text="что-то")
