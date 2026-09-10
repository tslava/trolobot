"""Реплей гейта (и, с ``--generate``, полного пайплайна) на выгрузке Telegram Desktop.

``python -m trolobot.replay exports/result.json [--config config.yaml] [--seed 1]
  [--bot-username otec_fedor_bot] [--bot-user-id 0] [--verbose]
  [--generate [--judge] [--max-calls 20]]``

Без ``--generate`` прогоняет ``gate.should_consider`` по каждому сообщению экспорта
с in-memory состоянием (без sqlite, ничего не отправляет и никуда не пишет). При PASS
считает, что ответ отправлен — иначе дневные лимиты и кулдауны не проверить
(см. PLAN.md, этап 2, "Реплей — здесь, а не в этапе 8").

С ``--generate`` (PLAN.md/CLAUDE.md, этап 4) на каждом PASS дополнительно вызывается
тот же путь генерации, что у ``Responder._generate_and_send``: свежий контекст —
последние ``context_window`` сообщений экспорта до текущего, ``recent_replies`` —
уже сгенерированные в этом прогоне реплики, ``build_messages`` -> ``LLMClient.call``
(настоящий HTTP-вызов OpenRouter, ключ из ``Settings().openrouter_api_key``) ->
``parse_reply`` -> ``filters.check_output`` с полным ``FilterContext`` (судья — только
при ``--judge``). Ничего не отправляется и не пишется в постоянную БД: состояние
LLM-бюджетов (``llm_calls``/``llm_spent_usd``/circuit) живёт в ``InMemoryStateStore``
только на время прогона, но лимиты из конфига (``daily_calls_cap``, ``daily_budget_usd``,
предохранитель) при этом действуют по-настоящему — плюс собственный жёсткий потолок
``--max-calls`` на сам прогон, не тратить бюджет впустую при повторных запусках.

``--max-calls`` считается по РЕАЛЬНЫМ сетевым вызовам (основная модель + судья), а не
по числу PASS с генерацией: перед каждым PASS-генерацией читаем ``llm_calls`` для
текущего дня из ``InMemoryStateStore`` и, если он уже ``>= max_calls``, генерацию не
запускаем (счёт по дням гейта при этом продолжается как обычно). Проверка идёт до
вызова, поэтому сама срабатывающая генерация может увеличить счётчик ещё на основной
вызов и, если слои 1-2 прошли или включён shadow, на вызов судьи — превышение
``max_calls`` максимум на один судейский вызов допустимо и не является багом. В
итоговой строке отчёта печатается фактическое число сетевых вызовов из стора
(``InMemoryStateStore.total_llm_calls()``), не число попыток генерации.

``gate.py`` и ``patterns.py`` изначально писались параллельно другими агентами — импорт
внутри ``_run_replay_async()`` оставлен ленивым по той же причине (исторически, чтобы
тесты парсера экспорта не падали до их появления).
"""

from __future__ import annotations

import argparse
import asyncio
import random
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from trolobot.config import load_config
from trolobot.config_models import Config
from trolobot.db import MessageRow
from trolobot.export_parser import ExportMessage, parse_export
from trolobot.few_shot import load_few_shot, render_few_shot
from trolobot.filters import FilterContext, check_output
from trolobot.gate_types import (
    GateMessage,
    GateState,
    RecentActivity,
    StateChange,
    Trigger,
    Verdict,
)
from trolobot.judge import Judge
from trolobot.llm import LLMClient, LLMError
from trolobot.patterns import Patterns
from trolobot.prompt import build_messages, parse_reply, render_context
from trolobot.sanitize import sanitize_display_name
from trolobot.settings import Settings
from trolobot.timeutil import day_key, local_date, local_dt

_TEXT_TRUNCATE_LEN = 120
_TOP_DROP_REASONS = 5

# FilterContext.recent_replies — последние 50 реплик (CLAUDE.md, "Интерфейсы этапа 4"),
# независимо от cfg.behaviour.recent_replies_memory, которым ограничен {recent_replies}
# в самом промпте генерации — см. тот же приём в responder.py.
_FILTER_RECENT_REPLIES = 50


class ReplayState:
    """In-memory снимок состояния гейта для реплея — без sqlite, без gate/patterns.

    Отдельный класс, чтобы окно ``recent`` и инкремент счётчиков были
    тестируемы без модулей gate.py/patterns.py.
    """

    def __init__(self, tz: str, window_min: int) -> None:
        self.tz = tz
        self.window_min = window_min
        self.panic = False
        self.stop_until: int | None = None
        self.topic_cooldown_until: int | None = None
        self.muted_user_ids: frozenset[int] = frozenset()
        self.last_mention_reply_at: int | None = None
        self.last_mention_reply_at_user: dict[int, int] = {}
        self.last_ambient_at: int | None = None
        self.mention_count: dict[str, int] = {}
        self.ambient_count: dict[str, int] = {}
        self._recent: deque[RecentActivity] = deque()

    def push_recent(self, user_id: int, created_at: int) -> None:
        """Добавить не-бот сообщение в окно live_talk и выкинуть устаревшие."""
        self._recent.append(RecentActivity(user_id=user_id, created_at=created_at))
        self._prune_recent(created_at)

    def _prune_recent(self, now: int) -> None:
        cutoff = now - self.window_min * 60
        while self._recent and self._recent[0].created_at < cutoff:
            self._recent.popleft()

    def recent_window(self, now: int) -> tuple[RecentActivity, ...]:
        self._prune_recent(now)
        return tuple(self._recent)

    def gate_state(self, msg: GateMessage, now: int) -> GateState:
        mention_key = day_key("mention_count", now, self.tz)
        ambient_key = day_key("ambient_count", now, self.tz)
        return GateState(
            panic=self.panic,
            stop_until=self.stop_until,
            topic_cooldown_until=self.topic_cooldown_until,
            muted_user_ids=self.muted_user_ids,
            mention_count_today=self.mention_count.get(mention_key, 0),
            last_mention_reply_at=self.last_mention_reply_at,
            last_mention_reply_at_user=self.last_mention_reply_at_user.get(msg.user_id),
            ambient_count_today=self.ambient_count.get(ambient_key, 0),
            last_ambient_at=self.last_ambient_at,
            recent=self.recent_window(now),
        )

    def apply_state_changes(self, changes: Iterable[StateChange]) -> None:
        """Единственный StateChange гейта этапа 2 — topic_cooldown_until."""
        for change in changes:
            if change.key == "topic_cooldown_until":
                self.topic_cooldown_until = None if change.value is None else int(change.value)

    def apply_pass(self, trigger: Trigger, msg: GateMessage, now: int) -> None:
        """PASS => считаем ответ отправленным: инкремент счётчиков и last_*_at."""
        if trigger in (Trigger.MENTION, Trigger.REPLY, Trigger.NAME):
            key = day_key("mention_count", now, self.tz)
            self.mention_count[key] = self.mention_count.get(key, 0) + 1
            self.last_mention_reply_at = now
            self.last_mention_reply_at_user[msg.user_id] = now
        elif trigger is Trigger.AMBIENT:
            key = day_key("ambient_count", now, self.tz)
            self.ambient_count[key] = self.ambient_count.get(key, 0) + 1
            self.last_ambient_at = now


@dataclass
class DayStats:
    date: str
    messages: int = 0
    pass_mention: int = 0
    pass_reply: int = 0
    pass_name: int = 0
    pass_ambient: int = 0
    night: int = 0
    drop_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def pass_total(self) -> int:
        return self.pass_mention + self.pass_reply + self.pass_name + self.pass_ambient

    def record_pass(self, trigger: Trigger) -> None:
        if trigger is Trigger.MENTION:
            self.pass_mention += 1
        elif trigger is Trigger.REPLY:
            self.pass_reply += 1
        elif trigger is Trigger.NAME:
            self.pass_name += 1
        elif trigger is Trigger.AMBIENT:
            self.pass_ambient += 1

    def record_drop(self, reason: str) -> None:
        self.drop_reasons[reason] = self.drop_reasons.get(reason, 0) + 1


@dataclass
class GenerateStats:
    """Счётчики прогона ``--generate``: вызовы, что было бы отправлено, срезы, цена.

    ``calls`` заполняется в конце прогона из ``InMemoryStateStore.total_llm_calls()`` —
    реальное число сетевых вызовов (основная модель + судья), а не число PASS, для
    которых была запущена генерация (см. ``--max-calls`` в докстринге модуля).
    """

    calls: int = 0
    would_send: int = 0
    cost_usd: float = 0.0
    cut_reasons: dict[str, int] = field(default_factory=dict)

    def record_cut(self, reason: str) -> None:
        self.cut_reasons[reason] = self.cut_reasons.get(reason, 0) + 1


class InMemoryStateStore:
    """In-memory реализация ``llm._StateStore`` для ``LLMClient`` в реплее.

    ``day_key()``-счётчики (llm_calls/llm_spent_usd, error streak, circuit) живут
    только в памяти прогона, без sqlite — но бюджеты из конфига (daily_calls_cap,
    daily_budget_usd, предохранитель) при этом действуют по-настоящему, ровно как
    в реальном ``Database``.
    """

    def __init__(self) -> None:
        self._state: dict[str, str] = {}

    async def get_state(self, key: str) -> str | None:
        return self._state.get(key)

    async def set_state(self, key: str, value: str) -> None:
        self._state[key] = value

    async def increment_state(self, key: str, by: int = 1) -> int:
        current = int(self._state.get(key, "0"))
        new_value = current + by
        self._state[key] = str(new_value)
        return new_value

    async def add_state_float(self, key: str, by: float) -> float:
        current = float(self._state.get(key, "0"))
        new_value = current + by
        self._state[key] = str(new_value)
        return new_value

    async def llm_calls_for_day(self, now: int, tz: str) -> int:
        """``llm_calls`` для суток ``now`` (по ``persona.timezone``) — используется
        ``--max-calls`` перед каждой новой PASS-генерацией."""
        raw = await self.get_state(day_key("llm_calls", now, tz))
        return int(raw) if raw is not None else 0

    def total_llm_calls(self) -> int:
        """Сумма ``llm_calls:<день>`` по всем суткам прогона — реальное число сетевых
        вызовов (основная модель + судья), независимо от того, сколько дней покрывает
        экспорт. Используется для финальной строки отчёта."""
        return sum(int(value) for key, value in self._state.items() if key.startswith("llm_calls:"))


@dataclass
class _GenerateContext:
    """Всё, что нужно генерации на протяжении прогона: клиент, судья, шаблоны, стор."""

    llm: LLMClient
    judge: Judge | None
    prompt_template: str
    few_shot_text: str
    store: InMemoryStateStore


def _unique_participant_names(context_rows: list[MessageRow]) -> list[str]:
    """Уникальные display_name не-ботов из context_rows, в порядке первого появления."""
    names: list[str] = []
    seen: set[str] = set()
    for row in context_rows:
        if row.is_bot or not row.display_name or row.display_name in seen:
            continue
        seen.add(row.display_name)
        names.append(row.display_name)
    return names


def _build_generate_context(cfg: Config, use_judge: bool) -> _GenerateContext:
    """Собирает LLMClient (+ судью, если ``--judge``) для прогона ``--generate``.

    Ключ — из ``Settings().openrouter_api_key`` (тот же .env, что у самого бота).
    Нет ключа -> понятная ошибка вместо невнятного сбоя внутри LLMClient.
    """
    settings = Settings()
    if settings.openrouter_api_key is None:
        raise RuntimeError(
            "--generate требует OPENROUTER_API_KEY: задайте его в .env "
            "(см. README, раздел «Реплей»)"
        )

    prompt_template = settings.prompt_path.read_text(encoding="utf-8")
    few_shot_items = load_few_shot(settings.few_shot_path)
    few_shot_text = render_few_shot(few_shot_items)

    store = InMemoryStateStore()
    http = httpx.AsyncClient(timeout=cfg.llm.timeout_sec)
    llm = LLMClient(
        api_key=settings.openrouter_api_key.get_secret_value(),
        cfg_getter=lambda: cfg,
        db=store,
        http=http,
    )

    judge: Judge | None = None
    if use_judge:
        judge_prompt = settings.judge_prompt_path.read_text(encoding="utf-8")
        judge = Judge(llm, lambda: cfg, judge_prompt)

    return _GenerateContext(
        llm=llm,
        judge=judge,
        prompt_template=prompt_template,
        few_shot_text=few_shot_text,
        store=store,
    )


async def _generate_one(
    ctx: _GenerateContext,
    cfg: Config,
    tz: str,
    exported: ExportMessage,
    trigger: Trigger,
    display_name: str,
    context_rows: list[MessageRow],
    generated_replies: list[str],
    stats: GenerateStats,
    patterns: Patterns,
) -> str:
    """Один PASS с включённым ``--generate``: генерация + выходной фильтр.

    Возвращает готовую строку отчёта: "время | имя | триггер | сообщение → реплика | вердикт".
    По тому же пути, что ``Responder._generate_and_send``: настоящий вызов LLM, затем
    ``filters.check_output`` с полным ``FilterContext``. Ничего не отправляется — только
    печатается и учитывается в ``stats``. Реальное число сетевых вызовов считается не
    здесь, а из ``ctx.store.total_llm_calls()`` в конце прогона (``stats.calls``,
    см. докстринг модуля про ``--max-calls``).
    """
    when = local_dt(exported.created_at, tz).strftime("%Y-%m-%d %H:%M:%S")
    trigger_text = (
        exported.text if trigger in (Trigger.MENTION, Trigger.REPLY, Trigger.NAME) else ""
    )

    age = cfg.persona.age(local_date(exported.created_at, tz))
    recent_for_prompt = generated_replies[-cfg.behaviour.recent_replies_memory :]
    messages = build_messages(
        ctx.prompt_template,
        age=age,
        few_shot=ctx.few_shot_text,
        context=render_context(context_rows),
        recent_replies="\n".join(recent_for_prompt),
        places="",
        situation="",
    )

    def _line(reply_text: str, verdict_str: str) -> str:
        return (
            f"{when} | {display_name} | {trigger.value} | "
            f"{_truncate(exported.text)} → {_truncate(reply_text)} | {verdict_str}"
        )

    try:
        result = await ctx.llm.call(
            messages,
            model=cfg.llm.main_model,
            max_tokens=cfg.llm.max_tokens,
            now=exported.created_at,
        )
    except LLMError as exc:
        stats.record_cut(exc.reason)
        return _line("(нет ответа)", f"cut: {exc.reason}")

    stats.cost_usd += result.cost_usd

    reply = parse_reply(result.text)
    if reply is None:
        stats.record_cut("llm:invalid_json")
        return _line("(нет ответа)", "cut: llm:invalid_json")
    if not reply.speak:
        stats.record_cut("llm:silent")
        return _line("(молчание)", "cut: llm:silent")

    participant_names = _unique_participant_names(context_rows)
    bot_names = [cfg.persona.name, cfg.persona.display_name, *cfg.persona.name_triggers]
    filter_ctx = FilterContext(
        cfg=cfg,
        recent_replies=generated_replies[-_FILTER_RECENT_REPLIES:],
        context_rows=context_rows,
        places_names=[],
        participant_names=participant_names,
        bot_names=bot_names,
        muted_names=[],  # /mute не симулируется в реплее
        patterns=patterns,
        system_prompt=ctx.prompt_template,
        trigger_text=trigger_text,
        now=exported.created_at,
    )
    verdict = await check_output(reply.text, filter_ctx, ctx.judge)

    if verdict.ok:
        stats.would_send += 1
        generated_replies.append(reply.text)
        return _line(reply.text, "pass")

    reasons = verdict.reasons or (verdict.reason,)
    for reason in reasons:
        stats.record_cut(reason)
    if cfg.filters.shadow:
        # shadow: считает и режет только в отчёте, реально отправил бы.
        stats.would_send += 1
        generated_replies.append(reply.text)
    return _line(reply.text, ", ".join(reasons))


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m trolobot.replay",
        description=(
            "Прогоняет гейт (и, с --generate, полный пайплайн генерации и выходного "
            "фильтра) по экспорту Telegram Desktop (result.json). Ничего не отправляет "
            "и не пишет в БД."
        ),
    )
    parser.add_argument("export_path", type=Path, help="Путь к result.json из экспорта")
    parser.add_argument(
        "--config", type=Path, default=Path("config.yaml"), help="Путь к config.yaml"
    )
    parser.add_argument("--seed", type=int, default=1, help="Seed для random.Random (шаг 11 гейта)")
    parser.add_argument(
        "--bot-username", default="", help="Username бота без @ — для детекта упоминаний"
    )
    parser.add_argument(
        "--bot-user-id", type=int, default=0, help="user_id бота в экспорте, 0 = неизвестен"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Печатать каждую PASS-строку отдельно"
    )
    parser.add_argument(
        "--generate",
        action="store_true",
        help=(
            "На каждом PASS реально сгенерировать реплику и прогнать её через выходной "
            "фильтр (нужен OPENROUTER_API_KEY в .env). Ничего не отправляется."
        ),
    )
    parser.add_argument(
        "--judge",
        action="store_true",
        help="Включить LLM-судью (слой 3 выходного фильтра) при --generate",
    )
    parser.add_argument(
        "--max-calls",
        type=int,
        default=20,
        dest="max_calls",
        help="Потолок реальных сетевых вызовов LLM за прогон --generate (по умолчанию 20)",
    )
    return parser.parse_args(argv)


def _truncate(text: str, limit: int = _TEXT_TRUNCATE_LEN) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


async def _run_replay_async(args: argparse.Namespace) -> str:
    # gate.py исторически писался параллельно — ленивый импорт (см. докстринг модуля).
    # patterns.py импортируется на уровне модуля (нужен и для типа Patterns в _generate_one).
    from trolobot.gate import should_consider

    generate = bool(getattr(args, "generate", False))
    use_judge = bool(getattr(args, "judge", False))
    max_calls = int(getattr(args, "max_calls", 20))

    cfg = load_config(Path(args.config))
    patterns = Patterns(cfg.filters, cfg.persona.name_triggers, args.bot_username)
    reserved = {
        cfg.persona.name,
        cfg.persona.display_name,
        *cfg.persona.name_triggers,
        args.bot_username,
    }

    export_messages = parse_export(Path(args.export_path), cfg.persona.timezone)

    rng = random.Random(args.seed)
    state = ReplayState(cfg.persona.timezone, cfg.behaviour.live_talk.window_min)
    bot_message_ids: set[int] = set()

    day_stats: dict[str, DayStats] = {}
    verbose_lines: list[str] = []
    generate_lines: list[str] = []
    gen_stats = GenerateStats()

    gen_ctx: _GenerateContext | None = None
    if generate:
        gen_ctx = _build_generate_context(cfg, use_judge)

    context_buffer: deque[MessageRow] = deque(maxlen=cfg.behaviour.context_window)
    generated_replies: list[str] = []

    try:
        for exported in export_messages:
            is_bot = args.bot_user_id != 0 and exported.user_id == args.bot_user_id
            reply_to_bot = (
                exported.reply_to_tg_message_id is not None
                and exported.reply_to_tg_message_id in bot_message_ids
            )
            display_name = sanitize_display_name(exported.display_name, exported.user_id, reserved)

            msg = GateMessage(
                chat_id=0,
                tg_message_id=exported.tg_message_id,
                user_id=exported.user_id,
                is_bot=is_bot,
                text=exported.text,
                reply_to_bot=reply_to_bot,
                created_at=exported.created_at,
            )

            if not is_bot:
                state.push_recent(msg.user_id, msg.created_at)

            context_buffer.append(
                MessageRow(
                    id=exported.tg_message_id,
                    tg_message_id=exported.tg_message_id,
                    chat_id=0,
                    user_id=exported.user_id,
                    display_name=display_name,
                    text=exported.text,
                    reply_to_tg_message_id=exported.reply_to_tg_message_id,
                    is_bot=is_bot,
                    created_at=exported.created_at,
                )
            )

            gate_state = state.gate_state(msg, msg.created_at)
            decision = should_consider(msg, gate_state, cfg, patterns, msg.created_at, rng)
            state.apply_state_changes(decision.state_changes)

            date_str = local_date(msg.created_at, cfg.persona.timezone).isoformat()
            stats = day_stats.setdefault(date_str, DayStats(date=date_str))
            stats.messages += 1

            if decision.verdict == Verdict.PASS:
                assert decision.trigger is not None
                state.apply_pass(decision.trigger, msg, msg.created_at)
                stats.record_pass(decision.trigger)
                if args.verbose:
                    when = local_dt(msg.created_at, cfg.persona.timezone).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    verbose_lines.append(
                        f"{when} | {display_name} | {decision.trigger.value} | "
                        f"{_truncate(exported.text)}"
                    )
                if gen_ctx is not None:
                    # Потолок --max-calls — по РЕАЛЬНЫМ сетевым вызовам (основная модель
                    # + судья), не по числу PASS: читаем llm_calls для суток этого
                    # сообщения из стора перед тем, как решить, генерировать ли ещё.
                    # Проверка — до вызова, поэтому сама генерация ниже может увеличить
                    # счётчик ещё на один (основной) или два (основной + судья) вызова —
                    # допустимое превышение max_calls максимум на один судейский вызов,
                    # см. докстринг модуля.
                    calls_so_far = await gen_ctx.store.llm_calls_for_day(
                        exported.created_at, cfg.persona.timezone
                    )
                    if calls_so_far < max_calls:
                        line = await _generate_one(
                            gen_ctx,
                            cfg,
                            cfg.persona.timezone,
                            exported,
                            decision.trigger,
                            display_name,
                            list(context_buffer),
                            generated_replies,
                            gen_stats,
                            patterns,
                        )
                        generate_lines.append(line)
            elif decision.verdict == Verdict.QUEUE_NIGHT:
                stats.night += 1
            else:
                stats.record_drop(decision.reason)

            if is_bot:
                bot_message_ids.add(exported.tg_message_id)
    finally:
        if gen_ctx is not None:
            # Реальное число сетевых вызовов за весь прогон (не число PASS-генераций) —
            # печатается в итоговой строке отчёта.
            gen_stats.calls = gen_ctx.store.total_llm_calls()
            await gen_ctx.llm.aclose()

    return _format_report(
        day_stats,
        verbose_lines,
        verbose=args.verbose,
        generate_lines=generate_lines,
        gen_stats=gen_stats if generate else None,
    )


def run_replay(args: argparse.Namespace) -> str:
    """Прогоняет гейт (и, с ``--generate``, генерацию) по экспорту, возвращает отчёт."""
    return asyncio.run(_run_replay_async(args))


def _format_report(
    day_stats: dict[str, DayStats],
    verbose_lines: list[str],
    verbose: bool,
    generate_lines: list[str] | None = None,
    gen_stats: GenerateStats | None = None,
) -> str:
    lines: list[str] = []

    if generate_lines:
        lines.append("Генерация:")
        lines.extend(generate_lines)
        lines.append("")
    elif verbose and verbose_lines:
        lines.append("PASS:")
        lines.extend(verbose_lines)
        lines.append("")

    lines.append("дата       | msgs | pass mention/reply/name/ambient | night | drop (top-5)")
    for date_str in sorted(day_stats):
        stats = day_stats[date_str]
        top_drops = sorted(stats.drop_reasons.items(), key=lambda kv: (-kv[1], kv[0]))[
            :_TOP_DROP_REASONS
        ]
        drops_str = ", ".join(f"{reason}={count}" for reason, count in top_drops) or "-"
        lines.append(
            f"{stats.date} | {stats.messages:4d} | "
            f"{stats.pass_mention}/{stats.pass_reply}/{stats.pass_name}/{stats.pass_ambient} | "
            f"{stats.night:5d} | {drops_str}"
        )

    days = len(day_stats)
    total_messages = sum(s.messages for s in day_stats.values())
    total_pass = sum(s.pass_total for s in day_stats.values())
    total_ambient = sum(s.pass_ambient for s in day_stats.values())
    days_without_pass = sum(1 for s in day_stats.values() if s.pass_total == 0)
    avg_pass = total_pass / days if days else 0.0
    avg_ambient = total_ambient / days if days else 0.0

    lines.append("")
    lines.append(
        f"Итого: дней={days}, сообщений={total_messages}, "
        f"pass={total_pass} (в среднем {avg_pass:.2f}/день), "
        f"ambient={total_ambient} (в среднем {avg_ambient:.2f}/день), "
        f"дней без единого pass={days_without_pass}"
    )

    if gen_stats is not None:
        top_cuts = sorted(gen_stats.cut_reasons.items(), key=lambda kv: (-kv[1], kv[0]))[
            :_TOP_DROP_REASONS
        ]
        cuts_str = ", ".join(f"{reason}={count}" for reason, count in top_cuts) or "-"
        lines.append(
            f"Генерация: вызовов={gen_stats.calls}, отправлено бы={gen_stats.would_send}, "
            f"срезано (топ-5): {cuts_str}, потрачено $={gen_stats.cost_usd:.4f}"
        )

    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    print(run_replay(args))


if __name__ == "__main__":
    main()
