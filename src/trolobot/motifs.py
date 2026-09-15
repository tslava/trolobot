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
from collections.abc import Mapping, Sequence

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
