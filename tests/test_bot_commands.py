"""Меню команд и профиль бота: валидны по ограничениям Bot API."""
from __future__ import annotations

import re

from app.bot import BOT_ABOUT, BOT_DESCRIPTION, bot_commands

_NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")


def test_commands_valid_for_telegram() -> None:
    commands = bot_commands()
    assert 1 <= len(commands) <= 100
    names = [c.command for c in commands]
    assert len(names) == len(set(names)), "дубликаты команд"
    for c in commands:
        assert _NAME_RE.match(c.command), f"недопустимое имя: {c.command}"
        assert 1 <= len(c.description) <= 256, f"описание вне лимита: {c.command}"


def test_profile_texts_within_api_limits() -> None:
    assert 1 <= len(BOT_ABOUT) <= 120       # setMyShortDescription
    assert 1 <= len(BOT_DESCRIPTION) <= 512  # setMyDescription


def test_commands_have_registered_handlers() -> None:
    """Каждая команда меню реально зарегистрирована хендлером (не фантом).

    Ловит класс бага «в меню есть, обработчика нет» (как были /margin и
    /setmin в подменю). /start регистрируется через CommandStart().
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    sources = "\n".join(
        p.read_text(encoding="utf-8")
        for p in [*(root / "app/handlers").glob("*.py"), root / "app/arbitrage/handlers.py"]
    )
    registered = set(re.findall(r'Command\("([a-z0-9_]+)"\)', sources))
    if "CommandStart()" in sources:
        registered.add("start")
    for c in bot_commands():
        assert c.command in registered, f"/{c.command} в меню, но хендлер не зарегистрирован"
