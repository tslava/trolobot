"""Зрение на фото: снимок из чата превращается в описание (CLAUDE.md,
"Интерфейсы: зрение на фото").

Решение владельца: до сих пор фото в чате было просто «[фото]» — бот на них слеп
и в контексте у модели зияла дырка. Модель со зрением описывает снимок одной-двумя
фразами, и это описание становится текстом сообщения в БД (``[фото: ...]``), так что
дальше всё — гейт, followup, генерация ответа — работает с ним как с обычным текстом
и ничего больше про фото знать не должно.

Стоит денег, поэтому описывается не каждый снимок: только когда есть повод
(обращение или открытое горячее окно), иногда по кубику, и всегда под суточным
потолком со своим счётчиком ``vision_calls`` (отдельно от основного ``llm_calls``,
как у дешёвой проверки followup) — см. ``should_describe``.

Байты фото и их base64 никуда не сохраняются и в лог не попадают никогда: они
живут ровно один вызов ``describe`` и уходят в тело запроса к модели. В
``filter_log`` пишется только готовое описание.

Модуль ничего не знает про aiogram: размер фото описан структурным протоколом
``PhotoSizeLike`` (настоящий ``aiogram.types.PhotoSize`` подходит под него без
наследования), скачиванием занимается вызывающий (``bot.py``), в БД модуль не ходит.
"""

from __future__ import annotations

import base64
import logging
import random
import re
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from trolobot.config_models import Config, VisionConfig
from trolobot.llm import LLMClient, LLMError
from trolobot.sanitize import normalize_text

logger = logging.getLogger(__name__)

# Свой суточный счётчик попыток вместо общего llm_calls: описание фото не должно
# съедать бюджет вызовов основной модели (тот же приём, что followup._COUNTER_KEY).
_COUNTER_KEY = "vision_calls"

_PHOTO_PLACEHOLDER = "[фото]"
_CAPTION_EMPTY = "(без подписи)"
_ELLIPSIS = "…"

# Дублирует вырезание разделителей из sanitize.normalize_text/prompt.py/judge.py —
# на всякий случай ещё раз чистим и подпись, уходящую в промпт, и ответ модели.
_LT_RUN_RE = re.compile(r"<{3,}")
_GT_RUN_RE = re.compile(r">{3,}")

_SLOT_RE = re.compile(r"\{(caption)\}")


def _strip_fake_delimiters(text: str) -> str:
    return _GT_RUN_RE.sub(" ", _LT_RUN_RE.sub(" ", text))


class PhotoSizeLike(Protocol):
    """Структурный тип размера фото (``aiogram.types.PhotoSize`` подходит без
    наследования, как и ``SimpleNamespace``/dataclass в тестах).

    Атрибуты объявлены read-only свойствами, а не изменяемыми полями: модулю
    нужно только читать их, а read-only протокол принимает и обычный атрибут
    pydantic-модели, и настоящее property.
    """

    @property
    def file_id(self) -> str: ...
    @property
    def width(self) -> int: ...
    @property
    def height(self) -> int: ...


def pick_photo_size(sizes: Sequence[PhotoSizeLike], max_width: int) -> PhotoSizeLike | None:
    """Самый большой размер не шире ``max_width``; если все шире — самый маленький.

    Телеграм отдаёт ``message.photo`` по возрастанию размера, но полагаться на
    порядок не нужно — выбор идёт по ``width``. Смысл потолка: за разрешение выше
    примерно 1024 px модель со зрением берёт больше токенов, а описание от этого
    не становится лучше. Если даже самый мелкий превью шире потолка — берём его,
    описать снимок всё равно лучше, чем не описать.
    """
    if not sizes:
        return None
    fitting = [size for size in sizes if size.width <= max_width]
    if fitting:
        return max(fitting, key=lambda size: size.width)
    return min(sizes, key=lambda size: size.width)


def should_describe(
    *,
    addressed: bool,
    hot: bool,
    count_today: int,
    cfg: VisionConfig,
    rng: random.Random,
) -> bool:
    """Тратить ли вызов модели на это фото.

    Порядок (CLAUDE.md): enabled -> суточный потолок -> есть повод (обращение или
    открытое горячее окно) -> кубик. Кубик последним, чтобы ``rng`` тратился только
    когда всё остальное уже позволило описать — тесты с фиксированным seed так
    стабильнее (ровно как последний шаг гейта и ``pick_reaction``).
    """
    if not cfg.enabled:
        return False
    if count_today >= cfg.daily_cap:
        return False
    if addressed or hot:
        return True
    return rng.random() < cfg.ambient_probability


def _truncate(text: str, max_chars: int) -> str:
    """Обрезка по границе слова с «…»; длина результата не превышает ``max_chars``."""
    if len(text) <= max_chars:
        return text
    truncated = text[: max_chars - 1]
    if " " in truncated:
        truncated = truncated.rsplit(" ", 1)[0]
    return truncated.rstrip() + _ELLIPSIS


def photo_text(description: str | None, caption: str) -> str:
    """Текст, который ляжет в ``messages.text`` вместо фото.

    Без описания — привычный «[фото]» (как ``sanitize.media_placeholder``), с
    описанием — «[фото: ...]». Подпись автора, если она есть, дописывается следом
    и остаётся его собственными словами.
    """
    head = f"[фото: {description}]" if description else _PHOTO_PLACEHOLDER
    caption = caption.strip()
    return f"{head} {caption}" if caption else head


class VisionDescriber:
    """Один вызов модели со зрением на снимок -> описание или None.

    Промпт (``prompts/vision.txt``) со слотом ``{caption}`` подставляется за один
    проход ``re.sub`` — тем же приёмом, что в ``judge.py``/``followup.py``: цепочка
    ``str.replace`` могла бы повторно затронуть уже подставленные данные.

    Любой сбой — ``None``: пустая модель, ``LLMError`` (включая исчерпанный
    суточный потолок), пустой ответ. Вызывающий тогда пишет обычное «[фото]» —
    фото никогда не ломает обработку сообщения.
    """

    def __init__(
        self,
        llm: LLMClient,
        cfg_getter: Callable[[], Config],
        prompt_template: str,
    ) -> None:
        self._llm = llm
        self._cfg_getter = cfg_getter
        self._prompt_template = prompt_template

    async def describe(self, image: bytes, *, mime: str, caption: str, now: int) -> str | None:
        cfg = self._cfg_getter()
        vision_cfg = cfg.behaviour.vision
        model = vision_cfg.model or cfg.llm.main_model
        if not model:
            logger.info("vision: модель не задана (behaviour.vision.model и llm.main_model пусты)")
            return None

        clean_caption = _strip_fake_delimiters(normalize_text(caption)).strip()
        prompt = _SLOT_RE.sub(lambda _m: clean_caption or _CAPTION_EMPTY, self._prompt_template)

        # base64 живёт только внутри тела запроса: ни в лог, ни в БД он не попадает.
        data_url = f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}"
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ]

        try:
            result = await self._llm.call_raw(
                messages,
                model=model,
                max_tokens=vision_cfg.max_tokens,
                now=now,
                counter_key=_COUNTER_KEY,
                calls_cap=vision_cfg.daily_cap,
            )
        except LLMError as exc:
            if exc.reason == "llm:calls_cap":
                # Исчерпанный потолок — штатное состояние к концу активных суток,
                # а не сбой: INFO, чтобы не будить владельца WARNING'ами.
                logger.info("vision: суточный потолок описаний исчерпан")
            else:
                logger.warning("vision llm error: reason=%s", exc.reason)
            return None

        description = _strip_fake_delimiters(normalize_text(result.text)).strip()
        if not description:
            return None
        return _truncate(description, vision_cfg.max_chars)
