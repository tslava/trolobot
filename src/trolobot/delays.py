"""Задержки ответа: взвешенные бакеты, быстрый бакет для схлопывания, дебаунс.

Чистые функции этапа 3 (PLAN.md, "Отложенный ответ" и "Срочное — в быстрый
бакет"). ``rng`` передаётся снаружи, чтобы тесты были детерминированными.
"""

from __future__ import annotations

import random

from trolobot.config_models import BehaviourConfig


def pick_delay(cfg: BehaviourConfig, rng: random.Random, *, urgent: bool) -> int:
    """Выбирает бакет по весам (rng.random() кумулятивно) и секунды внутри него.

    urgent=True обрезает результат до cfg.urgent_max_delay_sec — срочное
    ("сегодня", "куда идём"...) не должно ждать час, даже если выпал долгий бакет.
    """
    roll = rng.random()
    cumulative = 0.0
    buckets = cfg.reply_delay_buckets
    chosen = buckets[-1]
    for bucket in buckets:
        cumulative += bucket.weight
        if roll < cumulative:
            chosen = bucket
            break

    lo, hi = chosen.range_sec
    delay = rng.randint(lo, hi)
    if urgent:
        delay = min(delay, cfg.urgent_max_delay_sec)
    return delay


def fast_delay(cfg: BehaviourConfig, rng: random.Random) -> int:
    """Задержка по первому (самому быстрому) бакету — схлопывание повторного обращения."""
    lo, hi = cfg.reply_delay_buckets[0].range_sec
    return rng.randint(lo, hi)


def debounce_seconds(cfg: BehaviourConfig, rng: random.Random) -> float:
    """Пауза дебаунса перед сборкой контекста: rng.uniform внутри debounce_sec."""
    lo, hi = cfg.debounce_sec
    return rng.uniform(lo, hi)
