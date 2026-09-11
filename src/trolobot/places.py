"""Рантайм-выбор заведений (PLAN.md, этап 5; CLAUDE.md, "Интерфейсы этапа 5").

Чистые синхронные функции без I/O — наполнение кэша (Google Places, LLM-сжатие
отзывов) делает отдельный офлайн-скрипт (``places_fill.py``, другой агент),
этот модуль только читает уже готовые ``db.PlaceRow`` и решает, что подмешать
в промпт по тексту сообщения-триггера:

- ``select_places`` / ``render_places_block`` — старый путь (regex-детект +
  фильтрованный список из 1-2 мест); оставлены для ``replay.py`` и тестов,
  ``responder._generate_and_send`` их больше не зовёт;
- ``render_places_menu`` — рантайм-путь при прямом обращении: весь кэш целиком,
  решение «спрашивали ли про место» и выбор 1-2 подходящих отдаёт модели (решение
  владельца после живого теста — regex не покрывает «колись где пиво нормальное»);
- ``validate_fact`` — та же валидация, что ``places_fill.py`` прогоняет по
  сжатому отзыву перед записью в БД. Отзыв — чужой текст из интернета, канал
  непрямой инъекции (PLAN.md, этап 5, п.3): команда в отзыве может проехать
  через LLM-сжатие в fact и оттуда в каждый промпт про места, поэтому фильтр
  строгий (только кириллица, пробелы, запятая, дефис, до 40 символов) и это
  последняя автоматическая защита перед ручным просмотром.
"""

from __future__ import annotations

import random
import re

from trolobot.config_models import PlacesConfig
from trolobot.db import PlaceRow
from trolobot.prompt import PLACES_NONE

# Районы (CHARACTER.md, раздел 7) — канонические имена и их русские/польские
# варианты написания. Сравнение всегда без учёта регистра и ё/е (_normalize).
_DISTRICT_VARIANTS: dict[str, list[str]] = {
    "Wilda": ["wilda", "вильда"],
    "Jeżyce": ["jeżyce", "jezyce", "ежице", "ежицы"],
    "Stare Miasto": ["stare miasto", "старый город", "старе място", "центр", "centrum"],
    "Grunwald": ["grunwald", "грюнвальд"],
    "Łazarz": ["łazarz", "lazarz", "лазарж"],
    "Rataje": ["rataje", "ратае"],
    "Winogrady": ["winogrady", "винограды"],
    "Kórnik": ["kórnik", "kornik", "курник", "кёрник"],
    "Puszczykowo": ["puszczykowo", "пущиково"],
    "Strzeszyn": ["strzeszyn", "стшешин", "стржешин"],
}

# «Тихо/спокойно/поговорить/посидеть/не орать» -> quiet=1 (CLAUDE.md, "Интерфейсы этапа 5").
_QUIET_KEYWORDS = ["тихо", "спокойно", "поговорить", "посидеть", "не орать"]
# «За город/съездить/природа/озеро/лес» -> category outskirts.
_OUTSKIRTS_KEYWORDS = ["за город", "съездить", "природа", "озеро", "лес"]
_OUTSKIRTS_CATEGORY = "outskirts"

_FACT_MAX_LEN = 40
_FACT_RE = re.compile(r"^[а-яёА-ЯЁ ,\-]+$")


def _normalize(text: str) -> str:
    """casefold + ё->е, для сравнения без учёта регистра, принятого в контракте."""
    return text.strip().casefold().replace("ё", "е")


def _build_district_lookup() -> dict[str, str]:
    lookup: dict[str, str] = {}
    for canonical, variants in _DISTRICT_VARIANTS.items():
        lookup[_normalize(canonical)] = canonical
        for variant in variants:
            lookup[_normalize(variant)] = canonical
    return lookup


_DISTRICT_LOOKUP = _build_district_lookup()


def _district_group(name: str) -> str | None:
    """Канонический район для произвольного написания (PlaceRow.district), либо None."""
    return _DISTRICT_LOOKUP.get(_normalize(name))


def _detect_district(request_text: str) -> str | None:
    """Первый упомянутый в тексте район (любой вариант написания), либо None."""
    normalized = _normalize(request_text)
    for variant, canonical in _DISTRICT_LOOKUP.items():
        if variant and variant in normalized:
            return canonical
    return None


def _mentions_any(request_text: str, keywords: list[str]) -> bool:
    normalized = _normalize(request_text)
    return any(_normalize(keyword) in normalized for keyword in keywords)


def select_places(
    rows: list[PlaceRow], cfg: PlacesConfig, request_text: str, rng: random.Random
) -> list[PlaceRow]:
    """Фильтрует ``rows`` по тексту запроса и выбирает до ``cfg.max_per_reply``.

    Порядок (CLAUDE.md, "Интерфейсы этапа 5"):

    1. База: ``operational``, ``rating >= cfg.min_rating``, ``reviews >= cfg.min_reviews``.
    2. Если в запросе слова про тишину — жёсткий фильтр ``quiet=1`` (пусто, если
       среди оставшихся нет ни одного тихого).
    3. Если упомянут район — он в приоритете: сужаем до совпадений по району,
       но только если такие совпадения есть, иначе (район в кэше не встретился)
       используем то, что было до этого шага, а не пустой список.
    4. Иначе, если в запросе слова про выезд за город — жёсткий фильтр
       ``category == "outskirts"``.
    5. Иначе — любая категория.

    Из итогового набора — ``rng.sample`` до ``max_per_reply`` (или меньше, если
    подходящих не хватает). Пустой список на любом шаге -> ``[]``.
    """
    candidates = [
        row
        for row in rows
        if row.operational and row.rating >= cfg.min_rating and row.reviews >= cfg.min_reviews
    ]
    if not candidates:
        return []

    if _mentions_any(request_text, _QUIET_KEYWORDS):
        candidates = [row for row in candidates if row.quiet]
        if not candidates:
            return []

    district = _detect_district(request_text)
    if district is not None:
        district_matches = [row for row in candidates if _district_group(row.district) == district]
        if district_matches:
            candidates = district_matches
    elif _mentions_any(request_text, _OUTSKIRTS_KEYWORDS):
        candidates = [row for row in candidates if row.category == _OUTSKIRTS_CATEGORY]
        if not candidates:
            return []

    if not candidates:
        return []

    k = min(cfg.max_per_reply, len(candidates))
    if k <= 0:
        return []
    return rng.sample(candidates, k)


def render_places_block(rows: list[PlaceRow]) -> str:
    """Блок ``{places}``: название, район, категория, факт. Пусто -> prompt.PLACES_NONE.

    Часы работы не выводятся никогда (их нет в PlaceRow), источник (Google) не
    упоминается — "был, ничего особенного", а не "по отзывам в Гугле".
    """
    if not rows:
        return PLACES_NONE
    lines = ["Заведения, о которых ты можешь сказать (ты там бывал, других не называешь):"]
    for row in rows:
        parts = [row.name, row.district, row.category]
        if row.fact:
            parts.append(row.fact)
        lines.append("- " + ", ".join(parts))
    return "\n".join(lines)


_MENU_HEADER = (
    "Заведения, где ты бывал (только они, других не называешь). Если про место "
    "НЕ спрашивали — не упоминай ни одно. Если спросили — назови одно, максимум "
    "два подходящих по тишине и району, без часов работы и без цен:"
)


def render_places_menu(rows: list[PlaceRow]) -> str:
    """Весь кэш заведений для промпта при прямом обращении (mention/reply/name).

    Решение владельца после живого теста (CLAUDE.md, "Интерфейсы этапа 5"): детект
    запроса про место регулярками не покрывает живую речь («колись где пиво
    нормальное», «есть что-то тихое на Ежицах?»), поэтому при любом прямом
    обращении список уходит в промпт целиком, а решение «спрашивали ли про место»
    принимает модель по инструкции в этом же блоке — не ``select_places``.
    Регулярки (``patterns.places_request``) остаются для статистики и ambient.

    Пусто -> ``prompt.PLACES_NONE``. Иначе строки по ``name``, ``тихо``/``шумно``
    по ``quiet``, ``fact`` — только если непустой. Никаких рейтингов, цен, часов
    работы, слова Google (источник не палится, как и в ``render_places_block``).
    """
    if not rows:
        return PLACES_NONE
    lines = [_MENU_HEADER]
    for row in sorted(rows, key=lambda r: r.name):
        parts = [row.name, row.district, row.category, "тихо" if row.quiet else "шумно"]
        if row.fact:
            parts.append(row.fact)
        lines.append("- " + ", ".join(parts))
    return "\n".join(lines)


def validate_fact(raw: str) -> str | None:
    """Только кириллица, пробелы, запятая, дефис; <= 40 символов; иначе None.

    Отзыв Google — недоверенный внешний текст (PLAN.md, этап 5, п.3): латиница,
    цифры и любая другая пунктуация валят факт целиком, а не только вырезаются
    из него — частичная очистка оставила бы шанс протащить инъекцию обрывками.
    """
    text = raw.strip()
    if not text or len(text) > _FACT_MAX_LEN:
        return None
    if not _FACT_RE.fullmatch(text):
        return None
    return text
