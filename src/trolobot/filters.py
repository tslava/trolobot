"""Выходной фильтр — заглушка этапа 3, наполняется слоями на этапе 4.

Интерфейс уже финальный (см. CLAUDE.md, "Интерфейсы этапа 3"), чтобы
``responder.py`` можно было писать и тестировать независимо от фильтра.
Этап 4 добавит три слоя: регулярки (длина, разметка, эхо, утечка промпта,
маркеры модели, телефон, выдуманные заведения), детерминированные правила
(дедуп по Жаккару, частота польского, маркеры ассистента, двойной вопрос)
и LLM-судью (in_character/risky/obeyed_user). Сейчас — всегда пропуск.
"""

from __future__ import annotations

from dataclasses import dataclass

from trolobot.config_models import Config
from trolobot.db import MessageRow


@dataclass(frozen=True, slots=True)
class FilterContext:
    cfg: Config
    recent_replies: list[str]
    context_rows: list[MessageRow]
    places_names: list[str]


@dataclass(frozen=True, slots=True)
class FilterVerdict:
    ok: bool
    reason: str


async def check_output(text: str, ctx: FilterContext) -> FilterVerdict:
    """Заглушка: всегда пропускает. Этап 4 наполнит слоями (см. докстринг модуля)."""
    return FilterVerdict(ok=True, reason="pass")
