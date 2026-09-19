"""Погода как фон жизни персонажа (CLAUDE.md, "Интерфейсы: погода" и
"Интерфейсы: погода в другом месте").

Повод — живой случай 19.09.2026: на «какая погода завтра в Познани?» персонаж
ответил «узнаю по коленке». Теперь у него есть настоящие данные. Источник —
Open-Meteo: публичный API без ключа и регистрации.

Погода — фон, а не справочная услуга: одна-две строки уходят в системный промпт,
и модель сама решает, к слову они или нет. Домашняя точка (координаты и название)
живёт в ``.env``, а не в конфиге и не в коде: репозиторий публичный, а где живёт
владелец — его личные данные. Координат нет -> ``get()`` без места отдаёт ``None``,
блок погоды пустой, всё остальное работает как раньше.

Если в обращении спрашивают про другое место («а в Варшаве?»), название достаёт
из текста дешёвая модель (``WeatherPlaceExtractor`` — тот же приём, что у
``followup.FollowupChecker``: один вызов, только когда есть повод), координаты
даёт геокодер Open-Meteo, и в промпт добавляется вторая строка.

Сеть есть только в этом модуле, и наружу её ошибки не выходят: любая неудача —
``logger.warning`` и прошлый (пусть протухший) кэш либо ``None``. Молчание про
погоду дешевле, чем сорванный ответ.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from trolobot.config_models import Config
from trolobot.llm import LLMClient, LLMError
from trolobot.timeutil import local_date

logger = logging.getLogger(__name__)

_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

# Свой суточный счётчик попыток (как vision_calls/followup_calls): дешёвый вызов
# не должен съедать потолок вызовов основной модели.
_COUNTER_KEY = "weather_calls"

# Название длиннее — это уже не место, а пересказ сообщения: модель не поняла
# задачу, геокодер такое всё равно не найдёт.
_PLACE_MAX_LEN = 60

# Страна не добавляется к названию, если место дома, в Польше: «Kórnik» звучит
# как место, куда персонаж ездит, а «Kórnik, Польша» — как справка из интернета.
# language=ru геокодера отдаёт страну по-русски, поэтому вариантов несколько.
_HOME_COUNTRIES = frozenset({"польша", "poland", "polska"})

_SLOT_RE = re.compile(r"\{(text)\}")
_LT_RUN_RE = re.compile(r"<{3,}")
_GT_RUN_RE = re.compile(r">{3,}")
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL | re.IGNORECASE)

# Коды WMO Open-Meteo -> короткое описание по-русски. Ровно те коды, которые
# API действительно возвращает; всё остальное -> "" (строка без описания).
WMO: dict[int, str] = {
    0: "ясно",
    1: "переменная облачность",
    2: "переменная облачность",
    3: "пасмурно",
    45: "туман",
    48: "туман",
    51: "морось",
    53: "морось",
    55: "морось",
    56: "морось",
    57: "морось",
    61: "дождь",
    63: "дождь",
    65: "дождь",
    66: "ледяной дождь",
    67: "ледяной дождь",
    71: "снег",
    73: "снег",
    75: "снег",
    77: "снег",
    80: "ливни",
    81: "ливни",
    82: "ливни",
    85: "снегопад",
    86: "снегопад",
    95: "гроза",
    96: "гроза",
    99: "гроза",
}


@dataclass(frozen=True, slots=True)
class Weather:
    temp_now: float
    code_now: int
    today_min: float
    today_max: float
    today_code: int
    tomorrow_min: float
    tomorrow_max: float
    tomorrow_code: int
    fetched_at: int


@dataclass(frozen=True, slots=True)
class Place:
    """Точка на карте. ``name`` — как её зовут в промпте: для домашней точки это
    ``Settings.weather_home_name`` (может быть пустым), для найденной геокодером —
    то, что вернул геокодер."""

    name: str
    latitude: float
    longitude: float


def describe(code: int) -> str:
    """Код WMO -> описание по-русски; неизвестный код -> "" (строка без описания)."""
    return WMO.get(code, "")


def format_temp(value: float) -> str:
    """Температура целым числом со знаком: "+9", "-3", "0" (ноль без знака).

    Публичная: той же формой пользуется строка погоды в ``/status``
    (commands.py), чтобы «+9» в промпте и в статусе выглядело одинаково."""
    rounded = round(value)
    if rounded == 0:
        return "0"
    return f"{rounded:+d}"


def _same_local_day(fetched_at: int, tz: str, now: int) -> bool:
    """Снимок погоды сделан в те же локальные сутки, что и ``now``.

    Важно для честности «днём» и «завтра»: при долгом обрыве сети клиент отдаёт
    прошлый кэш (лучше вчерашняя погода, чем никакой), но если он снят вчера, то
    «завтра» в нём — это уже сегодня, и строка врала бы. Такой снимок не
    рендерится вовсе — в промпте просто не будет абзаца про погоду.
    """
    return local_date(fetched_at, tz) == local_date(now, tz)


def _with_desc(prefix: str, code: int, *, joiner: str) -> str:
    text = describe(code)
    if not text:
        return prefix
    return f"{prefix}{joiner}{text}"


def render_weather(w: Weather | None, tz: str, now: int, home_name: str = "") -> str:
    """Блок домашней погоды для системного промпта. ``None`` -> "" (абзац исчезает).

    Название домашней точки в текст НЕ подставляется: русские названия пришлось бы
    склонять («в Познань» вместо «в Познани»), а модель и так знает из промпта, где
    персонаж живёт. ``home_name`` нужен только чтобы не выводить второй блок, когда
    спросили про свой же город (``responder``). Строки НЕ начинаются с «- »:
    ``filters.regex:prompt_leak`` считает инструктивной частью промпта именно
    строки-буллеты, и погода-буллет срезала бы ответ персонажа как утечку.
    Часов и дат в строке нет — модель не должна пересказывать «данные на 12:30».
    """
    if w is None or not _same_local_day(w.fetched_at, tz, now):
        return ""
    now_part = _with_desc(f"сейчас {format_temp(w.temp_now)}", w.code_now, joiner=" и ")
    first = (
        "Погода за окном (это фон, упоминай только если к слову): "
        f"{now_part}, днём от {format_temp(w.today_min)} до {format_temp(w.today_max)}."
    )
    second = _with_desc(
        f"Завтра от {format_temp(w.tomorrow_min)} до {format_temp(w.tomorrow_max)}",
        w.tomorrow_code,
        joiner=", ",
    )
    return f"{first}\n{second}."


def render_weather_place(place_name: str, w: Weather | None, tz: str, now: int) -> str:
    """Вторая строка блока — погода в месте, про которое спросили. ``None`` -> ""."""
    if w is None or not _same_local_day(w.fetched_at, tz, now):
        return ""
    now_part = _with_desc(f"сейчас {format_temp(w.temp_now)}", w.code_now, joiner=" и ")
    tomorrow_part = _with_desc(
        f"завтра от {format_temp(w.tomorrow_min)} до {format_temp(w.tomorrow_max)}",
        w.tomorrow_code,
        joiner=", ",
    )
    # Название в именительном падеже: «Спрашивают про Варшава» было бы ошибкой, а
    # склонять названия из геокодера нечем — поэтому двоеточие после «место».
    return f"Спрашивают про место: {place_name}. {now_part.capitalize()}, {tomorrow_part}."


def _strip_fake_delimiters(text: str) -> str:
    return _GT_RUN_RE.sub(" ", _LT_RUN_RE.sub(" ", text))


def _strip_code_fence(text: str) -> str:
    match = _CODE_FENCE_RE.match(text)
    if match is not None:
        return match.group(1).strip()
    return text


def _parse_weather(data: Any, now: int) -> Weather:
    """Ответ Open-Meteo -> Weather. Любое расхождение с форматом — исключение
    (KeyError/IndexError/TypeError/ValueError), которое ловит вызывающий."""
    current = data["current"]
    daily = data["daily"]
    mins = daily["temperature_2m_min"]
    maxs = daily["temperature_2m_max"]
    codes = daily["weather_code"]
    return Weather(
        temp_now=float(current["temperature_2m"]),
        code_now=int(current["weather_code"]),
        today_min=float(mins[0]),
        today_max=float(maxs[0]),
        today_code=int(codes[0]),
        tomorrow_min=float(mins[1]),
        tomorrow_max=float(maxs[1]),
        tomorrow_code=int(codes[1]),
        fetched_at=now,
    )


def _parse_place(data: Any) -> Place:
    """Ответ геокодера -> Place. Страна дописывается к названию, если это не
    Польша: «Варшава» и так понятно, а «Кёльн, Германия» — уже другая история."""
    results = data["results"]
    first = results[0]
    name = str(first["name"]).strip()
    country = str(first.get("country") or "").strip()
    country_code = str(first.get("country_code") or "").strip()
    if (
        name
        and country
        and country_code.upper() != "PL"
        and country.casefold() not in _HOME_COUNTRIES
    ):
        name = f"{name}, {country}"
    return Place(name=name, latitude=float(first["latitude"]), longitude=float(first["longitude"]))


@dataclass(frozen=True, slots=True)
class _GeocodeEntry:
    """Результат геокодирования в кэше. ``place`` = None — «искали, не нашли»:
    отрицательный ответ кэшируется тоже, чтобы не долбить геокодер одним и тем
    же несуществующим местом."""

    place: Place | None
    fetched_at: int


class WeatherClient:
    """Открытый API Open-Meteo с кэшем в памяти. Единственное место в проекте,
    которое ходит за погодой в сеть.

    ``home`` — домашняя точка из ``.env`` (app.py собирает её из
    ``WEATHER_LATITUDE``/``WEATHER_LONGITUDE``/``WEATHER_HOME_NAME``). ``None``
    значит «домашней точки нет»: ``get()`` без аргумента вернёт ``None``, а
    погода по спрошенному месту всё равно работает.
    """

    def __init__(
        self,
        cfg_getter: Callable[[], Config],
        home: Place | None = None,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        self._cfg_getter = cfg_getter
        self._home = home
        self._http = (
            http
            if http is not None
            else httpx.AsyncClient(timeout=cfg_getter().behaviour.weather.timeout_sec)
        )
        self._clock = clock
        # Кэш по округлённым до сотых координатам: домашняя точка и каждое
        # спрошенное место живут отдельно и не перетирают друг друга.
        self._cache: dict[tuple[float, float], Weather] = {}
        self._geocode_cache: dict[str, _GeocodeEntry] = {}
        # Два разных лока: погода и геокодер зовутся последовательно (сначала
        # geocode, потом get), но брать один лок дважды подряд незачем, а вложить
        # их друг в друга было бы легко испортить будущей правкой.
        self._lock = asyncio.Lock()
        self._geocode_lock = asyncio.Lock()

    @property
    def home_name(self) -> str:
        """Как звать домашнюю точку в промпте; "" — названия нет или точки нет."""
        return self._home.name if self._home is not None else ""

    @property
    def home(self) -> Place | None:
        return self._home

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(self, place: Place | None = None) -> Weather | None:
        """Погода в ``place`` (или в домашней точке). Свежий кэш — без сети.

        Ошибка сети/разбора -> прошлый кэш этой же точки, даже протухший: лучше
        вчерашняя погода, чем никакой (за честность «сегодня/завтра» отвечает
        ``render_weather``, который вчерашний снимок не рендерит).
        """
        cfg = self._cfg_getter()
        weather_cfg = cfg.behaviour.weather
        if not weather_cfg.enabled:
            return None

        target = place if place is not None else self._home
        if target is None:
            return None
        key = (round(target.latitude, 2), round(target.longitude, 2))

        cached = self._cache.get(key)
        if cached is not None and self._clock() - cached.fetched_at < weather_cfg.ttl_min * 60:
            return cached

        async with self._lock:
            # Перепроверка под локом: пока ждали своей очереди, параллельный
            # вызов мог уже сходить в сеть — второй запрос не нужен.
            cached = self._cache.get(key)
            now = self._clock()
            if cached is not None and now - cached.fetched_at < weather_cfg.ttl_min * 60:
                return cached

            fetched = await self._fetch(cfg, target, now)
            if fetched is None:
                return self._cache.get(key)
            self._cache[key] = fetched
            return fetched

    async def _fetch(self, cfg: Config, place: Place, now: int) -> Weather | None:
        weather_cfg = cfg.behaviour.weather
        params: dict[str, str | float | int] = {
            "latitude": place.latitude,
            "longitude": place.longitude,
            "current": "temperature_2m,weather_code",
            "daily": "temperature_2m_min,temperature_2m_max,weather_code",
            "timezone": cfg.persona.timezone,
            "forecast_days": 2,
        }
        try:
            response = await self._http.get(
                _FORECAST_URL, params=params, timeout=weather_cfg.timeout_sec
            )
        except httpx.HTTPError as exc:
            # TimeoutException — тоже HTTPError, отдельной ветки не нужно.
            logger.warning("weather request failed: error=%s", type(exc).__name__)
            return None

        if not (200 <= response.status_code < 300):
            logger.warning("weather http error: status=%s", response.status_code)
            return None

        try:
            return _parse_weather(response.json(), now)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            logger.warning("weather parse failed: error=%s", type(exc).__name__)
            return None

    async def geocode(self, query: str) -> Place | None:
        """Название места -> координаты. Кэш по строке запроса, TTL
        ``geocode_ttl_days``; отрицательный результат кэшируется тоже."""
        cfg = self._cfg_getter()
        weather_cfg = cfg.behaviour.weather
        key = query.strip().lower()
        if not key:
            return None
        ttl_sec = weather_cfg.geocode_ttl_days * 86400

        entry = self._geocode_cache.get(key)
        if entry is not None and self._clock() - entry.fetched_at < ttl_sec:
            return entry.place

        async with self._geocode_lock:
            entry = self._geocode_cache.get(key)
            now = self._clock()
            if entry is not None and now - entry.fetched_at < ttl_sec:
                return entry.place

            place = await self._geocode_request(query.strip(), weather_cfg.timeout_sec)
            self._geocode_cache[key] = _GeocodeEntry(place=place, fetched_at=now)
            return place

    async def _geocode_request(self, query: str, timeout_sec: int) -> Place | None:
        params: dict[str, str | int] = {
            "name": query,
            "count": 1,
            "language": "ru",
            "format": "json",
        }
        try:
            response = await self._http.get(_GEOCODE_URL, params=params, timeout=timeout_sec)
        except httpx.HTTPError as exc:
            logger.warning("geocode request failed: error=%s", type(exc).__name__)
            return None

        if not (200 <= response.status_code < 300):
            logger.warning("geocode http error: status=%s", response.status_code)
            return None

        try:
            return _parse_place(response.json())
        except (KeyError, IndexError, TypeError, ValueError):
            # Ничего не нашлось — в ответе просто нет ключа "results". Это
            # обычное дело («под Кórnik-ом»), не ошибка сервиса.
            logger.warning("geocode: место не найдено")
            return None


class WeatherPlaceExtractor:
    """Достаёт название места из вопроса про погоду одним дешёвым вызовом.

    Русские склонения («в Варшаве», «под Гданьском») геокодер не понимает, а
    регулярками их не разобрать — поэтому именительный падеж восстанавливает
    модель. Вызов делается, только когда сработала регулярка повода
    (``patterns.weather_request``) и только для прямых обращений: см.
    ``responder._generate_and_send``.

    Любой сбой (пустая модель, ``LLMError``, не JSON, слишком длинный ответ) —
    ``None``: тогда в промпт уходит только домашняя погода.
    """

    def __init__(
        self, llm: LLMClient, cfg_getter: Callable[[], Config], prompt_template: str
    ) -> None:
        self._llm = llm
        self._cfg_getter = cfg_getter
        self._prompt_template = prompt_template

    async def extract(self, text: str, *, now: int) -> str | None:
        cfg = self._cfg_getter()
        weather_cfg = cfg.behaviour.weather
        model = weather_cfg.lookup_model or cfg.llm.judge_model
        if not model:
            return None

        slot_values = {"text": _strip_fake_delimiters(text).strip()}
        system = _SLOT_RE.sub(lambda m: slot_values[m.group(1)], self._prompt_template)

        try:
            result = await self._llm.call(
                [{"role": "system", "content": system}],
                model=model,
                max_tokens=weather_cfg.lookup_max_tokens,
                now=now,
                counter_key=_COUNTER_KEY,
                calls_cap=weather_cfg.lookup_daily_cap,
            )
        except LLMError as exc:
            logger.warning("weather place llm error: reason=%s", exc.reason)
            return None

        place = _parse_place_name(result.text)
        if place is None:
            logger.info("weather place: места в сообщении нет")
            return None
        logger.info("weather place: %s", place)
        return place


def _parse_place_name(raw: str) -> str | None:
    """``{"place": "Варшава"|null}`` -> название или None.

    Разбор такой же строгий и терпимый, как ``judge._parse_verdict``: срез
    ```json``` обёрток, поиск первой "{" и ``raw_decode`` от неё. Пустая строка,
    не строка и слишком длинный ответ — тоже None (модель не поняла задачу).
    """
    try:
        candidate = _strip_code_fence(raw.strip())
        start = candidate.find("{")
        if start == -1:
            return None
        data, _end = json.JSONDecoder().raw_decode(candidate, start)
        if not isinstance(data, dict):
            return None

        value = data.get("place")
        if not isinstance(value, str):
            return None
        place = _strip_fake_delimiters(value).strip()
        if not place or len(place) > _PLACE_MAX_LEN:
            return None
        return place
    except Exception:
        # Ответ модели — недоверенный внешний текст: любой сбой разбора значит
        # «места нет», а не падение ответа целиком.
        return None
