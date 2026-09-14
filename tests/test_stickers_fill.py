"""Тесты trolobot.stickers_fill: без сети — фейковый Bot (get_sticker_set/download) и
фейковый LLM (call_raw), по образцу tests/test_places_fill.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from trolobot.settings import Settings
from trolobot.stickers import Sticker, StickerCatalog, load_catalog
from trolobot.stickers_fill import (
    build_catalog,
    recognize_sticker,
    render_report,
    render_yaml,
    run,
)

NOW = 1_768_003_200


@pytest.fixture(autouse=True)
def _isolated_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)


def _settings(tmp_path: Path, *, openrouter_key: str | None = None) -> Settings:
    return Settings(
        bot_token="test-bot-token",
        openrouter_api_key=openrouter_key,
        db_path=tmp_path / "bot.db",
        config_path=tmp_path / "no-such-config.yaml",
    )


@dataclass
class FakeRawSticker:
    file_id: str
    emoji: str | None = "😂"
    is_animated: bool = False
    is_video: bool = False


@dataclass
class FakeStickerSet:
    stickers: list[FakeRawSticker]


@dataclass
class FakeDownloaded:
    data: bytes

    def read(self) -> bytes:
        return self.data


@dataclass
class FakeBot:
    sticker_set: FakeStickerSet
    downloads: dict[str, bytes] = field(default_factory=dict)
    downloaded_ids: list[str] = field(default_factory=list)

    async def get_sticker_set(self, name: str) -> FakeStickerSet:
        del name
        return self.sticker_set

    async def download(self, file: str) -> FakeDownloaded | None:
        self.downloaded_ids.append(file)
        return FakeDownloaded(self.downloads.get(file, b"fake-webp-bytes"))


@dataclass
class FakeLLM:
    text: str = '{"text": "Ну ты даёшь", "when": "удивление"}'
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def call_raw(
        self, messages: list[dict[str, object]], *, model: str, max_tokens: int, now: int
    ) -> Any:
        from trolobot.llm import LLMResult

        self.calls.append({"messages": messages, "model": model, "max_tokens": max_tokens})
        return LLMResult(text=self.text, cost_usd=0.0001, prompt_tokens=5, completion_tokens=5)


# --- build_catalog: слияние ---------------------------------------------------


async def test_new_sticker_gets_next_id_and_recognized_text() -> None:
    bot = FakeBot(FakeStickerSet([FakeRawSticker(file_id="F1", emoji="😂")]))
    llm = FakeLLM()
    existing = StickerCatalog()

    catalog = await build_catalog(
        bot=bot, set_name="myset", existing=existing, llm=llm, model="vision/x", now=NOW
    )

    assert len(catalog.stickers) == 1
    sticker = catalog.stickers[0]
    assert sticker.id == 1
    assert sticker.file_id == "F1"
    assert sticker.text == "Ну ты даёшь"
    assert sticker.when == "удивление"
    assert sticker.enabled is True
    assert len(llm.calls) == 1
    assert bot.downloaded_ids == ["F1"]


async def test_existing_sticker_preserves_owner_edits() -> None:
    bot = FakeBot(FakeStickerSet([FakeRawSticker(file_id="F1", emoji="🙂")]))
    llm = FakeLLM(text='{"text": "НОВЫЙ ТЕКСТ ОТ МОДЕЛИ", "when": "не важно"}')
    existing = StickerCatalog(
        set_name="myset",
        stickers=[
            Sticker(
                id=7,
                file_id="F1",
                emoji="😂",
                text="Ручная правка владельца",
                when="ручной when",
                enabled=False,
            )
        ],
    )

    catalog = await build_catalog(
        bot=bot, set_name="myset", existing=existing, llm=llm, model="vision/x", now=NOW
    )

    assert len(catalog.stickers) == 1
    sticker = catalog.stickers[0]
    # id/text/when/enabled — из старой записи, не тронуты моделью/скриптом.
    assert sticker.id == 7
    assert sticker.text == "Ручная правка владельца"
    assert sticker.when == "ручной when"
    assert sticker.enabled is False
    # emoji обновляется от Telegram.
    assert sticker.emoji == "🙂"
    # Совпадение по file_id -> распознавание не вызывается вовсе (не тратим деньги).
    assert llm.calls == []
    assert bot.downloaded_ids == []


async def test_missing_sticker_is_disabled_not_removed(caplog: pytest.LogCaptureFixture) -> None:
    bot = FakeBot(FakeStickerSet([]))  # набор пуст — старый стикер "пропал"
    llm = FakeLLM()
    existing = StickerCatalog(
        set_name="myset",
        stickers=[Sticker(id=3, file_id="GONE", text="Был да пропал", enabled=True)],
    )

    with caplog.at_level(logging.WARNING):
        catalog = await build_catalog(
            bot=bot, set_name="myset", existing=existing, llm=llm, model="vision/x", now=NOW
        )

    assert len(catalog.stickers) == 1
    sticker = catalog.stickers[0]
    assert sticker.id == 3
    assert sticker.file_id == "GONE"
    assert sticker.enabled is False
    assert any("пропал" in record.message for record in caplog.records)


async def test_new_ids_continue_after_max_existing_id() -> None:
    bot = FakeBot(FakeStickerSet([FakeRawSticker(file_id="NEW")]))
    llm = FakeLLM()
    existing = StickerCatalog(
        set_name="myset",
        stickers=[Sticker(id=5, file_id="OLD", text="старый", enabled=True)],
    )

    catalog = await build_catalog(
        bot=bot, set_name="myset", existing=existing, llm=llm, model="vision/x", now=NOW
    )

    new_sticker = next(s for s in catalog.stickers if s.file_id == "NEW")
    assert new_sticker.id == 6


# --- пропуск анимированных/видео ---------------------------------------------


async def test_animated_and_video_stickers_are_skipped(caplog: pytest.LogCaptureFixture) -> None:
    bot = FakeBot(
        FakeStickerSet(
            [
                FakeRawSticker(file_id="STATIC"),
                FakeRawSticker(file_id="ANIM", is_animated=True),
                FakeRawSticker(file_id="VIDEO", is_video=True),
            ]
        )
    )
    llm = FakeLLM()
    existing = StickerCatalog()

    with caplog.at_level(logging.WARNING):
        catalog = await build_catalog(
            bot=bot, set_name="myset", existing=existing, llm=llm, model="vision/x", now=NOW
        )

    file_ids = {s.file_id for s in catalog.stickers}
    assert file_ids == {"STATIC"}
    assert bot.downloaded_ids == ["STATIC"]
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("ANIM" in w for w in warnings)
    assert any("VIDEO" in w for w in warnings)


# --- --no-llm ------------------------------------------------------------------


async def test_recognize_sticker_without_llm_is_empty() -> None:
    text, when = await recognize_sticker(b"bytes", llm=None, model="x", now=NOW)
    assert (text, when) == ("", "")


async def test_no_llm_leaves_new_stickers_blank() -> None:
    bot = FakeBot(FakeStickerSet([FakeRawSticker(file_id="F1")]))
    existing = StickerCatalog()

    catalog = await build_catalog(
        bot=bot, set_name="myset", existing=existing, llm=None, model="", now=NOW
    )

    assert catalog.stickers[0].text == ""
    assert catalog.stickers[0].when == ""
    # Скачиваем всё равно — распознавание может включиться позже.
    assert bot.downloaded_ids == ["F1"]


# --- run(): CLI склейка, --dry-run --------------------------------------------


async def test_run_dry_run_does_not_write_file(tmp_path: Path) -> None:
    out_path = tmp_path / "stickers.yaml"
    bot = FakeBot(FakeStickerSet([FakeRawSticker(file_id="F1")]))
    llm = FakeLLM()
    settings = _settings(tmp_path)

    await run(
        ["myset", "--out", str(out_path), "--dry-run", "--model", "vision/x"],
        settings=settings,
        bot=bot,
        llm=llm,
    )

    assert not out_path.exists()


async def test_run_writes_merged_yaml(tmp_path: Path) -> None:
    out_path = tmp_path / "stickers.yaml"
    out_path.write_text(
        yaml.safe_dump(
            {
                "set_name": "myset",
                "stickers": [
                    {
                        "id": 1,
                        "file_id": "F1",
                        "emoji": "😂",
                        "text": "Старый текст",
                        "when": "старый when",
                        "enabled": True,
                    }
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    bot = FakeBot(FakeStickerSet([FakeRawSticker(file_id="F1"), FakeRawSticker(file_id="F2")]))
    llm = FakeLLM()
    settings = _settings(tmp_path)

    catalog = await run(
        ["myset", "--out", str(out_path), "--model", "vision/x"],
        settings=settings,
        bot=bot,
        llm=llm,
    )

    assert len(catalog.stickers) == 2
    reloaded = load_catalog(out_path)
    assert {s.file_id for s in reloaded.stickers} == {"F1", "F2"}
    old = next(s for s in reloaded.stickers if s.file_id == "F1")
    assert old.text == "Старый текст"  # правка не потеряна


async def test_run_no_llm_flag_skips_recognition(tmp_path: Path) -> None:
    out_path = tmp_path / "stickers.yaml"
    bot = FakeBot(FakeStickerSet([FakeRawSticker(file_id="F1")]))
    settings = _settings(tmp_path)

    await run(["myset", "--out", str(out_path), "--no-llm"], settings=settings, bot=bot)

    reloaded = load_catalog(out_path)
    assert reloaded.stickers[0].text == ""


# --- рендер отчёта -------------------------------------------------------------


def test_render_report_lists_all_columns() -> None:
    catalog = StickerCatalog(
        set_name="x",
        stickers=[Sticker(id=1, file_id="F", emoji="😂", text="t", when="w", enabled=True)],
    )
    report = render_report(catalog)
    assert "id | emoji | text | when | enabled" in report
    assert "1 | 😂 | t | w | да" in report


def test_render_yaml_round_trips_through_load_catalog(tmp_path: Path) -> None:
    catalog = StickerCatalog(
        set_name="x",
        stickers=[Sticker(id=1, file_id="F", emoji="😂", text="t", when="w", enabled=True)],
    )
    path = tmp_path / "stickers.yaml"
    path.write_text(render_yaml(catalog), encoding="utf-8")
    reloaded = load_catalog(path)
    assert reloaded == catalog
