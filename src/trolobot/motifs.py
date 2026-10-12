"""Реквизит персонажа и квота баек (CLAUDE.md, "меньше и разнообразнее", мера 5).

Диагноз владельца после стопа 15.09: бот не только слишком много говорил, но и
говорил одно и то же — в 22 репликах жена встретилась 7 раз, теплица 5, гараж 5,
«в девяносто пятом» 5, и почти каждая реплика была байкой. Лечится с двух концов:

- **до модели** — ``used_motifs``/``story_count``/``render_avoid`` собирают из уже
  сказанного абзац для слота ``{avoid}`` системного промпта («ты уже поминал жену,
  теплицу, гараж — сейчас без них»), то есть модель предупреждена заранее;
- **после модели** — ``motifs_in``/``story_count`` используются выходным фильтром
  (``filters.layer_rules``: ``dedup:motif`` и ``style:story_quota``) как страховка,
  если предупреждение не сработало.

Функции чистые, без I/O и без зависимости от конфига: скомпилированные регулярки
приходят готовыми из ``patterns.Patterns`` (``motifs``/``story_markers``), окна —
числами из ``FiltersConfig``. Записи стикеров («[стикер #N] надпись», их пишет
``responder.py`` через ``insert_bot_reply``) при подсчёте пропускаются: это не
реплика персонажа, а картинка, и мотивов в ней нет — тот же приём, что в
``postprocess._recent_has_emoji``, включая собственную константу префикса, чтобы
не тянуть сюда ``stickers.py`` с его ``llm``-зависимостями.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from trolobot.gate_types import Trigger

# "[стикер #3] Ну ты даёшь" в recent_bot_replies — картинка, а не текст персонажа
# (см. докстринг модуля). Дублирует stickers.STICKER_TAG_RE намеренно.
_STICKER_PLACEHOLDER_PREFIX = "[стикер #"

# Метки мотивов склоняются в винительный падеж для фразы «ты уже поминал: жену,
# теплицу, гараж». Метки, которых тут нет, идут как есть — конфиг открыт для
# правки владельцем (/set filters.motifs), и незнакомая метка не должна ломать
# промпт, максимум звучать чуть коряво.
_ACCUSATIVE = {
    "жена": "жену",
    "теплица": "теплицу",
    "машина": "машину",
    "сын": "сына",
    "зато": "«зато»",
}

_AVOID_TEMPLATE = (
    "В последних репликах ты уже поминал: {items}. "
    "Сейчас без них: другая деталь или вовсе без байки."
)
_AVOID_NO_STORY = "Байку сейчас не рассказывай: короткий ответ по делу, одна фраза."


def _is_sticker_record(text: str) -> bool:
    return text.startswith(_STICKER_PLACEHOLDER_PREFIX)


def _tail(recent_replies: Sequence[str], window: int) -> list[str]:
    """Последние ``window`` реплик без записей-стикеров.

    Окно отсчитывается по всем записям, а стикеры отбрасываются уже внутри него
    (как в ``postprocess._recent_has_emoji``): «последние 10 реплик» — это ровно
    последние 10 строк ``db.recent_bot_replies``, часть из которых может оказаться
    стикерами и в подсчёт не попасть.
    """
    if window <= 0:
        return []
    return [reply for reply in recent_replies[-window:] if not _is_sticker_record(reply)]


def motifs_in(text: str, motifs: Mapping[str, Sequence[re.Pattern[str]]]) -> list[str]:
    """Метки мотивов, встретившихся в тексте, в порядке объявления в конфиге."""
    if not text:
        return []
    return [
        label
        for label, patterns in motifs.items()
        if any(pattern.search(text) for pattern in patterns)
    ]


def used_motifs(
    recent_replies: Sequence[str], window: int, motifs: Mapping[str, Sequence[re.Pattern[str]]]
) -> list[str]:
    """Метки мотивов из последних ``window`` реплик, без повторов, в порядке конфига.

    ``recent_replies`` — хронологически, свежие последними (как отдаёт
    ``db.recent_bot_replies``).
    """
    tail = _tail(recent_replies, window)
    if not tail:
        return []
    used: list[str] = []
    for label, patterns in motifs.items():
        if any(pattern.search(reply) for reply in tail for pattern in patterns):
            used.append(label)
    return used


def story_count(
    recent_replies: Sequence[str], window: int, markers: Sequence[re.Pattern[str]]
) -> int:
    """Сколько из последних ``window`` реплик выглядят байкой (есть маркер истории).

    Считаются реплики, а не срабатывания: две «девяностых» в одной фразе — одна
    байка, а не две.
    """
    tail = _tail(recent_replies, window)
    return sum(1 for reply in tail if any(marker.search(reply) for marker in markers))


def render_avoid(used: Sequence[str], *, no_story: bool) -> str:
    """Абзац для слота ``{avoid}`` системного промпта. Нечего сказать -> "".

    ``used`` — метки мотивов из ``used_motifs``; ``no_story`` — квота баек в окне
    уже выбрана (``story_count >= filters.story_max``).
    """
    parts: list[str] = []
    if used:
        items = ", ".join(_ACCUSATIVE.get(label, label) for label in used)
        parts.append(_AVOID_TEMPLATE.replace("{items}", items))
    if no_story:
        parts.append(_AVOID_NO_STORY)
    return " ".join(parts)


# Метрика «про себя» (CLAUDE.md, "про себя через раз"): после 0.6.0 7 из 7 непрошеных
# реплик были про самого Фёдора. Ничего не режет — только строка в /status, чтобы
# видеть тренд после правок промпта. Обращения считаются отдельно: на вопрос про
# него говорить о себе нормально. /say (дословный текст владельца) и /life (новость
# о себе по определению) не считаются вовсе, стикеры — тоже.
_SELF_ADDRESS_TRIGGERS = frozenset(
    {
        Trigger.MENTION.value,
        Trigger.REPLY.value,
        Trigger.NAME.value,
        Trigger.FOLLOWUP.value,
        "places",
    }
)
_SELF_SKIP_TRIGGERS = frozenset({"say", "life"})


@dataclass(frozen=True, slots=True)
class SelfShare:
    """Сколько реплик «про себя» из скольких, по группам триггеров."""

    ambient: tuple[int, int] = (0, 0)
    address: tuple[int, int] = (0, 0)
    other: tuple[int, int] = (0, 0)


def speaks_about_self(text: str, markers: Sequence[re.Pattern[str]]) -> bool:
    """Есть ли в реплике маркер «о себе» (первое лицо, его родня). Стикер — нет."""
    if not text or _is_sticker_record(text):
        return False
    return any(marker.search(text) for marker in markers)


def self_share(replies: Iterable[tuple[str, str]], markers: Sequence[re.Pattern[str]]) -> SelfShare:
    """``replies`` — пары (trigger, text) из ``bot_replies``. Группы: ambient,
    обращения (mention/reply/name/followup/places), прочее (morning, checkin,
    callback, spontaneous). say/life и стикеры пропускаются."""
    counts = {"ambient": [0, 0], "address": [0, 0], "other": [0, 0]}
    for trigger, text in replies:
        if trigger in _SELF_SKIP_TRIGGERS or _is_sticker_record(text):
            continue
        if trigger == Trigger.AMBIENT.value:
            group = "ambient"
        elif trigger in _SELF_ADDRESS_TRIGGERS:
            group = "address"
        else:
            group = "other"
        counts[group][1] += 1
        if speaks_about_self(text, markers):
            counts[group][0] += 1
    return SelfShare(
        ambient=(counts["ambient"][0], counts["ambient"][1]),
        address=(counts["address"][0], counts["address"][1]),
        other=(counts["other"][0], counts["other"][1]),
    )


def render_self_share(share: SelfShare, days: int) -> str:
    """Строка для /status: «о себе за 3 сут: ambient 6/6, обращения 2/2, прочее 1/1»."""
    return (
        f"о себе за {days} сут: ambient {share.ambient[0]}/{share.ambient[1]}, "
        f"обращения {share.address[0]}/{share.address[1]}, "
        f"прочее {share.other[0]}/{share.other[1]}"
    )
