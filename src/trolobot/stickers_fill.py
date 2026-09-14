"""Офлайн-наполнение каталога стикеров (CLAUDE.md, "Интерфейсы: стикеры"; PLAN.md,
раздел "Стикеры").

``python -m trolobot.stickers_fill <set_name> [--out stickers.yaml] [--dry-run]
    [--no-llm] [--model provider/model]``

Не рантайм бота — отдельный ручной прогон (после того, как владелец собрал/поправил
набор стикеров в Telegram). Ходит в Bot API: ``get_sticker_set`` за списком стикеров
набора, затем ``download`` за содержимым каждого нового (webp). Animated/video-стикеры
(``is_animated``/``is_video``) распознавать не умеем (tgs/webm) — пропускаются с
WARNING, в каталог не попадают вовсе.

Надпись на стикере телеграм не отдаёт как текст — распознаётся один раз моделью со
зрением через ``LLMClient.call_raw`` (content-массив с ``image_url``), результат
(``text``/``when``) владелец правит руками — этот скрипт даёт только черновик.

Существующий ``stickers.yaml`` (если есть) читается и МЕРЖИТСЯ по ``file_id``:
совпадение — id/text/when/enabled старой записи сохраняются (правки владельца не
теряются), новые стикеры получают следующие id, пропавшие из набора остаются в
файле, но помечаются ``enabled: false`` с WARNING (не удаляются — на них могут
быть ссылки в ``bot_replies``/недавно использованных).

Зависимости, нужные тестам, инжектируются явно (по образцу ``places_fill.py``):
``BotLike`` (тесты подставляют подделку с ``get_sticker_set``/``download``, без
настоящего aiogram) и ``LLMLike`` (подмножество ``LLMClient.call_raw``, не считает
бюджет/бухгалтерию живого бота — как ``_InMemoryState`` в ``places_fill.py``).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import re
import time as time_module
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import yaml

from trolobot.config import load_config
from trolobot.llm import LLMClient, LLMError, LLMResult
from trolobot.settings import Settings
from trolobot.stickers import Sticker, StickerCatalog, load_catalog

logger = logging.getLogger(__name__)

_RECOGNITION_MAX_TOKENS = 120
_RECOGNITION_INSTRUCTION = (
    "На стикере надпись. Ответь одним JSON без markdown: "
    '{"text": "надпись дословно", "when": "когда такой стикер уместен в дружеском '
    'чате, 3-8 слов"}. Если надписи нет — text пустой.'
)

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL | re.IGNORECASE)


def _strip_code_fence(text: str) -> str:
    match = _CODE_FENCE_RE.match(text)
    if match is not None:
        return match.group(1).strip()
    return text


class _ReadableLike(Protocol):
    def read(self) -> bytes: ...


class _StickerLike(Protocol):
    """Подмножество ``aiogram.types.Sticker``, нужное этому модулю."""

    @property
    def file_id(self) -> str: ...
    @property
    def emoji(self) -> str | None: ...
    @property
    def is_animated(self) -> bool: ...
    @property
    def is_video(self) -> bool: ...


class _StickerSetLike(Protocol):
    """Подмножество ``aiogram.types.StickerSet``, нужное этому модулю.

    ``Sequence`` (не ``list``) — так же, как ``commands._DbLike.last_bot_replies``:
    ``list`` инвариантен по параметру, поэтому настоящий
    ``aiogram.types.StickerSet`` (``stickers: list[Sticker]``) не подошёл бы
    структурно под ``list[_StickerLike]``, а под ковариантный ``Sequence`` —
    подходит, раз ``Sticker`` структурно совпадает с ``_StickerLike``.
    """

    @property
    def stickers(self) -> Sequence[_StickerLike]: ...


class BotLike(Protocol):
    """Узкий протокол вместо ``aiogram.Bot`` — тесты подделывают его без aiogram
    (по образцу ``responder._BotLike``/``reactions.ReactionBotLike``)."""

    async def get_sticker_set(self, name: str) -> _StickerSetLike: ...

    async def download(self, file: str) -> _ReadableLike | None: ...


class LLMLike(Protocol):
    """Подмножество ``LLMClient``, нужное этому модулю — тесты подставляют подделку,
    не считающую бюджет/бухгалтерию живого бота (см. модульный докстринг)."""

    async def call_raw(
        self, messages: list[dict[str, object]], *, model: str, max_tokens: int, now: int
    ) -> LLMResult: ...


class _InMemoryState:
    """Минимальный state-стор для настоящего LLMClient внутри stickers_fill.

    Наполнение каталога — офлайн и разовое действие: дневные лимиты/бюджет/
    предохранитель живого бота (общая таблица state) ему не нужны, счётчики
    LLMClient здесь живут только на время одного прогона (по образцу
    ``places_fill._InMemoryState``)."""

    def __init__(self) -> None:
        self._state: dict[str, str] = {}

    async def get_state(self, key: str) -> str | None:
        return self._state.get(key)

    async def set_state(self, key: str, value: str) -> None:
        self._state[key] = value

    async def increment_state(self, key: str, by: int = 1) -> int:
        new_value = int(self._state.get(key, "0")) + by
        self._state[key] = str(new_value)
        return new_value

    async def add_state_float(self, key: str, by: float) -> float:
        new_value = float(self._state.get(key, "0")) + by
        self._state[key] = str(new_value)
        return new_value


def _parse_recognition(raw: str) -> tuple[str, str]:
    """Разбирает ответ распознавания так же терпимо, как ``judge._parse_verdict``:
    срез ```json``` обёрток, поиск первой "{" и ``raw_decode`` от неё. Любой сбой ->
    ("", "") — черновик остаётся пустым, владелец заполнит сам."""
    try:
        candidate = _strip_code_fence(raw.strip())
        start = candidate.find("{")
        if start == -1:
            return "", ""
        data, _end = json.JSONDecoder().raw_decode(candidate, start)
        if not isinstance(data, dict):
            return "", ""
        text_value = data.get("text", "")
        when_value = data.get("when", "")
        text = text_value.strip() if isinstance(text_value, str) else ""
        when = when_value.strip() if isinstance(when_value, str) else ""
        return text, when
    except Exception:
        return "", ""


async def recognize_sticker(
    image_bytes: bytes, *, llm: LLMLike | None, model: str, now: int
) -> tuple[str, str]:
    """(text, when) черновика по картинке стикера (webp). ``llm=None`` -> ("", "")
    без сетевого вызова — это и есть ``--no-llm``."""
    if llm is None:
        return "", ""
    encoded = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:image/webp;base64,{encoded}"
    messages: list[dict[str, object]] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": _RECOGNITION_INSTRUCTION},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]
    try:
        result = await llm.call_raw(
            messages, model=model, max_tokens=_RECOGNITION_MAX_TOKENS, now=now
        )
    except LLMError as exc:
        logger.warning("stickers_fill: llm error while recognizing sticker: reason=%s", exc.reason)
        return "", ""
    return _parse_recognition(result.text)


async def _download_bytes(bot: BotLike, file_id: str) -> bytes:
    downloaded = await bot.download(file_id)
    if downloaded is None:
        raise RuntimeError(f"stickers_fill: bot.download({file_id!r}) вернул пусто")
    return downloaded.read()


async def build_catalog(
    *,
    bot: BotLike,
    set_name: str,
    existing: StickerCatalog,
    llm: LLMLike | None,
    model: str,
    now: int,
) -> StickerCatalog:
    """Тянет набор из Bot API и мержит с ``existing`` по ``file_id`` (см. модульный
    докстринг: правки владельца сохраняются, новые получают следующие id, пропавшие
    остаются выключенными)."""
    sticker_set = await bot.get_sticker_set(set_name)

    existing_by_file_id = {sticker.file_id: sticker for sticker in existing.stickers}
    seen_file_ids: set[str] = set()
    next_id = max((sticker.id for sticker in existing.stickers), default=0) + 1

    result: list[Sticker] = []
    for raw in sticker_set.stickers:
        if raw.is_animated or raw.is_video:
            logger.warning(
                "stickers_fill: пропускаю анимированный/видео стикер file_id=%s "
                "(tgs/webm не распознаём)",
                raw.file_id,
            )
            continue

        seen_file_ids.add(raw.file_id)
        old = existing_by_file_id.get(raw.file_id)
        if old is not None:
            # Совпадение по file_id — id/text/when/enabled владельца не трогаем,
            # emoji обновляем сам (его правкой владелец не занимается).
            result.append(
                Sticker(
                    id=old.id,
                    file_id=raw.file_id,
                    emoji=raw.emoji or old.emoji,
                    text=old.text,
                    when=old.when,
                    enabled=old.enabled,
                )
            )
            continue

        image_bytes = await _download_bytes(bot, raw.file_id)
        text, when = await recognize_sticker(image_bytes, llm=llm, model=model, now=now)
        sticker_id = next_id
        next_id += 1
        result.append(
            Sticker(
                id=sticker_id,
                file_id=raw.file_id,
                emoji=raw.emoji or "",
                text=text,
                when=when,
                enabled=True,
            )
        )

    for old in existing.stickers:
        if old.file_id in seen_file_ids:
            continue
        logger.warning(
            "stickers_fill: стикер id=%s (file_id=%s) пропал из набора %r, выключаю",
            old.id,
            old.file_id,
            set_name,
        )
        result.append(
            Sticker(
                id=old.id,
                file_id=old.file_id,
                emoji=old.emoji,
                text=old.text,
                when=old.when,
                enabled=False,
            )
        )

    result.sort(key=lambda sticker: sticker.id)
    return StickerCatalog(set_name=set_name, stickers=result)


def render_report(catalog: StickerCatalog) -> str:
    lines = ["id | emoji | text | when | enabled"]
    for sticker in catalog.stickers:
        lines.append(
            f"{sticker.id} | {sticker.emoji or '-'} | {sticker.text or '-'} | "
            f"{sticker.when or '-'} | {'да' if sticker.enabled else 'нет'}"
        )
    return "\n".join(lines)


def render_yaml(catalog: StickerCatalog) -> str:
    payload = {
        "set_name": catalog.set_name,
        "stickers": [sticker.model_dump(mode="json") for sticker in catalog.stickers],
    }
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m trolobot.stickers_fill",
        description='Офлайн-наполнение каталога стикеров (PLAN.md, раздел "Стикеры").',
    )
    parser.add_argument("set_name", help="имя набора из t.me/addstickers/<имя>")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("stickers.yaml"),
        help="путь к каталогу (читается и мержится)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="только напечатать таблицу, не писать файл"
    )
    parser.add_argument(
        "--no-llm", action="store_true", help="text/when всегда пустые, модель не вызывается"
    )
    parser.add_argument(
        "--model", type=str, default=None, help="модель со зрением; по умолчанию cfg.llm.main_model"
    )
    return parser.parse_args(argv)


async def run(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    bot: BotLike | None = None,
    llm: LLMLike | None = None,
) -> StickerCatalog:
    """Точка входа, общая для CLI и тестов (см. модульный докстринг про DI)."""
    args = parse_args(argv)
    settings = settings if settings is not None else Settings()
    existing = load_catalog(args.out)
    now = int(time_module.time())

    owns_bot = bot is None
    if bot is not None:
        bot_obj: BotLike = bot
    else:
        from aiogram import Bot  # ленивый импорт: тестам не нужен настоящий aiogram Bot

        bot_obj = Bot(token=settings.bot_token.get_secret_value())

    model = args.model or ""
    use_llm: LLMLike | None
    owns_llm = False
    if args.no_llm:
        use_llm = None
    elif llm is not None:
        use_llm = llm
        model = model or load_config(settings.config_path).llm.main_model
    else:
        cfg = load_config(settings.config_path)
        model = model or cfg.llm.main_model
        if settings.openrouter_api_key is not None and model:
            use_llm = LLMClient(
                api_key=settings.openrouter_api_key.get_secret_value(),
                cfg_getter=lambda: cfg,
                db=_InMemoryState(),
            )
            owns_llm = True
        else:
            use_llm = None
            logger.warning(
                "stickers_fill: LLM недоступен (нет OPENROUTER_API_KEY или модель со "
                "зрением не задана) — text/when будут пустыми для новых стикеров"
            )

    try:
        catalog = await build_catalog(
            bot=bot_obj,
            set_name=args.set_name,
            existing=existing,
            llm=use_llm,
            model=model,
            now=now,
        )
    finally:
        if owns_bot:
            # owns_bot=True -> bot_obj — настоящий aiogram.Bot (создан выше), не
            # подделка теста; BotLike не объявляет .session, поэтому type: ignore.
            await bot_obj.session.close()  # type: ignore[attr-defined]
        if owns_llm:
            assert isinstance(use_llm, LLMClient)
            await use_llm.aclose()

    for line in render_report(catalog).splitlines():
        logger.info(line)
    logger.info("Проверьте text/when руками перед выкатом в %s.", args.out)

    if not args.dry_run:
        args.out.write_text(render_yaml(catalog), encoding="utf-8")

    return catalog


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run())


if __name__ == "__main__":
    main()
