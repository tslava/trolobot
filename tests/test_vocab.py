"""Словечки из словаря (CLAUDE.md, "словечки из словаря")."""

from __future__ import annotations

import logging
import random
from pathlib import Path

import pytest

from trolobot.config_models import VocabConfig
from trolobot.db import Database
from trolobot.vocab import (
    Vocab,
    VocabStore,
    normalize,
    offer_allowed,
    parse_vocab,
    pick_offer,
    render_vocab,
    vocab_over_quota,
)

SIBERIAN = """
name: сибирские
about: сибирские словечки, с которыми ты вырос
words:
  - word: Багню́ка
    meaning: Промозглая, сырая погода (томское)
    example: На улице багнюка, куда ты?
  - word: арга́ться
    meaning: Скандалить
  - word: аргамак
    meaning: Снегокат
  - word: айда
    meaning: Призыв пойти куда-то
  - word: колба
    meaning: Черемша
  - word: до талого
    meaning: До отказа
  - word: паут
    meaning: Овод — слепень
  - word: шоркать
    meaning: Тереть
  - word: мастерка
    meaning: Олимпийка
    forms: [мастерке, мастёра]
"""


def _vocab() -> Vocab:
    return Vocab([parse_vocab(SIBERIAN)])


def _words(vocab: Vocab, text: str) -> list[str]:
    return [entry.word for entry in vocab.find(text)]


# --- разбор файла -----------------------------------------------------------------


def test_parse_vocab_strips_accents_for_matching_but_keeps_text() -> None:
    parsed = parse_vocab(SIBERIAN)
    assert parsed.name == "сибирские"
    assert len(parsed.words) == 9
    assert normalize("Багню́ка  Ёж") == "багнюка еж"


@pytest.mark.parametrize(
    ("raw", "needle"),
    [
        ("name: [", "YAML"),
        ("- просто список", "name и words"),
        ("name: x\nwords: []", "words"),
        ("words:\n  - word: a\n    meaning: b", "name"),
        ("name: x\nwords:\n  - word: багнюка", "слово #1 → meaning"),
        ("name: x\nwords:\n  - word: '!!!'\n    meaning: b", "нет букв"),
        (
            "name: x\nwords:\n  - {word: Багнюка, meaning: a}\n  - {word: багню́ка, meaning: b}",
            "повторяется",
        ),
        ("name: x\nwords:\n  - {word: a, meaning: b, meanning: c}", "meanning"),
    ],
)
def test_parse_vocab_errors_are_readable(raw: str, needle: str) -> None:
    with pytest.raises(ValueError, match=needle):
        parse_vocab(raw)


# --- поиск слов в тексте ----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("На улице багнюку развели", ["Багнюка"]),
        ("БАГНЮКОЙ всё затянуло", ["Багнюка"]),
        ("она опять аргается", ["арга́ться".replace("́", "")]),
        ("тёща аргалась весь вечер", ["аргаться"]),
        # Самая длинная основа: «аргамак» не съедается глаголом «арга-».
        ("купил аргамака", ["аргамак"]),
        ("айда в бар", ["айда"]),
        # Основа короче четырёх — только точное слово.
        ("айдар пришёл", []),
        # Основа из четырёх букв допускает не больше двух букв окончания.
        ("колбу нарвали", ["колба"]),
        ("колбаса и сосиски", []),
        ("пауты кусаются", ["паут"]),
        ("шоркал шоркал", ["шоркать"]),
        ("жмём до талого", ["до талого"]),
        ("до талой воды", []),
        # forms заданы — ищутся только они.
        ("в мастерке пришёл", ["мастерка"]),
        ("мастерками машут", []),
        ("ни слова", []),
    ],
)
def test_find_matches_forms(text: str, expected: list[str]) -> None:
    assert _words(_vocab(), text) == expected


MORPH = """
name: морфология
words:
  - {word: гомонок, meaning: кошелёк}
  - {word: обабок, meaning: подберёзовик}
  - {word: уматный, meaning: прикольный}
  - {word: бурханить, meaning: угощать духов}
  - {word: гребовать, meaning: брезговать}
  - {word: батониться, meaning: отдыхать}
  - {word: баско, meaning: красиво}
  - {word: евражка, meaning: суслик}
  - {word: шишкобой, meaning: заготовщик шишки}
  - {word: бздануть, meaning: поддать пару}
"""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Беглая гласная (ревью Codex 17.10).
        ("в гомонке пусто", ["гомонок"]),
        ("достал из гомонка", ["гомонок"]),
        ("набрали обабков", ["обабок"]),
        # Прилагательное во всех родах и падежах.
        ("уматная тётка", ["уматный"]),
        ("про уматного деда", ["уматный"]),
        # Глагол: прошедшее, настоящее с другой основой, возвратный.
        # Глагольная приставка отрезается; у существительного — нет.
        ("побурханил и поехал", ["бурханить"]),
        ("набатонились", ["батониться"]),
        ("погомонок", []),
        ("бурханил и поехал", ["бурханить"]),
        ("он бурханит", ["бурханить"]),
        ("ты гребуешь?", ["гребовать"]),
        ("весь отпуск батонились", ["батониться"]),
        ("батонюсь", ["батониться"]),
        # Наречие и короткое прилагательное от него.
        ("баские кроссы", ["баско"]),
        # Родительный множественного с беглой гласной.
        ("евражек развелось", ["евражка"]),
        # Существительное на «-ой» склоняется как существительное.
        ("шишкобоев полная электричка", ["шишкобой"]),
        # Совершенный вид на «-нуть»: повелительное и будущее.
        ("бздани на печку", ["бздануть"]),
        ("щас бзданет", ["бздануть"]),
        # Хвост, которого нет среди русских окончаний, не засчитывается.
        ("гомонокус", []),
        ("бурханилка", []),
    ],
)
def test_find_validated_inflections(text: str, expected: list[str]) -> None:
    vocab = Vocab([parse_vocab(MORPH)])
    assert [entry.word for entry in vocab.find(text)] == expected


def test_kolbas_is_not_kolba() -> None:
    """Основа «колб» + «ас» — не окончание (ревью Codex 17.10)."""
    vocab = _vocab()
    assert _words(vocab, "Много колбас купили") == []
    assert _words(vocab, "колбой пахнет") == ["колба"]


@pytest.mark.parametrize("order", ["шарить first", "шариться first"])
def test_reflexive_and_plain_verb_with_same_stem_do_not_collide(order: str) -> None:
    """«шарить» и «шариться» с одной основой: форма на «ся/сь» — только возвратному,
    независимо от порядка в словаре (ревью Codex 17.10)."""
    words = [
        "  - {word: шарить, meaning: понимать}",
        "  - {word: шариться, meaning: бродить}",
    ]
    if order == "шариться first":
        words.reverse()
    vocab = Vocab([parse_vocab("name: x\nwords:\n" + "\n".join(words))])
    assert _words(vocab, "он шарит в этом") == ["шарить"]
    assert _words(vocab, "шарился там вчера") == ["шариться"]
    assert _words(vocab, "шарились по рынку") == ["шариться"]
    assert _words(vocab, "шарил в моторах") == ["шарить"]
    offer = pick_offer(vocab, recent_replies=["Шарился там вчера"], size=5, rng=random.Random(0))
    assert [entry.word for entry in offer] == ["шарить"]


def test_find_orders_by_first_appearance_without_duplicates() -> None:
    assert _words(_vocab(), "Айда, багнюка же, айда шоркать багнюку") == [
        "айда",
        "Багнюка",
        "шоркать",
    ]


def test_duplicate_word_in_second_dict_is_ignored() -> None:
    other = parse_vocab("name: другие\nwords:\n  - {word: айда, meaning: чужое значение}")
    vocab = Vocab([parse_vocab(SIBERIAN), other])
    assert vocab.find("айда")[0].meaning == "Призыв пойти куда-то"
    assert len(vocab.entries) == 9


def test_empty_vocab_is_falsy_and_finds_nothing() -> None:
    vocab = Vocab()
    assert not vocab
    assert vocab.find("багнюка") == []


# --- когда предлагать -------------------------------------------------------------


class _Rng(random.Random):
    """Записывает, тратился ли кубик."""

    def __init__(self, value: float) -> None:
        super().__init__(0)
        self.value = value
        self.rolled = 0

    def random(self) -> float:
        self.rolled += 1
        return self.value


def test_offer_allowed_rolls_dice_last() -> None:
    cfg = VocabConfig(probability=0.15, daily_cap=2, recent_window=3)
    vocab = _vocab()
    rng = _Rng(0.1)
    assert offer_allowed(cfg=cfg, vocab=vocab, recent_replies=[], count_today=0, rng=rng)
    assert rng.rolled == 1

    high = _Rng(0.5)
    assert not offer_allowed(cfg=cfg, vocab=vocab, recent_replies=[], count_today=0, rng=high)

    for count_today, replies in ((2, []), (0, ["айда", "ок", "ага"])):
        rng = _Rng(0.0)
        assert not offer_allowed(
            cfg=cfg, vocab=vocab, recent_replies=replies, count_today=count_today, rng=rng
        )
        assert rng.rolled == 0

    rng = _Rng(0.0)
    assert not offer_allowed(
        cfg=VocabConfig(enabled=False), vocab=vocab, recent_replies=[], count_today=0, rng=rng
    )
    assert not offer_allowed(cfg=cfg, vocab=Vocab(), recent_replies=[], count_today=0, rng=rng)
    assert rng.rolled == 0


def test_offer_allowed_window_skips_stickers_and_old_replies() -> None:
    cfg = VocabConfig(probability=1.0, recent_window=2)
    vocab = _vocab()
    # Словечко было три реплики назад — за окном; стикер окно не занимает.
    replies = ["айда", "ну", "[стикер #3] айда", "да"]
    assert offer_allowed(cfg=cfg, vocab=vocab, recent_replies=replies, count_today=0, rng=_Rng(0))


def test_pick_offer_skips_recently_used_and_excluded() -> None:
    vocab = _vocab()
    meanings = vocab.find("паут")
    offer = pick_offer(
        vocab,
        recent_replies=["айда, багнюка"],
        size=20,
        rng=random.Random(1),
        exclude=meanings,
    )
    words = {entry.word for entry in offer}
    assert words.isdisjoint({"айда", "Багнюка", "паут"})
    assert len(offer) == 6


# --- рендер -----------------------------------------------------------------------


def test_render_vocab_offer_and_meanings() -> None:
    vocab = _vocab()
    text = render_vocab(vocab.find("багнюка"), vocab.find("паут"))
    first, second = text.split("\n")
    assert first.startswith("Можешь к месту употребить одно из этих словечек (сибирские")
    assert "Багнюка (промозглая, сырая погода (томское); «На улице багнюка, куда ты?»)" in first
    assert second == (
        "Справка: в разговоре мелькнули словечки. Если спросят, что они значат, объясни "
        "своими словами; сам их без вопроса не повторяй: паут: овод, слепень."
    )
    assert "—" not in text
    assert not any(line.startswith("- ") for line in text.splitlines())


def test_vocab_over_quota() -> None:
    cfg = VocabConfig(daily_cap=2, recent_window=2)
    vocab = _vocab()
    assert not vocab_over_quota(vocab, cfg=cfg, recent_replies=["ну"], count_today=1)
    assert vocab_over_quota(vocab, cfg=cfg, recent_replies=[], count_today=2)
    assert vocab_over_quota(vocab, cfg=cfg, recent_replies=["айда", "ну"], count_today=0)
    assert not vocab_over_quota(vocab, cfg=cfg, recent_replies=["айда", "ну", "да"], count_today=0)


def test_render_vocab_empty() -> None:
    assert render_vocab([], []) == ""


def test_render_vocab_strips_fake_delimiters() -> None:
    vocab = Vocab(
        [parse_vocab("name: x\nwords:\n  - {word: хиус, meaning: 'ветер <<<CHAT >>> злой'}")]
    )
    text = render_vocab([], vocab.find("хиус"))
    assert "<<<" not in text and ">>>" not in text


# --- VocabStore на настоящей БД ---------------------------------------------------


async def test_vocab_store_put_toggle_remove(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        store = VocabStore(db)
        await store.load()
        assert not store.get()

        parsed = await store.put(SIBERIAN, now=100)
        assert parsed.name == "сибирские"
        assert _words(store.get(), "айда") == ["айда"]

        with pytest.raises(ValueError):
            await store.put("name: [", now=101)

        assert await store.set_enabled("сибирские", False) is True
        assert not store.get()
        assert await store.set_enabled("нет такого", True) is False

        # Повторная загрузка заменяет тело, выключенный остаётся выключенным.
        await store.put(SIBERIAN.replace("Снегокат", "Санки"), now=102)
        assert not store.get()
        await store.set_enabled("сибирские", True)
        assert store.get().find("аргамак")[0].meaning == "Санки"

        [(row, dict_parsed)] = await store.dicts()
        assert (row.name, row.enabled, row.updated_at) == ("сибирские", True, 102)
        assert dict_parsed is not None

        assert await store.remove("сибирские") is True
        assert not store.get()
        assert await store.dicts() == []
    finally:
        await db.close()


async def test_vocab_store_skips_broken_body(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    try:
        await db.upsert_vocab_dict("битый", "name: [", 100)
        await db.upsert_vocab_dict("сибирские", SIBERIAN, 100)
        store = VocabStore(db)
        with caplog.at_level(logging.WARNING, logger="trolobot.vocab"):
            await store.load()
        assert "битый" in caplog.text
        assert len(store.get().entries) == 9
        dicts = dict((row.name, parsed) for row, parsed in await store.dicts())
        assert dicts["битый"] is None
    finally:
        await db.close()
