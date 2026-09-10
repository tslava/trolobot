"""Тесты для trolobot.app. main() не запускается — только setup_logging.

Полная сборка процесса (Settings -> ConfigStore -> PromptStore -> Bot -> Deps ->
Dispatcher -> Responder) проверяется сквозным тестом в
tests/test_commands_integration.py, не здесь: main() требует реального aiogram
Bot.get_me() (сетевой вызов), поэтому напрямую не вызывается ни в одном тесте.
"""

from __future__ import annotations

import logging

from trolobot.app import setup_logging


def test_setup_logging_does_not_raise() -> None:
    setup_logging("DEBUG")
    logging.getLogger("trolobot").info("still works")


def test_setup_logging_accepts_default_level() -> None:
    setup_logging("INFO")
