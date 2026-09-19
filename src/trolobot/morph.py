"""Падежные формы слова-обращения (CLAUDE.md, "Интерфейсы: имя в падежах и
проверка обращения по имени").

Два живых случая 19.09.2026 показали, что грубая регулярка ``\\bфедор\\b`` имя в
падежах не ловит вовсе: «играю с внуком Федора» бот не услышал (``gate:not_live``),
хотя обратились почти к нему. Здесь — первый слой лечения: имя ищется во всех
падежах единственного числа. Второй слой (обращаются или просто говорят о нём)
живёт в ``followup.FollowupChecker.check_name`` — регулярка про это не знает
ничего и знать не должна.

Главное требование — отсутствие ложных срабатываний: «федерация», «дедлайн»,
«Федяев», «отечество», «батюшкам» не должны совпадать ни с одним триггером.
Поэтому окончания — закрытый список морфем, а не открытый стем: после стема
обязана идти ровно одна из перечисленных морфем и граница слова.

Модуль чистый: без I/O и без зависимостей от других модулей проекта. Буква «ё»
здесь не нормализуется — вызывающий (``persona.name_triggers``) держит и «фёдор»,
и «федор», и формы генерируются для каждого триггера отдельно.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Согласные кириллицы: «ь» и «ъ» сюда не входят намеренно — слово на мягкий знак
# («федь») склоняется не по этому образцу, для него остаётся точное совпадение.
_CONSONANTS = frozenset("бвгджзйклмнпрстфхцчшщ")

# Окончания по образцам склонения. Пустая строка в _CONSONANT_ENDINGS — сама
# именительная форма («федор»), она же делает группу в регулярке необязательной.
_EC_ENDINGS = ("а", "у", "ом", "е")  # отец -> отца, отцу, отцом, отце
_KA_ENDINGS = ("а", "и", "е", "у", "ой")  # батюшка -> батюшки, батюшке, батюшку, батюшкой
_YA_ENDINGS = ("я", "и", "е", "ю", "ей")  # федя -> феди, феде, федю, федей
_A_ENDINGS = ("а", "ы", "е", "у", "ой")
_CONSONANT_ENDINGS = ("", "а", "у", "ом", "е", "ы")  # федор -> федора, федору, федором, ...


@dataclass(frozen=True, slots=True)
class _Declension:
    """Разбор слова на стем и закрытый список окончаний.

    ``keep_word`` — нужно ли считать само слово отдельной формой: у беглой
    гласной («отец» -> «отц-») именительный падеж стемом не покрывается.
    """

    stem: str
    endings: tuple[str, ...]
    keep_word: bool


def _analyze(word: str) -> _Declension | None:
    """Образец склонения для слова или None, если склонять его нечем (мягкий знак,
    «отче», латиница, что угодно ещё) — тогда остаётся точное совпадение."""
    if not word:
        return None
    if len(word) > 2 and word.endswith("ец"):
        return _Declension(stem=word[:-2] + "ц", endings=_EC_ENDINGS, keep_word=True)
    if len(word) > 2 and word.endswith("ка"):
        return _Declension(stem=word[:-1], endings=_KA_ENDINGS, keep_word=False)
    if len(word) > 1 and word.endswith("я"):
        return _Declension(stem=word[:-1], endings=_YA_ENDINGS, keep_word=False)
    if len(word) > 1 and word.endswith("а"):
        return _Declension(stem=word[:-1], endings=_A_ENDINGS, keep_word=False)
    if word[-1] in _CONSONANTS:
        return _Declension(stem=word, endings=_CONSONANT_ENDINGS, keep_word=False)
    return None


def _group(endings: tuple[str, ...]) -> str:
    return "|".join(re.escape(ending) for ending in endings if ending)


def name_pattern(word: str) -> str:
    """Регулярка (строкой, без ``re.compile``) для слова-триггера во всех падежах
    единственного числа. Регистр не учитывается вызывающим через ``re.IGNORECASE``.

    Все части экранируются ``re.escape``: триггеры задаются в ``config.yaml`` и
    попасть туда может что угодно, а падать на кривой регулярке гейт не должен.
    """
    analysis = _analyze(word)
    if analysis is None:
        return rf"\b{re.escape(word)}\b"
    stem = re.escape(analysis.stem)
    group = _group(analysis.endings)
    if analysis.keep_word:
        return rf"\b({re.escape(word)}|{stem}({group}))\b"
    if "" in analysis.endings:
        return rf"\b{stem}({group})?\b"
    return rf"\b{stem}({group})\b"


def name_forms(word: str) -> list[str]:
    """Сами формы слова — то же множество, которое матчит ``name_pattern``.

    Нужны тестам (проверять список форм нагляднее, чем строку регулярки) и
    больше нигде: в рантайме ищется всегда регуляркой.
    """
    analysis = _analyze(word)
    if analysis is None:
        return [word]
    forms = [word] if analysis.keep_word else []
    forms += [analysis.stem + ending for ending in analysis.endings]
    seen: set[str] = set()
    unique: list[str] = []
    for form in forms:
        if form not in seen:
            seen.add(form)
            unique.append(form)
    return unique
