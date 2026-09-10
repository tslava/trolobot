"""Реплей гейта на выгрузке Telegram Desktop — CLI и отчёт в stdout.

``python -m trolobot.replay exports/result.json [--config config.yaml] [--seed 1]
  [--bot-username otec_fedor_bot] [--bot-user-id 0] [--verbose]``

Прогоняет ``gate.should_consider`` по каждому сообщению экспорта с in-memory
состоянием (без sqlite, ничего не отправляет и никуда не пишет). При PASS
считает, что ответ отправлен — иначе дневные лимиты и кулдауны не проверить
(см. PLAN.md, этап 2, "Реплей — здесь, а не в этапе 8").

``gate.py`` и ``patterns.py`` пишутся параллельно другими агентами и на момент
написания этого модуля могли ещё не существовать — импорт лениво, внутри
``run_replay()``, чтобы тесты парсера экспорта не падали до их появления.
"""

from __future__ import annotations

import argparse
import random
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from trolobot.config import load_config
from trolobot.export_parser import parse_export
from trolobot.gate_types import (
    GateMessage,
    GateState,
    RecentActivity,
    StateChange,
    Trigger,
    Verdict,
)
from trolobot.sanitize import sanitize_display_name
from trolobot.timeutil import day_key, local_date, local_dt

_TEXT_TRUNCATE_LEN = 120
_TOP_DROP_REASONS = 5


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


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m trolobot.replay",
        description=(
            "Прогоняет гейт по экспорту Telegram Desktop (result.json). "
            "Ничего не отправляет и не пишет в БД."
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
    return parser.parse_args(argv)


def _truncate(text: str, limit: int = _TEXT_TRUNCATE_LEN) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def run_replay(args: argparse.Namespace) -> str:
    """Прогоняет гейт по экспорту, возвращает текстовый отчёт (main() его печатает)."""
    # gate.py/patterns.py пишутся параллельно — ленивый импорт, чтобы тесты
    # парсера экспорта не падали до их появления (см. докстринг модуля).
    from trolobot.gate import should_consider
    from trolobot.patterns import Patterns

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
                when = local_dt(msg.created_at, cfg.persona.timezone).strftime("%Y-%m-%d %H:%M:%S")
                verbose_lines.append(
                    f"{when} | {display_name} | {decision.trigger.value} | "
                    f"{_truncate(exported.text)}"
                )
        elif decision.verdict == Verdict.QUEUE_NIGHT:
            stats.night += 1
        else:
            stats.record_drop(decision.reason)

        if is_bot:
            bot_message_ids.add(exported.tg_message_id)

    return _format_report(day_stats, verbose_lines, verbose=args.verbose)


def _format_report(day_stats: dict[str, DayStats], verbose_lines: list[str], verbose: bool) -> str:
    lines: list[str] = []

    if verbose and verbose_lines:
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

    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    print(run_replay(args))


if __name__ == "__main__":
    main()
