"""answer_safe: LLM-текст уходит с Markdown, при битой разметке — plain."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest

from app.handlers.common import answer_safe

pytestmark = pytest.mark.asyncio


def _bad_request() -> TelegramBadRequest:
    return TelegramBadRequest(method=AsyncMock(), message="Bad Request: can't parse entities")


async def test_happy_path_sends_markdown_once() -> None:
    msg = AsyncMock()
    await answer_safe(msg, "Всё *хорошо*")
    msg.answer.assert_awaited_once()
    assert msg.answer.await_args.kwargs["parse_mode"] == "Markdown"


async def test_parse_error_falls_back_to_plain() -> None:
    msg = AsyncMock()
    calls: list[dict[str, Any]] = []

    async def answer(text: str, **kwargs: Any) -> None:
        calls.append(kwargs)
        if kwargs.get("parse_mode") == "Markdown":
            raise _bad_request()

    msg.answer = answer
    await answer_safe(msg, "нечётная * звёздочка")
    assert len(calls) == 2
    assert calls[0].get("parse_mode") == "Markdown"
    assert "parse_mode" not in calls[1]  # fallback — без разметки


async def test_non_parse_errors_propagate() -> None:
    msg = AsyncMock()
    msg.answer.side_effect = RuntimeError("network down")
    with pytest.raises(RuntimeError):
        await answer_safe(msg, "текст")
