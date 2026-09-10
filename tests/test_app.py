"""Тесты для trolobot.app. main() не запускается — только setup_logging и ConfigHolder."""

from __future__ import annotations

import logging

from trolobot.app import ConfigHolder, setup_logging
from trolobot.config_models import Config


def test_setup_logging_does_not_raise() -> None:
    setup_logging("DEBUG")
    logging.getLogger("trolobot").info("still works")


def test_setup_logging_accepts_default_level() -> None:
    setup_logging("INFO")


def test_config_holder_get_returns_stored_config() -> None:
    cfg = Config()
    holder = ConfigHolder(cfg)

    assert holder.get() is cfg


def test_config_holder_set_replaces_config() -> None:
    cfg1 = Config()
    cfg2 = Config()
    holder = ConfigHolder(cfg1)

    holder.set(cfg2)

    assert holder.get() is cfg2
