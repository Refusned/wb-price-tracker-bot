"""Tests for AccessMiddleware -- the primary deny-by-default auth gate.

is_user_allowed is unit-tested in test_security.py, but the middleware wiring
(extract event_from_user, block disallowed, pass allowed) had no test. A
regression that registers it inner, drops callback_query, or inverts the check
would pass every other test. This pins the gate behaviour.
"""
from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message

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
    handler.assert_not_awaited()          # handler never reached
    event.answer.assert_awaited_once()    # deny message sent
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
    # callback denial must use show_alert=True
    assert event.answer.call_args.kwargs.get("show_alert") is True
