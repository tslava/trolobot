"""Тесты для delays.py: распределение по бакетам, границы, urgent, дебаунс."""

from __future__ import annotations

import random

from trolobot.config_models import BehaviourConfig
from trolobot.delays import debounce_seconds, fast_delay, pick_delay


def _cfg() -> BehaviourConfig:
    return BehaviourConfig()


def test_pick_delay_bucket_distribution_matches_weights() -> None:
    cfg = _cfg()
    rng = random.Random(42)
    buckets = cfg.reply_delay_buckets
    counts = [0 for _ in buckets]
    n = 10000

    for _ in range(n):
        delay = pick_delay(cfg, rng, urgent=False)
        for i, bucket in enumerate(buckets):
            lo, hi = bucket.range_sec
            if lo <= delay <= hi:
                counts[i] += 1
                break

    for count, bucket in zip(counts, buckets, strict=True):
        fraction = count / n
        assert abs(fraction - bucket.weight) <= 0.03


def test_pick_delay_within_chosen_bucket_range() -> None:
    cfg = _cfg()
    rng = random.Random(1)
    ranges = [bucket.range_sec for bucket in cfg.reply_delay_buckets]

    for _ in range(500):
        delay = pick_delay(cfg, rng, urgent=False)
        assert any(lo <= delay <= hi for lo, hi in ranges)


def test_pick_delay_urgent_caps_at_urgent_max_delay_sec() -> None:
    cfg = _cfg()
    rng = random.Random(7)
    lo = cfg.reply_delay_buckets[0].range_sec[0]

    for _ in range(500):
        delay = pick_delay(cfg, rng, urgent=True)
        assert lo <= delay <= cfg.urgent_max_delay_sec


def test_fast_delay_within_first_bucket_range() -> None:
    cfg = _cfg()
    rng = random.Random(3)
    lo, hi = cfg.reply_delay_buckets[0].range_sec

    for _ in range(200):
        delay = fast_delay(cfg, rng)
        assert lo <= delay <= hi


def test_debounce_seconds_within_bounds() -> None:
    cfg = _cfg()
    rng = random.Random(5)
    lo, hi = cfg.debounce_sec

    for _ in range(200):
        value = debounce_seconds(cfg, rng)
        assert lo <= value <= hi
