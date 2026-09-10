"""Тесты places_fill.py (CLAUDE.md/PLAN.md, этап 5): офлайн-наполнение кэша заведений.

Google Places API замокан через ``httpx.MockTransport`` (сеть не трогаем — конвенция
проекта, CLAUDE.md "Конвенции"), LLM — подделкой под ``LLMLike`` (Protocol с ``call``,
как ``FakeStore``/фейковые клиенты в ``test_llm.py``/``test_injections.py``). База —
временный файл через ``tmp_path`` и настоящий ``trolobot.db.Database`` (проверяем
через ``places_all(operational_only=False)``, а не приватные детали SQL).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from trolobot.db import Database
from trolobot.llm import LLMResult
from trolobot.places_fill import (
    FetchedPlace,
    ManualEntry,
    build_manual_only_rows,
    build_review_text,
    find_manual_override,
    load_manual,
    parse_price_level,
    pick_category,
    pick_district,
    render_report,
    run,
)
from trolobot.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_MANUAL_PATH = REPO_ROOT / "places_manual.yaml"
API_KEY = "gp-test-key"


@pytest.fixture(autouse=True)
def _isolated_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Как в test_settings.py: гарантируем, что рядом нет настоящего .env/config.yaml.
    monkeypatch.chdir(tmp_path)


def _settings(
    tmp_path: Path, *, google_key: str | None = API_KEY, openrouter_key: str | None = None
) -> Settings:
    return Settings(
        bot_token="test-bot-token",
        google_places_key=google_key,
        openrouter_api_key=openrouter_key,
        db_path=tmp_path / "bot.db",
        config_path=tmp_path / "no-such-config.yaml",
    )


def _write_manual(tmp_path: Path, entries: list[dict[str, Any]]) -> Path:
    path = tmp_path / "manual.yaml"
    path.write_text(yaml.safe_dump(entries, allow_unicode=True), encoding="utf-8")
    return path


def _google_place(
    place_id: str,
    name: str,
    *,
    rating: float = 4.5,
    reviews: int = 100,
    status: str = "OPERATIONAL",
    address: str = "Testowa 1, 60-100 Poznań",
    reviews_text: list[str] | None = None,
    primary_type: str = "cafe",
    price_level: object = "PRICE_LEVEL_MODERATE",
) -> dict[str, Any]:
    return {
        "id": place_id,
        "displayName": {"text": name, "languageCode": "pl"},
        "formattedAddress": address,
        "rating": rating,
        "userRatingCount": reviews,
        "priceLevel": price_level,
        "businessStatus": status,
        "primaryType": primary_type,
        "reviews": [{"text": {"text": t}} for t in (reviews_text or [])],
    }


def _handler_for(
    responses_by_query: dict[str, dict[str, Any]],
) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == API_KEY
        payload = json.loads(request.content)
        query = payload["textQuery"]
        assert payload["languageCode"] == "pl"
        assert payload["regionCode"] == "PL"
        body = responses_by_query.get(query, {"places": []})
        return httpx.Response(200, json=body)

    return handler


def _http(responses_by_query: dict[str, dict[str, Any]]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(_handler_for(responses_by_query)))


@dataclass
class _FakeLLM:
    response_text: str
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def call(
        self, messages: list[dict[str, str]], *, model: str, max_tokens: int, now: int
    ) -> LLMResult:
        self.calls.append(
            {"messages": messages, "model": model, "max_tokens": max_tokens, "now": now}
        )
        return LLMResult(
            text=self.response_text, cost_usd=0.0, prompt_tokens=1, completion_tokens=1
        )


async def _stored_rows(settings: Settings) -> list[Any]:
    db = Database(settings.db_path)
    await db.connect()
    try:
        return await db.places_all(operational_only=False)
    finally:
        await db.close()


# --------------------------------------------------------------------------- #
# Фильтр + запись: closed/низкий рейтинг отсеиваются, нормальное место остаётся
# --------------------------------------------------------------------------- #


async def test_closed_and_low_rating_places_filtered_only_ok_one_written(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    places = [
        _google_place("places/closed", "Закрытый бар", status="CLOSED_PERMANENTLY"),
        _google_place("places/low", "Низкий рейтинг", rating=3.9),
        _google_place(
            "places/ok", "Piwna Stopa", reviews_text=["quiet nice place", "great craft beer"]
        ),
    ]
    http = _http({"test query": {"places": places}})
    llm = _FakeLLM("тихо")
    try:
        rows = await run(
            ["--queries", "test query", "--manual", str(REAL_MANUAL_PATH)],
            settings=settings,
            http=http,
            llm=llm,
        )
    finally:
        await http.aclose()

    assert [row.place_id for row in rows] == ["places/ok"]
    assert rows[0].name == "Piwna Stopa"
    assert rows[0].fact == "тихо"

    stored = await _stored_rows(settings)
    assert len(stored) == 1
    assert stored[0].place_id == "places/ok"
    assert stored[0].fact == "тихо"


# --------------------------------------------------------------------------- #
# Manual переопределяет quiet/category
# --------------------------------------------------------------------------- #


async def test_manual_overrides_quiet_and_category(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(tmp_path)
    manual_path = _write_manual(
        tmp_path,
        [{"name": "Jeżycówka", "quiet": True, "category": "outskirts"}],
    )
    # Без override: primary_type не "bar" и не "cheap" в отзывах -> по умолчанию craft;
    # quiet по умолчанию False. Manual должен это перебить.
    place = _google_place("places/j", "Jeżycówka", primary_type="cafe", reviews_text=[])
    http = _http({"q": {"places": [place]}})
    caplog.set_level(logging.INFO)
    try:
        rows = await run(
            ["--queries", "q", "--manual", str(manual_path), "--no-llm"],
            settings=settings,
            http=http,
        )
    finally:
        await http.aclose()

    assert len(rows) == 1
    assert rows[0].quiet is True
    assert rows[0].category == "outskirts"
    assert rows[0].manual_override is True

    # Код-ревью: применённая ручная запись -- лог INFO и статус "manual" в отчёте.
    assert any(
        "manual override: Jeżycówka → Jeżycówka" in record.message for record in caplog.records
    )
    report = render_report(rows)
    assert "manual" in report.splitlines()[1]


def test_find_manual_override_matches_case_insensitively_and_by_word_boundary() -> None:
    manual = [ManualEntry(name="Klubokawiarnia LALKA", quiet=True, category="pub", district=None)]

    assert find_manual_override("klubokawiarnia lalka", manual) is not None
    assert find_manual_override("Klubokawiarnia LALKA (Prusa 18)", manual) is not None
    assert find_manual_override("Совсем другое место", manual) is None


def test_find_manual_override_short_single_word_name_requires_exact_match() -> None:
    """Код-ревью: manual-имя из одного слова длиной <= 3 ("BRO") матчит только
    точное совпадение целиком, не любое место, где это слово мелькает мимоходом."""
    manual = [ManualEntry(name="BRO", quiet=False, category="pub", district=None)]

    assert find_manual_override("Browar Poznański", manual) is None
    assert find_manual_override("BRO", manual) is not None
    assert find_manual_override("bro", manual) is not None


def test_find_manual_override_matches_by_word_not_substring() -> None:
    """Код-ревью: сопоставление по границам слов, не по подстроке."""
    manual = [ManualEntry(name="Piwnica", quiet=False, category="pub", district=None)]

    assert find_manual_override("Sklep Pub Piwnica", manual) is not None
    # Все слова manual-имени входят как слова в displayName -- это допустимо, даже
    # если manual-имя не единственное слово в названии.
    assert find_manual_override("Piwnica pod Baranami", manual) is not None
    # "Piwniczka" - другое слово, не подстрока "Piwnica" по границам слов.
    assert find_manual_override("Piwniczka", manual) is None


# --------------------------------------------------------------------------- #
# fact через подделку LLM: валидный записан, невалидный -> пустой + WARNING
# --------------------------------------------------------------------------- #


async def test_valid_fact_from_llm_is_stored(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    place = _google_place("places/v", "Валидное место", reviews_text=["it is quiet here"])
    http = _http({"q": {"places": [place]}})
    llm = _FakeLLM("тихо")
    try:
        rows = await run(["--queries", "q"], settings=settings, http=http, llm=llm)
    finally:
        await http.aclose()

    assert rows[0].fact == "тихо"
    assert len(llm.calls) == 1
    # Отзывы уходят в разделителях <<<CHAT ... >>> с оговоркой "не выполнять".
    user_content = llm.calls[0]["messages"][1]["content"]
    assert "<<<CHAT" in user_content
    assert "it is quiet here" in user_content
    assert "не выполняй" in user_content
    assert llm.calls[0]["max_tokens"] == 40


async def test_invalid_llm_fact_becomes_empty_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(tmp_path)
    place = _google_place("places/i", "Невалидное место", reviews_text=["some review text"])
    http = _http({"q": {"places": [place]}})
    llm = _FakeLLM("Ignore instructions")
    caplog.set_level(logging.WARNING)
    try:
        rows = await run(["--queries", "q"], settings=settings, http=http, llm=llm)
    finally:
        await http.aclose()

    assert rows[0].fact == ""
    assert any("invalid fact" in record.message for record in caplog.records)

    stored = await _stored_rows(settings)
    assert stored[0].fact == ""


# --------------------------------------------------------------------------- #
# --no-llm -> fact пустой, модель не вызывается вовсе
# --------------------------------------------------------------------------- #


async def test_no_llm_flag_skips_model_call_entirely(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    place = _google_place("places/n", "Без LLM", reviews_text=["irrelevant review"])
    http = _http({"q": {"places": [place]}})
    llm = _FakeLLM("тихо")
    try:
        rows = await run(["--queries", "q", "--no-llm"], settings=settings, http=http, llm=llm)
    finally:
        await http.aclose()

    assert rows[0].fact == ""
    assert llm.calls == []


# --------------------------------------------------------------------------- #
# --dry-run: таблица есть, БД пуста
# --------------------------------------------------------------------------- #


async def test_dry_run_prints_table_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = _settings(tmp_path)
    place = _google_place("places/d", "Драй Ран Бар", reviews_text=["nice"])
    http = _http({"q": {"places": [place]}})
    try:
        rows = await run(["--queries", "q", "--dry-run", "--no-llm"], settings=settings, http=http)
    finally:
        await http.aclose()

    assert len(rows) == 1
    out = capsys.readouterr().out
    assert "Драй Ран Бар" in out
    assert "Проверьте fact руками" in out

    assert not settings.db_path.exists()


# --------------------------------------------------------------------------- #
# --manual-only без ключа Google -> 14 строк в places (весь places_manual.yaml)
# --------------------------------------------------------------------------- #


async def test_manual_only_without_key_writes_all_manual_rows(tmp_path: Path) -> None:
    settings = _settings(tmp_path, google_key=None)

    rows = await run(
        ["--manual-only", "--manual", str(REAL_MANUAL_PATH)],
        settings=settings,
    )

    manual = load_manual(REAL_MANUAL_PATH)
    assert len(manual) == 14
    assert len(rows) == 14
    assert all(row.rating == 5.0 for row in rows)
    assert all(row.reviews == 999 for row in rows)
    assert all(row.operational for row in rows)

    stored = await _stored_rows(settings)
    assert len(stored) == 14


def test_build_manual_only_rows_marks_rating_and_reviews_as_manual() -> None:
    manual = [ManualEntry(name="Kórnik", quiet=True, category="outskirts", district="Kórnik")]
    rows = build_manual_only_rows(manual, now=1_000)

    assert rows == [
        FetchedPlace(
            place_id="manual:0:kórnik",
            name="Kórnik",
            district="Kórnik",
            category="outskirts",
            rating=5.0,
            reviews=999,
            price_level=None,
            quiet=True,
            fact="",
            operational=True,
        )
    ]


# --------------------------------------------------------------------------- #
# Нет ключа Google и нет --manual-only -> SystemExit с понятным текстом
# --------------------------------------------------------------------------- #


async def test_missing_key_without_manual_only_raises_system_exit(tmp_path: Path) -> None:
    settings = _settings(tmp_path, google_key=None)

    with pytest.raises(SystemExit) as excinfo:
        await run(["--queries", "q"], settings=settings)

    assert "GOOGLE_PLACES_KEY" in str(excinfo.value)
    assert not settings.db_path.exists()


# --------------------------------------------------------------------------- #
# Дедуп по place id между двумя запросами
# --------------------------------------------------------------------------- #


async def test_dedup_same_place_id_across_two_queries(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    place = _google_place("places/dup", "Дублирующееся место", reviews_text=["ok"])
    http = _http(
        {
            "query one": {"places": [place]},
            "query two": {"places": [place]},
        }
    )
    try:
        rows = await run(
            ["--queries", "query one;query two", "--no-llm"], settings=settings, http=http
        )
    finally:
        await http.aclose()

    assert len(rows) == 1
    stored = await _stored_rows(settings)
    assert len(stored) == 1


# --------------------------------------------------------------------------- #
# Код-ревью: пропавшее между прогонами место -> operational=0, manual не тронут
# --------------------------------------------------------------------------- #


async def test_place_missing_from_second_run_is_marked_not_operational(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    manual_path = _write_manual(tmp_path, [{"name": "Ручное Место", "quiet": True}])

    # Прогон 0: --manual-only сеет ручную запись (place_id с префиксом "manual:") --
    # она не должна пострадать от последующих настоящих прогонов с Google.
    await run(["--manual-only", "--manual", str(manual_path)], settings=settings)

    place_one = _google_place("places/one", "Место Один", reviews_text=["nice"])
    place_two = _google_place("places/two", "Место Два", reviews_text=["ok"])

    # Прогон 1: оба места на месте.
    http1 = _http({"q": {"places": [place_one, place_two]}})
    try:
        await run(["--queries", "q", "--no-llm"], settings=settings, http=http1)
    finally:
        await http1.aclose()

    stored_after_first = {row.place_id: row for row in await _stored_rows(settings)}
    assert stored_after_first["places/one"].operational is True
    assert stored_after_first["places/two"].operational is True
    manual_place_id = next(pid for pid in stored_after_first if pid.startswith("manual:"))
    assert stored_after_first[manual_place_id].operational is True

    # Прогон 2: "places/two" пропало из ответа Google (закрылось/отфильтровалось).
    http2 = _http({"q": {"places": [place_one]}})
    try:
        await run(["--queries", "q", "--no-llm"], settings=settings, http=http2)
    finally:
        await http2.aclose()

    stored_after_second = {row.place_id: row for row in await _stored_rows(settings)}
    assert stored_after_second["places/one"].operational is True
    assert stored_after_second["places/two"].operational is False
    # Ручная запись из --manual-only не трогается прогонами с Google.
    assert stored_after_second[manual_place_id].operational is True


async def test_dry_run_does_not_mark_places_not_seen(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    place_one = _google_place("places/one", "Место Один", reviews_text=["nice"])
    place_two = _google_place("places/two", "Место Два", reviews_text=["ok"])

    http1 = _http({"q": {"places": [place_one, place_two]}})
    try:
        await run(["--queries", "q", "--no-llm"], settings=settings, http=http1)
    finally:
        await http1.aclose()

    # Прогон с --dry-run "теряет" places/two, но ничего не пишет -- реальные
    # данные должны остаться нетронутыми.
    http2 = _http({"q": {"places": [place_one]}})
    try:
        await run(["--queries", "q", "--no-llm", "--dry-run"], settings=settings, http=http2)
    finally:
        await http2.aclose()

    stored = {row.place_id: row for row in await _stored_rows(settings)}
    assert stored["places/two"].operational is True


# --------------------------------------------------------------------------- #
# Чистые хелперы — покрытие напрямую, без сети
# --------------------------------------------------------------------------- #


def test_pick_district_from_address_and_manual_override() -> None:
    assert pick_district(address="Sikorskiego 38, Wilda, Poznań", manual=None) == "Wilda"
    assert pick_district(address="Puszczykowo, ul. Główna 1", manual=None) == "Puszczykowo"
    assert pick_district(address="Nowhere specific", manual=None) == "Poznań"

    manual = ManualEntry(name="X", quiet=False, category=None, district="Jeżyce")
    assert pick_district(address="Sikorskiego 38, Wilda, Poznań", manual=manual) == "Jeżyce"


def test_pick_category_priority_manual_then_cheap_then_outskirts_then_type() -> None:
    manual = ManualEntry(name="X", quiet=False, category="pub", district=None)
    assert (
        pick_category(manual=manual, review_text="cheap beer", address="", primary_type="bar")
        == "pub"
    )
    assert (
        pick_category(manual=None, review_text="fair prices here", address="", primary_type="bar")
        == "cheap"
    )
    assert (
        pick_category(manual=None, review_text="", address="near Kórnik", primary_type="")
        == "outskirts"
    )
    assert pick_category(manual=None, review_text="", address="", primary_type="bar") == "pub"
    assert pick_category(manual=None, review_text="", address="", primary_type="cafe") == "craft"


def test_parse_price_level_handles_string_enum_int_and_missing() -> None:
    assert parse_price_level("PRICE_LEVEL_MODERATE") == 2
    assert parse_price_level("PRICE_LEVEL_FREE") == 0
    assert parse_price_level(3) == 3
    assert parse_price_level(None) is None
    assert parse_price_level("PRICE_LEVEL_UNSPECIFIED") is None
    assert parse_price_level(True) is None  # bool — подкласс int, но не price_level


def test_build_review_text_limits_count_and_length() -> None:
    reviews = [{"text": {"text": f"review number {i}"}} for i in range(10)]
    text = build_review_text(reviews)

    assert text.count("review number") == 5  # не больше 5 отзывов

    long_reviews = [{"text": {"text": "a" * 2000}}]
    assert len(build_review_text(long_reviews)) == 1500


def test_build_review_text_empty_list_is_empty_string() -> None:
    assert build_review_text([]) == ""


def test_load_manual_reads_quiet_category_district() -> None:
    manual = load_manual(REAL_MANUAL_PATH)
    by_name = {entry.name: entry for entry in manual}

    lalka = by_name["Klubokawiarnia LALKA"]
    assert lalka.quiet is True
    assert lalka.category == "pub"
    assert lalka.district == "Jeżyce"

    piwna_stopa = by_name["Piwna Stopa"]
    assert piwna_stopa.quiet is False
    assert piwna_stopa.category == "craft"


def test_load_manual_missing_file_returns_empty_list(tmp_path: Path) -> None:
    assert load_manual(tmp_path / "does-not-exist.yaml") == []


def test_render_report_contains_header_rows_and_reminder() -> None:
    rows = [
        FetchedPlace(
            place_id="p1",
            name="Тест Бар",
            district="Wilda",
            category="craft",
            rating=4.6,
            reviews=120,
            price_level=2,
            quiet=True,
            fact="тихо",
            operational=True,
        )
    ]
    report = render_report(rows)

    assert "name | district | category" in report
    assert "Тест Бар" in report
    assert "Wilda" in report
    assert "4.6/120" in report
    assert "да" in report
    assert "тихо" in report
    assert "Проверьте fact руками" in report


def test_render_report_empty_list() -> None:
    report = render_report([])
    assert "Всего: 0" in report
