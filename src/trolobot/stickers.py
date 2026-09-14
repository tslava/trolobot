"""Стикеры вместо текстового ответа — второй вызов дешёвой моделью (CLAUDE.md,
"Интерфейсы: стикеры"; PLAN.md, раздел "Стикеры").

Основная модель ничего не знает про каталог стикеров — меню в её промпт
никогда не подмешивается (решение владельца: дороже основной запрос не нужен,
скорость ответа не важна). Вместо этого уже ГОТОВЫЙ, прошедший выходной фильтр
текст вместе с сообщением-триггером и каталогом уходит отдельным, дешёвым
вызовом (та же модель, что судья — ``cfg.llm.judge_model``, или своя
``cfg.behaviour.stickers.model``), которая либо называет номер стикера, либо
отвечает ``null`` — и тогда в чат уходит текст, как раньше.

Промпт хранится в ``prompts/sticker.txt`` и, как ``prompts/judge.txt``, сам
оборачивает данные ({trigger}/{reply}) в разделители ``<<<CHAT ... >>>`` —
здесь только вырезаются поддельные разделители из самих данных (тем же
приёмом, что и ``judge.py``), подстановка слотов — один проход ``re.sub``.

Каталог (``stickers.yaml``) собирается офлайн-скриптом ``stickers_fill.py`` со
зрением и правится владельцем руками; ``load_catalog`` — чистая загрузка без
сети, отсутствующий/пустой файл значит "стикеров нет", а не ошибку.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Sequence
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from trolobot.config_models import Config, StickersConfig
from trolobot.llm import LLMClient, LLMError

logger = logging.getLogger(__name__)

_NO_TRIGGER = "(без обращения, реплика по собственной инициативе)"

# Регулярка, которой responder.py вытаскивает id стикера из текста, записанного
# insert_bot_reply (см. render_sticker_menu/StickerChooser.choose ниже и модульный
# докстринг responder.py про "recent_ids"): "[стикер #3] Ну ты даёшь" -> 3.
STICKER_TAG_RE = re.compile(r"^\[стикер #(\d+)\]")

# Дублирует вырезание разделителей из sanitize.normalize_text/prompt.py/judge.py —
# на всякий случай ещё раз чистим то, что уходит в промпт чузера.
_LT_RUN_RE = re.compile(r"<{3,}")
_GT_RUN_RE = re.compile(r">{3,}")

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL | re.IGNORECASE)

_SLOT_RE = re.compile(r"\{(menu|trigger|reply)\}")

# "нет файла -> WARNING один раз" (CLAUDE.md) — один раз за процесс, не за путь:
# перегрузка каталога на горячую в этом боте не предусмотрена, повторный вызов
# load_catalog с тем же отсутствующим файлом означал бы только спам одного и того
# же предупреждения.
_missing_catalog_warned = False


def _strip_fake_delimiters(text: str) -> str:
    return _GT_RUN_RE.sub(" ", _LT_RUN_RE.sub(" ", text))


def _strip_code_fence(text: str) -> str:
    match = _CODE_FENCE_RE.match(text)
    if match is not None:
        return match.group(1).strip()
    return text


class Sticker(BaseModel):
    id: int
    file_id: str
    emoji: str = ""
    text: str
    when: str = ""
    enabled: bool = True


class StickerCatalog(BaseModel):
    set_name: str = ""
    stickers: list[Sticker] = Field(default_factory=list)


def load_catalog(path: Path) -> StickerCatalog:
    """``stickers.yaml`` -> ``StickerCatalog``. Файла нет или он пуст -> пустой
    каталог (всё остальное в боте работает как раньше, стикеров просто не будет).
    Кривой YAML/данные, не подходящие под схему, -> ``ValueError`` (в т.ч.
    ``pydantic.ValidationError`` — её базовый класс)."""
    global _missing_catalog_warned
    if not path.exists():
        if not _missing_catalog_warned:
            _missing_catalog_warned = True
            logger.warning("stickers: файл %s не найден, каталог стикеров пуст", path)
        return StickerCatalog()

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid stickers yaml {path}: {exc}") from exc

    if not raw:
        return StickerCatalog()
    if not isinstance(raw, dict):
        raise ValueError(f"stickers file {path} must contain a YAML mapping")
    return StickerCatalog.model_validate(raw)


def render_sticker_menu(stickers: list[Sticker]) -> str:
    """ "1: «Ну ты даёшь» — удивление, восхищение\\n2: ..." — только enabled."""
    lines = []
    for sticker in stickers:
        if not sticker.enabled:
            continue
        if sticker.when:
            lines.append(f"{sticker.id}: «{sticker.text}» — {sticker.when}")
        else:
            lines.append(f"{sticker.id}: «{sticker.text}»")
    return "\n".join(lines)


def parse_choice(raw: str, valid_ids: set[int]) -> int | None:
    """Разбирает ответ чузера так же строго и терпимо, как ``judge._parse_verdict``/
    ``prompt.parse_reply``: срез ```json``` обёрток, поиск первой "{" и
    ``json.JSONDecoder.raw_decode`` от неё. Ожидает ``{"sticker": int|null}`` —
    всё, что не целое число из ``valid_ids`` (включая ``bool`` — подкласс ``int``
    в Python, но не то, что здесь имеют в виду), означает ``None``."""
    try:
        candidate = _strip_code_fence(raw.strip())
        start = candidate.find("{")
        if start == -1:
            return None
        data, _end = json.JSONDecoder().raw_decode(candidate, start)
        if not isinstance(data, dict):
            return None

        sticker_id = data.get("sticker")
        if sticker_id is None or isinstance(sticker_id, bool):
            return None
        if not isinstance(sticker_id, int):
            return None
        if sticker_id not in valid_ids:
            return None
        return sticker_id
    except Exception:
        # Ответ модели — недоверенный внешний текст: любой сбой разбора значит
        # "стикер не выбран", а не падение.
        return None


def recent_sticker_ids(texts: Sequence[str], window: int) -> set[int]:
    """id стикеров, использованных в последних ``window`` репликах бота, отмеченных
    тегом ``STICKER_TAG_RE`` — не по всем текстам, а по последним ``window`` именно
    стикерным. ``texts`` — в хронологическом порядке (как отдаёт
    ``Database.recent_bot_replies``), самые свежие последними."""
    ids: list[int] = []
    for text in reversed(texts):
        match = STICKER_TAG_RE.match(text)
        if match is not None:
            ids.append(int(match.group(1)))
            if len(ids) >= window:
                break
    return set(ids)


def sticker_allowed(*, cfg: StickersConfig, replies_since: int, count_today: int) -> bool:
    """Может ли вообще идти речь о стикере сейчас — до того, как чузер вызван
    (CLAUDE.md: "min_replies_between не выдержан -> чузер не вызывается вовсе")."""
    if not cfg.enabled:
        return False
    if replies_since < cfg.min_replies_between:
        return False
    return count_today < cfg.daily_cap


class StickerChooser:
    """Второй, дешёвый вызов LLM: выбирает стикер под уже готовый ответ, либо
    ``None``. Само решение "можно ли вообще предлагать стикер сейчас"
    (``sticker_allowed``) принимает вызывающий (``responder.py``) — сюда
    попадают только уже отфильтрованные кандидаты."""

    def __init__(
        self,
        llm: LLMClient,
        cfg_getter: Callable[[], Config],
        catalog: StickerCatalog,
        prompt_template: str,
    ) -> None:
        self._llm = llm
        self._cfg_getter = cfg_getter
        self._catalog = catalog
        self._prompt_template = prompt_template

    async def choose(
        self,
        *,
        reply_text: str,
        trigger_text: str,
        exclude_ids: set[int],
        now: int,
    ) -> Sticker | None:
        candidates = [
            sticker
            for sticker in self._catalog.stickers
            if sticker.enabled and sticker.id not in exclude_ids
        ]
        if not candidates:
            return None

        cfg = self._cfg_getter()
        model = cfg.behaviour.stickers.model or cfg.llm.judge_model
        if not model:
            return None

        menu = render_sticker_menu(candidates)
        trigger_clean = _strip_fake_delimiters(trigger_text).strip() or _NO_TRIGGER
        reply_clean = _strip_fake_delimiters(reply_text).strip()

        slot_values = {"menu": menu, "trigger": trigger_clean, "reply": reply_clean}
        system = _SLOT_RE.sub(lambda m: slot_values[m.group(1)], self._prompt_template)
        messages = [{"role": "system", "content": system}]

        try:
            result = await self._llm.call(
                messages, model=model, max_tokens=cfg.behaviour.stickers.max_tokens, now=now
            )
        except LLMError as exc:
            # Стикер — необязательное украшение: текст всё равно уйдёт, поэтому
            # ошибка чузера — не повод резать ответ, только WARNING в лог.
            logger.warning("sticker chooser llm error: reason=%s", exc.reason)
            return None

        valid_ids = {sticker.id for sticker in candidates}
        chosen_id = parse_choice(result.text, valid_ids)
        if chosen_id is None:
            return None
        for sticker in candidates:
            if sticker.id == chosen_id:
                return sticker
        return None
