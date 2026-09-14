"""Тесты выходного фильтра (этап 4): слои 1-2 и сборка в check_output.

Табличные тесты через pytest.mark.parametrize. Основная таблица прогоняет вход
через check_output (без судьи) и проверяет либо полный pass, либо что ожидаемая
причина есть среди verdict.reasons (кейсы, где по конструкции текста может
сработать больше одной причины сразу — фильтр это допускает, реального вреда
персонажу от лишнего срабатывания нет, ретраев всё равно нет).

Таблица собирает кейсы из приёмки этапа 4 (PLAN.md, "таблица тестов по голосу")
и часть таблицы из 16 инъекций CLAUDE.md, которая проверяется именно на уровне
выходного фильтра (эхо, утечка промпта, маркеры модели, стоп-лист на выходе,
телефон, латиница) — остальные строки этой таблицы (санитизация имени, гейт 5a,
подстановка слотов, разделители, судья) проверяются в sanitize/gate/prompt/judge,
не здесь.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trolobot.config_models import Config
from trolobot.db import MessageRow
from trolobot.few_shot import load_few_shot
from trolobot.filters import FilterContext, FilterVerdict, check_output

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT = (REPO_ROOT / "prompts" / "system.txt").read_text(encoding="utf-8")

# --------------------------------------------------------------------------- #
# Хелперы
# --------------------------------------------------------------------------- #


def _row(display_name: str, text: str, *, is_bot: bool = False, created_at: int = 1) -> MessageRow:
    return MessageRow(
        id=created_at,
        tg_message_id=created_at,
        chat_id=1,
        user_id=1,
        display_name=display_name,
        text=text,
        reply_to_tg_message_id=None,
        is_bot=is_bot,
        created_at=created_at,
    )


def _ctx(
    *,
    cfg: Config | None = None,
    recent_replies: list[str] | None = None,
    context_rows: list[MessageRow] | None = None,
    places_names: list[str] | None = None,
    participant_names: list[str] | None = None,
    bot_names: list[str] | None = None,
    muted_names: list[str] | None = None,
    system_prompt: str = "",
    trigger_text: str = "",
    now: int = 0,
) -> FilterContext:
    return FilterContext(
        cfg=cfg if cfg is not None else Config(),
        recent_replies=recent_replies or [],
        context_rows=context_rows or [],
        places_names=places_names or [],
        participant_names=participant_names or [],
        bot_names=bot_names or [],
        muted_names=muted_names or [],
        system_prompt=system_prompt,
        trigger_text=trigger_text,
        now=now,
    )


_DEFAULT_CTX = _ctx()

_PROMPT_WITH_SLOT = (
    "Ты живёшь своей жизнью параллельно и иногда сообщаешь об этом чату. Примеры: {few_shot}"
)

# --------------------------------------------------------------------------- #
# Основная таблица: (id, text, ctx, expected_reason_or_None)
#
# expected_reason_or_None:
#   None  -> ожидается полный pass (ok=True, reason="pass", reasons=())
#   str   -> ожидается ok=False и эта причина среди verdict.reasons
# --------------------------------------------------------------------------- #

CASES: list[tuple[str, str, FilterContext, str | None]] = [
    # --- голос: длина / разметка / эмодзи / предложения (приёмка, таблица голоса) ---
    ("voice_pass_pivo", "Пиво хорошее, я не спорю.", _DEFAULT_CTX, None),
    # regex:sentences: фрагмент между [.!?…] считается предложением только от 3 слов
    # (CLAUDE.md, "Интерфейсы этапа 4") — рубленая байка из коротких фраз проходит,
    # лекция из трёх полноценных предложений — нет.
    (
        "sentences_three_long_cut",
        "Сегодня ездил в магазин рано утром. Купил хлеба и молока там. "
        "Вечером буду жарить яичницу.",
        _DEFAULT_CTX,
        "regex:sentences",
    ),
    (
        "sentences_short_fragments_pass",
        "У нас в девяносто восьмом тоже переносили. Дважды. "
        "Потом контору закрыли, и вопрос снялся.",
        _DEFAULT_CTX,
        None,
    ),
    ("sentences_two_short_pass", "Бот, бот. Ты обедал?", _DEFAULT_CTX, None),
    (
        "markdown_list",
        "Вот план:\n- сходи в магазин\n- купи хлеб",
        _DEFAULT_CTX,
        "regex:markdown",
    ),
    (
        "markdown_numbered",
        "1. Первое дело\n2. Второе дело",
        _DEFAULT_CTX,
        "regex:markdown",
    ),
    ("markdown_bold", "Это **очень** важно поверь мне.", _DEFAULT_CTX, "regex:markdown"),
    ("markdown_header", "# Важно: не открывай дверь чужим.", _DEFAULT_CTX, "regex:markdown"),
    ("markdown_codeblock", "```тут код``` не открывай.", _DEFAULT_CTX, "regex:markdown"),
    # 🚀 вне filters.allowed_emoji -> по-прежнему режется, даже не в конце реплики
    ("emoji", "Привет всем сегодня 🚀 хорошо.", _DEFAULT_CTX, "regex:emoji"),
    ("length_301", "ы" * 301, _DEFAULT_CTX, "regex:length"),
    ("length_boundary_300_pass", "ы" * 300, _DEFAULT_CTX, None),
    # --- разрешённые эмодзи (решение владельца, CHARACTER.md раздел 3/4) ---
    ("emoji_allowed_end_pass", "Бывает 🙂", _DEFAULT_CTX, None),
    ("emoji_allowed_after_dot_pass", "Бывает. 💩", _DEFAULT_CTX, None),
    ("emoji_allowed_thumbsup_skin_tone_pass", "Бывает 👍🏻", _DEFAULT_CTX, None),
    ("emoji_count_two_cut", "Бывает 🙂🙂", _DEFAULT_CTX, "style:emoji_count"),
    # style:emoji_position удалено (владелец: эмодзи не обязано быть в конце,
    # postprocess.py правит частоту/количество до фильтра, а не позицию).
    ("emoji_start_pass", "🙂 Бывает", _DEFAULT_CTX, None),
    ("emoji_disallowed_cut", "Бывает 🚀", _DEFAULT_CTX, "regex:emoji"),
    (
        "emoji_freq_within_window_cut",
        "Бывает 🙂",
        _ctx(recent_replies=["Все по домам.", "Бывает.", "Ладно.", "Зря 😂"]),
        "style:emoji_freq",
    ),
    (
        "emoji_freq_fifth_back_pass",
        "Бывает 🙂",
        _ctx(
            recent_replies=[
                "Зря 😂",
                "Все по домам разошлись.",
                "Ладно, я в гараже.",
                "Купил торф для рассады.",
                "Спокойной ночи всем.",
            ]
        ),
        None,
    ),
    ("emoji_none_no_style_triggers", "Бывает, с кем не случается.", _DEFAULT_CTX, None),
    # --- начинается с имени ---
    (
        "starts_name_participant",
        "Дима, ты не прав.",
        _ctx(participant_names=["Дима"]),
        "regex:starts_name",
    ),
    (
        "starts_name_bot",
        "Федя, ты как обычно опоздал.",
        _ctx(bot_names=["Федя"]),
        "regex:starts_name",
    ),
    (
        "starts_name_pass",
        "Ну ты даёшь, вот это да.",
        _ctx(participant_names=["Дима"]),
        None,
    ),
    # --- телефон ---
    ("phone_with_plus", "Вот его номер +48 512 345 678, звони давай.", _DEFAULT_CTX, "regex:phone"),
    (
        "phone_no_plus",
        "Возьми 5123456789, запиши куда-то.",
        _DEFAULT_CTX,
        "regex:phone",
    ),
    ("phone_pass_year", "Было это в 2020 году, ещё до переезда.", _DEFAULT_CTX, None),
    # --- выдуманные заведения ---
    ("venue_bar_nowy", "Сходите в Bar Nowy сегодня вечером.", _DEFAULT_CTX, "regex:venue"),
    (
        "venue_lalka_whitelisted",
        "Сходите в LALKA сегодня.",
        _ctx(places_names=["LALKA"]),
        None,
    ),
    ("venue_lidl_whitelist", "Заходил в Lidl за хлебом.", _DEFAULT_CTX, None),
    ("venue_olx_whitelist", "Продал колёса на OLX вчера.", _DEFAULT_CTX, None),
    (
        "venue_dzialka_lowercase_pass",
        "На działka был весь день, помидоры зреют.",
        _DEFAULT_CTX,
        None,
    ),
    # --- known_places / polish_words (CLAUDE.md, правки этапа 4) ---
    (
        "venue_known_place_lalka_empty_places_names_pass",
        "Сходите в LALKA на Жечицах.",
        _DEFAULT_CTX,  # places_names пуст — LALKA из filters.known_places
        None,
    ),
    (
        "venue_polish_word_dzialka_capitalized_pass",
        "Działka заросла совсем.",
        _DEFAULT_CTX,
        None,
    ),
    (
        "venue_polish_word_urzad_capitalized_pass",
        "Ходили в Urząd с соседом.",
        _DEFAULT_CTX,
        None,
    ),
    (
        "latin_polish_words_two_in_row_pass",
        "Мне przegląd zrobiony, машина в порядке.",
        _DEFAULT_CTX,
        None,
    ),
    (
        "venue_unknown_bar_still_cut",
        "Bar Nowy — новое место, я туда ни ногой.",
        _DEFAULT_CTX,
        "regex:venue",
    ),
    # --- латиница ---
    ("latin_english", "Sure, let me help with that.", _DEFAULT_CTX, "regex:latin"),
    ("latin_single_word_pass", "Заказал item вчера, всё пришло.", _DEFAULT_CTX, None),
    # --- стоп-лист тем на выходе ---
    (
        "topic_output_russia",
        "Ладно проехали, но вот в России сейчас всё сложно.",
        _DEFAULT_CTX,
        "regex:topic",
    ),
    ("topic_pass_torf", "Купил торф для рассады на выходных.", _DEFAULT_CTX, None),
    # --- замьюченные ---
    (
        "muted_name",
        "Дима опять всех подначивает, вот умора.",
        _ctx(muted_names=["Дима"]),
        "regex:muted_name",
    ),
    (
        "muted_name_pass",
        "Все разошлись по домам довольно быстро.",
        _ctx(muted_names=["Дима"]),
        None,
    ),
    # --- эхо ---
    (
        "echo_4_words",
        "Я знаю нормального электрика в городе, но сам не звонил.",
        _ctx(context_rows=[_row("Аня", "подскажите нормального электрика в городе")]),
        "regex:echo",
    ),
    (
        "echo_3_words_pass",
        "Я тоже нормального электрика в поисках, но не нашёл.",
        _ctx(context_rows=[_row("Аня", "нормального электрика в городе ищу")]),
        None,
    ),
    # --- утечка промпта: только инструктивная часть ("- ..." и строки с "JSON"),
    # не биография (CLAUDE.md, правки этапа 4) ---
    (
        "prompt_leak_bio_paraphrase_pass",
        # Перефраз биографии ("Пьёшь мало, одно пиво за вечер...", prompts/system.txt) —
        # не булет и не про JSON, поэтому не считается утечкой, даже пересказанная
        # почти дословно.
        "Пьёшь мало, одно пиво за вечер, и хватит.",
        _ctx(system_prompt=SYSTEM_PROMPT),
        None,
    ),
    (
        "prompt_leak_bullet_six_words_cut",
        # 6 слов подряд из булета "- Не даёшь советов по делу и не объясняешь...".
        "Не даёшь советов по делу и точка.",
        _ctx(system_prompt=SYSTEM_PROMPT),
        "regex:prompt_leak",
    ),
    (
        "prompt_leak_synthetic_slot_pass",
        "Ну и ладно, у меня свои дела сегодня.",
        _ctx(system_prompt=_PROMPT_WITH_SLOT),
        None,
    ),
    # --- маркеры модели ---
    (
        "model_talk",
        "Я тебе отвечаю как языковая модель, без затей.",
        _DEFAULT_CTX,
        "regex:model_talk",
    ),
    ("model_talk_pass", "Я тебе отвечу, но не сегодня.", _DEFAULT_CTX, None),
    # --- дубликаты (слой 2, Жаккар) ---
    (
        "jaccard_duplicate",
        "Знаю, он по телефону не разговаривает, его ловить надо, такой персонаж.",
        _ctx(
            recent_replies=[
                "Знаю. Он по телефону не разговаривает, его ловить надо, такой человек."
            ]
        ),
        "dedup:jaccard",
    ),
    (
        "jaccard_low_similarity_pass",
        "Даже не знаю, что сказать, но человек он в целом неплохой.",
        _ctx(
            recent_replies=[
                "Знаю. Он по телефону не разговаривает, его ловить надо, такой человек."
            ]
        ),
        None,
    ),
    (
        "jaccard_exact_short_duplicate",
        "Бывает.",
        _ctx(recent_replies=["Бывает."]),
        "dedup:jaccard",
    ),
    (
        "jaccard_short_different_pass",
        "Зря.",
        _ctx(recent_replies=["Бывает."]),
        None,
    ),
    # --- частота польского (слой 2) ---
    (
        "polish_freq_rule_b",
        "Ездил на przegląd машины, всё нормально.",
        _ctx(
            recent_replies=[
                "Все по домам разошлись.",
                "Бывает.",
                "На działka был, помидоры пошли.",
                "Ладно, я в гараже.",
            ]
        ),
        "dedup:polish_freq",
    ),
    (
        "polish_freq_fifth_back_pass",
        "Ездил на przegląd машины, всё нормально.",
        _ctx(
            recent_replies=[
                "На działka был, помидоры пошли.",
                "Все по домам разошлись.",
                "Бывает.",
                "Ладно, я в гараже.",
                "Спокойной ночи всем.",
            ]
        ),
        None,
    ),
    # --- самоповтор байки (слой 2, dedup:self_echo, CLAUDE.md правки этапа 4) ---
    (
        "self_echo_reworded_anekdote_cut",
        "я его дома пью, одну бутылку",
        _ctx(recent_replies=["пью одно пиво за вечер, дома, одну бутылку"]),
        "dedup:self_echo",
    ),
    (
        "self_echo_stopwords_only_pass",
        "И не то что я против, просто устал.",
        _ctx(recent_replies=["Ну и не то что тут скажешь."]),
        None,
    ),
    (
        "self_echo_three_common_words_pass",
        "рыбу ловил утром вчера",
        _ctx(recent_replies=["рыбу ловил утром сегодня"]),
        None,
    ),
    # --- маркеры ассистента и стиль ---
    ("style_assistant", "Конечно! Дальше сам разберёшься.", _DEFAULT_CTX, "style:assistant"),
    ("style_assistant_pass", "Не советую, но дело твое.", _DEFAULT_CTX, None),
    # --- маркеры сухости/раздражения (style:grumpy, CHARACTER.md раздел 3, warm-tone) ---
    (
        "style_grumpy_ya_zhe_napisal",
        "На жене. Тридцать лет уже, я же написал.",
        _DEFAULT_CTX,
        "style:grumpy",
    ),
    (
        "style_grumpy_nikomu_ne_interesno",
        "Название говорить не буду, никому не интересно",
        _DEFAULT_CTX,
        "style:grumpy",
    ),
    (
        "style_grumpy_pass",
        "Женат, тридцать лет. Жена до сих пор удивляется.",
        _DEFAULT_CTX,
        None,
    ),
    # --- пивные бренды из polish_words не режутся venue/latin (CHARACTER.md раздел 1/4) ---
    (
        "venue_lech_latin_polish_word_pass",
        "Пью Lech, он подешевле.",
        _DEFAULT_CTX,
        None,
    ),
    (
        "style_question_x2",
        "А ты как думаешь?",
        _ctx(recent_replies=["Ты серьёзно?"]),
        "style:question_x2",
    ),
    (
        "style_question_x2_pass",
        "А ты как думаешь?",
        _ctx(recent_replies=["Бывает."]),
        None,
    ),
    ("style_exclaim", "Ну ты даёшь!! Вот это да.", _DEFAULT_CTX, "style:exclaim"),
    ("style_exclaim_pass", "Ну и ладно!", _DEFAULT_CTX, None),
]


@pytest.mark.parametrize("case_id, text, ctx, expected", CASES, ids=[c[0] for c in CASES])
async def test_check_output_table(
    case_id: str, text: str, ctx: FilterContext, expected: str | None
) -> None:
    verdict = await check_output(text, ctx)
    if expected is None:
        assert verdict == FilterVerdict(ok=True, reason="pass", reasons=())
    else:
        assert verdict.ok is False
        assert expected in verdict.reasons
        assert verdict.reason == verdict.reasons[0]


# --------------------------------------------------------------------------- #
# reasons содержит все сработавшие причины сразу
# --------------------------------------------------------------------------- #


async def test_check_output_collects_all_matching_reasons() -> None:
    # 🚀 вне filters.allowed_emoji -> regex:emoji по-прежнему срабатывает.
    text = "- Сделай так\n- И вот так тоже 🚀"
    verdict = await check_output(text, _DEFAULT_CTX)
    assert verdict.ok is False
    assert verdict.reasons == ("regex:markdown", "regex:emoji")
    assert verdict.reason == "regex:markdown"


# --------------------------------------------------------------------------- #
# check_output + судья (слой 3)
# --------------------------------------------------------------------------- #


class _FakeJudge:
    def __init__(self, reasons: list[str]) -> None:
        self.called = False
        self._reasons = reasons

    async def check(self, *, candidate: str, trigger_text: str, now: int) -> list[str]:
        self.called = True
        return list(self._reasons)


async def test_judge_called_when_layers_clean() -> None:
    judge = _FakeJudge([])
    verdict = await check_output("Пиво хорошее, я не спорю.", _DEFAULT_CTX, judge=judge)
    assert judge.called is True
    assert verdict.ok is True


async def test_judge_not_called_when_dirty_and_shadow_false() -> None:
    cfg = Config()
    cfg.filters.shadow = False
    ctx = _ctx(cfg=cfg)
    judge = _FakeJudge(["judge:risky"])
    verdict = await check_output("ы" * 301, ctx, judge=judge)
    assert judge.called is False
    assert verdict.ok is False
    assert verdict.reason == "regex:length"
    assert "judge:risky" not in verdict.reasons


async def test_judge_called_and_reasons_added_when_dirty_and_shadow_true() -> None:
    cfg = Config()
    cfg.filters.shadow = True
    ctx = _ctx(cfg=cfg)
    judge = _FakeJudge(["judge:risky"])
    verdict = await check_output("ы" * 301, ctx, judge=judge)
    assert judge.called is True
    assert verdict.ok is False
    assert verdict.reason == "regex:length"
    assert "judge:risky" in verdict.reasons


# --------------------------------------------------------------------------- #
# Обратная совместимость: старый 4-полевой вызов FilterContext (responder.py
# пока собирает контекст только из cfg/recent_replies/context_rows/places_names,
# остальные поля обновит другой агент вместе с responder.py).
# --------------------------------------------------------------------------- #


async def test_filter_context_accepts_legacy_four_field_call() -> None:
    ctx = FilterContext(cfg=Config(), recent_replies=[], context_rows=[], places_names=[])
    verdict = await check_output("Пиво хорошее, я не спорю.", ctx)
    assert verdict.ok is True
    assert ctx.participant_names == []
    assert ctx.bot_names == []
    assert ctx.muted_names == []
    assert ctx.patterns is None
    assert ctx.system_prompt == ""
    assert ctx.trigger_text == ""
    assert ctx.now == 0


# --------------------------------------------------------------------------- #
# Регрессия слоя 1-2: ни один канонический пример few_shot.yaml со speak=true не
# должен резаться выходным фильтром. Если новая правка правила ловит собственные
# примеры голоса персонажа — правило слишком грубое (CLAUDE.md, правки этапа 4).
# --------------------------------------------------------------------------- #


async def test_all_few_shot_speak_true_examples_pass_check_output() -> None:
    items = load_few_shot(REPO_ROOT / "few_shot.yaml")
    speak_items = [item for item in items if item.speak]
    assert speak_items, "few_shot.yaml должен содержать хотя бы один пример speak=true"

    cfg = Config()
    participant_names = [item.name for item in items]
    bot_names = [cfg.persona.name, cfg.persona.display_name, *cfg.persona.name_triggers]

    failures: list[str] = []
    for item in speak_items:
        ctx = _ctx(
            cfg=cfg,
            participant_names=participant_names,
            bot_names=bot_names,
            system_prompt=SYSTEM_PROMPT,
            trigger_text=item.user,
        )
        verdict = await check_output(item.text, ctx)
        if not verdict.ok:
            failures.append(f"{item.name!r}: {item.text!r} -> {verdict.reasons}")

    assert not failures, "few_shot.yaml примеры срезаны выходным фильтром:\n" + "\n".join(failures)
