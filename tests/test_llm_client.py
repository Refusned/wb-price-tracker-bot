"""Юнит-тесты LLMClient. Никаких реальных сетевых вызовов — только фейк-сессия.

Money/safety: тест НИКОГДА не ходит в Ollama Cloud и не тратит токены.
"""
from __future__ import annotations

import json as _json
from typing import Any

import pytest

from app.llm.client import LLMClient, LLMError

pytestmark = pytest.mark.asyncio


class _FakeResponse:
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def json(self) -> Any:
        return self._payload

    async def text(self) -> str:
        return _json.dumps(self._payload)


class _FakeSession:
    """Отдаёт ответы из очереди; запоминает каждый вызов post()."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, *, json: Any = None, headers: Any = None, timeout: Any = None) -> _FakeResponse:
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self._responses.pop(0)


def _ok(content: str) -> _FakeResponse:
    return _FakeResponse(200, {"message": {"role": "assistant", "content": content}})


async def test_generate_returns_content_and_posts_correctly() -> None:
    session = _FakeSession([_ok("  Спасибо за отзыв!  ")])
    client = LLMClient(session, api_key="KEY", model="deepseek-v4-pro", backoff_seconds=0)  # type: ignore[arg-type]

    out = await client.generate(system="sys", user="usr", temperature=0.2, num_predict=200)

    assert out == "Спасибо за отзыв!"  # stripped
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == "https://ollama.com/api/chat"
    assert call["headers"]["Authorization"] == "Bearer KEY"
    body = call["json"]
    assert body["model"] == "deepseek-v4-pro"
    assert body["stream"] is False
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][1] == {"role": "user", "content": "usr"}
    assert body["options"]["temperature"] == 0.2
    assert body["options"]["num_predict"] == 200


async def test_generate_retries_then_succeeds() -> None:
    session = _FakeSession([_FakeResponse(500, {"error": "boom"}), _ok("ok")])
    client = LLMClient(session, api_key="K", retries=2, backoff_seconds=0)  # type: ignore[arg-type]

    out = await client.generate(system="s", user="u")

    assert out == "ok"
    assert len(session.calls) == 2


async def test_generate_raises_after_exhaustion() -> None:
    session = _FakeSession([_FakeResponse(500, {}), _FakeResponse(503, {})])
    client = LLMClient(session, api_key="K", retries=2, backoff_seconds=0)  # type: ignore[arg-type]

    with pytest.raises(LLMError):
        await client.generate(system="s", user="u")
    assert len(session.calls) == 2


async def test_generate_raises_on_empty_content() -> None:
    session = _FakeSession([_ok("   "), _ok("")])
    client = LLMClient(session, api_key="K", retries=2, backoff_seconds=0)  # type: ignore[arg-type]

    with pytest.raises(LLMError):
        await client.generate(system="s", user="u")
    assert len(session.calls) == 2
