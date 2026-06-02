"""Tests for AccessMiddleware -- the primary deny-by-default auth gate.

Middleware висит на dp.update (outer-middleware), поэтому event — это Update;
отказ неразрешённому юзеру отправляется через event.message / event.callback_query.
Эти тесты пинят И блокировку, И то, что отказ реально уходит (с Telegram ID),
И что гейт привязан к собранному диспетчеру (баг, который ловил прошлый /review).
"""
from __future__ import annotations

import dataclasses
import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TgUser

from app.config import AppConfig, load_config
from app.middlewares import AccessMiddleware

pytestmark = pytest.mark.asyncio


def _cfg(monkeypatch, **overrides) -> AppConfig:
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    cfg = load_config()
    return dataclasses.replace(cfg, **overrides)


def _user(uid: int) -> Any:
    u = MagicMock()
    u.id = uid
    return u


def _update_with_message() -> Any:
    """Update с message; message.answer — AsyncMock (как в реальном поллинге)."""
    upd = MagicMock(spec=Update)
    upd.message = MagicMock()
    upd.message.answer = AsyncMock()
    upd.callback_query = None
    return upd


def _update_with_callback() -> Any:
    upd = MagicMock(spec=Update)
    upd.message = None
    upd.callback_query = MagicMock()
    upd.callback_query.answer = AsyncMock()
    return upd


# ── unit: middleware called directly ─────────────────────────────────

async def test_allowed_user_passes_to_handler(monkeypatch) -> None:
    mw = AccessMiddleware(_cfg(monkeypatch, allowed_user_ids={42}))
    handler = AsyncMock(return_value="handled")
    event = MagicMock(spec=Update)
    result = await mw(handler, event, {"event_from_user": _user(42)})
    handler.assert_awaited_once()
    assert result == "handled"


async def test_disallowed_user_blocked(monkeypatch) -> None:
    mw = AccessMiddleware(_cfg(monkeypatch, allowed_user_ids={42}))
    handler = AsyncMock(return_value="handled")
    event = _update_with_message()
    result = await mw(handler, event, {"event_from_user": _user(7)})
    handler.assert_not_awaited()
    event.message.answer.assert_awaited_once()
    assert result is None


async def test_disallowed_message_includes_telegram_id(monkeypatch) -> None:
    """Regression: отказ должен показывать Telegram ID юзера (onboarding).

    Раньше middleware матчил event на Message/CallbackQuery, но висит на
    dp.update → event это Update, обе ветки были мертвы и отказ не уходил.
    """
    mw = AccessMiddleware(_cfg(monkeypatch, allowed_user_ids={42}))
    event = _update_with_message()
    await mw(AsyncMock(), event, {"event_from_user": _user(7)})
    sent_text = event.message.answer.call_args.args[0]
    assert "7" in sent_text


async def test_empty_whitelist_blocks_everyone(monkeypatch) -> None:
    mw = AccessMiddleware(_cfg(monkeypatch, allowed_user_ids=set()))
    handler = AsyncMock()
    event = _update_with_message()
    await mw(handler, event, {"event_from_user": _user(42)})
    handler.assert_not_awaited()


async def test_missing_user_blocked(monkeypatch) -> None:
    mw = AccessMiddleware(_cfg(monkeypatch, allowed_user_ids={42}))
    handler = AsyncMock()
    event = _update_with_message()
    await mw(handler, event, {"event_from_user": None})
    handler.assert_not_awaited()


async def test_disallowed_callback_uses_alert(monkeypatch) -> None:
    mw = AccessMiddleware(_cfg(monkeypatch, allowed_user_ids={42}))
    handler = AsyncMock()
    event = _update_with_callback()
    await mw(handler, event, {"event_from_user": _user(7)})
    handler.assert_not_awaited()
    event.callback_query.answer.assert_awaited_once()
    assert event.callback_query.answer.call_args.kwargs.get("show_alert") is True


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

    bot = AsyncMock()      # AsyncMock: для disallowed-ветки message.answer() ждёт bot
    bot.id = 1

    await dp.feed_update(bot, _msg_update(42, 1))
    assert seen.get("hit") == 42       # allowed user reaches handler

    seen.clear()
    await dp.feed_update(bot, _msg_update(7, 2))
    assert "hit" not in seen           # disallowed user blocked by middleware
