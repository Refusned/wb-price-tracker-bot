"""Меню команд Telegram: список валиден по ограничениям Bot API."""
from __future__ import annotations

import re

from app.bot import bot_commands

_NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")


def test_commands_valid_for_telegram() -> None:
    commands = bot_commands()
    assert 1 <= len(commands) <= 100
    names = [c.command for c in commands]
    assert len(names) == len(set(names)), "дубликаты команд"
    for c in commands:
        assert _NAME_RE.match(c.command), f"недопустимое имя: {c.command}"
        assert 1 <= len(c.description) <= 256, f"описание вне лимита: {c.command}"


def test_commands_exist_in_help() -> None:
    """Каждая команда меню документирована в /help (не фантом)."""
    from pathlib import Path

    help_src = (Path(__file__).resolve().parent.parent / "app/handlers/common.py").read_text()
    for c in bot_commands():
        assert f"/{c.command}" in help_src, f"/{c.command} нет в /help"
