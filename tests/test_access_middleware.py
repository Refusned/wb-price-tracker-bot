"""Tests for AccessMiddleware -- the primary deny-by-default auth gate.

is_user_allowed is unit-tested in test_security.py, but the middleware wiring
(extract event_from_user, block disallowed, pass allowed) had no test. A
regression that registers it inner, drops callback_query, or inverts the check
would pass every other test. This pins the gate behaviour AND that it is
actually attached to the built dispatcher (the bug a prior /review caught).
"""
from __future__ import annotations

import dataclasses
import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser

from app.config import AppConfig, load_config
from app.middlewares import AccessMiddleware

pytestmark = pytest.mark.asyncio


def _cfg(monkeypatch, **overrides) -> AppConfig:
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    cfg = load_config()
    return dataclasses.replace(cfg, **overrides)


def _user(uid):
    u = MagicMock()
    u.id = uid
    return u


# ── unit: middleware called directly ─────────────────────────────────

async def test_allowed_user_passes_to_handler(monkeypatch) -> None:
    mw = AccessMiddleware(_cfg(monkeypatch, allowed_user_ids={42}))
    handler = AsyncMock(return_value="handled")
    event = MagicMock(spec=Message)
    result = await mw(handler, event, {"event_from_user": _user(42)})
    handler.assert_awaited_once()
    assert result == "handled"


async def test_disallowed_user_blocked(monkeypatch) -> None:
    mw = AccessMiddleware(_cfg(monkeypatch, allowed_user_ids={42}))
    handler = AsyncMock(return_value="handled")
    event = MagicMock(spec=Message)
    event.answer = AsyncMock()
    result = await mw(handler, event, {"event_from_user": _user(7)})
    handler.assert_not_awaited()
    event.answer.assert_awaited_once()
    assert result is None


async def test_empty_whitelist_blocks_everyone(monkeypatch) -> None:
    mw = AccessMiddleware(_cfg(monkeypatch, allowed_user_ids=set()))
    handler = AsyncMock()
    event = MagicMock(spec=Message)
    event.answer = AsyncMock()
    await mw(handler, event, {"event_from_user": _user(42)})
    handler.assert_not_awaited()


async def test_missing_user_blocked(monkeypatch) -> None:
    mw = AccessMiddleware(_cfg(monkeypatch, allowed_user_ids={42}))
    handler = AsyncMock()
    event = MagicMock(spec=Message)
    event.answer = AsyncMock()
    await mw(handler, event, {"event_from_user": None})
    handler.assert_not_awaited()


async def test_disallowed_callback_uses_alert(monkeypatch) -> None:
    mw = AccessMiddleware(_cfg(monkeypatch, allowed_user_ids={42}))
    handler = AsyncMock()
    event = MagicMock(spec=CallbackQuery)
    event.answer = AsyncMock()
    await mw(handler, event, {"event_from_user": _user(7)})
    handler.assert_not_awaited()
    event.answer.assert_awaited_once()
    assert event.answer.call_args.kwargs.get("show_alert") is True


# ── integration: gate actually wired into a real dispatcher ──────────
# This is the test that would have caught "middleware defined but never
# registered". It feeds real Update objects through aiogram's full chain
# (incl. UserContextMiddleware that populates event_from_user) and asserts
# the allowed user reaches the handler while the disallowed user does not.

def _msg_update(uid: int, upid: int) -> Update:
    return Update(
        update_id=upid,
        message=Message(
            message_id=upid,
            date=datetime.datetime.now(),
            chat=Chat(id=1, type="private"),
            from_user=TgUser(id=uid, is_bot=False, first_name="X"),
            text="/x",
        ),
    )


async def test_dispatcher_gate_blocks_disallowed_end_to_end(monkeypatch) -> None:
    cfg = _cfg(monkeypatch, allowed_user_ids={42})
    dp = Dispatcher()
    dp.update.outer_middleware(AccessMiddleware(cfg))
    seen: dict[str, int] = {}

    @dp.message()
    async def _h(m: Message) -> None:
        seen["hit"] = m.from_user.id

    bot = MagicMock()
    bot.id = 1

    await dp.feed_update(bot, _msg_update(42, 1))
    assert seen.get("hit") == 42       # allowed user reaches handler

    seen.clear()
    await dp.feed_update(bot, _msg_update(7, 2))
    assert "hit" not in seen           # disallowed user blocked by middleware
