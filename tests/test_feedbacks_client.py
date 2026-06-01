"""Юнит-тесты WBFeedbacksClient. Только фейк-сессия — НИКАКИХ реальных
вызовов WB. Мутирующие методы (answer_*) проверяются на форму запроса, но
бьют в фейк, не в feedbacks-api.wildberries.ru.
"""
from __future__ import annotations

import json as _json
from typing import Any

import pytest

from app.wb.feedbacks_client import (
    FeedbacksApiError,
    Question,
    WBFeedbacksClient,
)

pytestmark = pytest.mark.asyncio


class _Resp:
    def __init__(self, status: int, payload: Any = None, text: str = "") -> None:
        self.status = status
        self._payload = payload
        self._text = text

    async def __aenter__(self) -> "_Resp":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def json(self) -> Any:
        return self._payload

    async def text(self) -> str:
        return self._text or _json.dumps(self._payload)


class _FakeSession:
    def __init__(self) -> None:
        self.get_q: list[_Resp] = []
        self.patch_q: list[_Resp] = []
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, *, params: Any = None, headers: Any = None, timeout: Any = None) -> _Resp:
        self.calls.append({"method": "GET", "url": url, "params": params})
        return self.get_q.pop(0)

    def patch(self, url: str, *, json: Any = None, headers: Any = None, timeout: Any = None) -> _Resp:
        self.calls.append({"method": "PATCH", "url": url, "json": json})
        return self.patch_q.pop(0)


def _client(session: _FakeSession) -> WBFeedbacksClient:
    return WBFeedbacksClient(session, api_key="TOKEN")  # type: ignore[arg-type]


async def test_get_unanswered_feedbacks_parses_and_filters() -> None:
    session = _FakeSession()
    session.get_q.append(_Resp(200, {"data": {"feedbacks": [
        {
            "id": "F1", "text": "Отличная колонка", "pros": "звук", "cons": "",
            "productValuation": 5, "createdDate": "2026-05-30T10:00:00Z",
            "userName": "Иван",
            "productDetails": {"nmId": 123, "productName": "Станция Миди"},
        },
        "garbage-not-a-dict",
    ]}}))
    fb = await _client(session).get_unanswered_feedbacks(take=50)

    assert len(fb) == 1  # мусорный элемент отброшен
    assert fb[0].id == "F1"
    assert fb[0].rating == 5
    assert fb[0].nm_id == 123
    assert fb[0].product_name == "Станция Миди"
    assert fb[0].user_name == "Иван"
    # фильтр неотвеченных уехал в query
    call = session.calls[0]
    assert call["url"].endswith("/api/v1/feedbacks")
    assert call["params"]["isAnswered"] == "false"
    assert call["params"]["take"] == 50


async def test_get_unanswered_questions_parses() -> None:
    session = _FakeSession()
    session.get_q.append(_Resp(200, {"data": {"questions": [
        {"id": "Q1", "text": "Когда поставка?", "createdDate": "2026-05-29T09:00:00Z",
         "productDetails": {"nmId": 777, "productName": "Колонка"}},
    ]}}))
    qs = await _client(session).get_unanswered_questions()

    assert qs == [Question(id="Q1", text="Когда поставка?", created_date="2026-05-29T09:00:00Z",
                           nm_id=777, product_name="Колонка")]
    assert session.calls[0]["url"].endswith("/api/v1/questions")


async def test_answer_feedback_sends_patch_with_id_and_text() -> None:
    session = _FakeSession()
    session.patch_q.append(_Resp(204))  # WB: отзыв -> 204
    await _client(session).answer_feedback("F1", "Спасибо за отзыв!")

    call = session.calls[0]
    assert call["method"] == "PATCH"
    assert call["url"].endswith("/api/v1/feedbacks")
    assert call["json"] == {"id": "F1", "text": "Спасибо за отзыв!"}


async def test_answer_question_sends_patch_with_answer_and_state() -> None:
    session = _FakeSession()
    session.patch_q.append(_Resp(200, {"data": None}))  # WB: вопрос -> 200
    await _client(session).answer_question("Q1", "Поставка на следующей неделе")

    call = session.calls[0]
    assert call["method"] == "PATCH"
    assert call["url"].endswith("/api/v1/questions")
    assert call["json"] == {
        "id": "Q1",
        "answer": {"text": "Поставка на следующей неделе"},
        "state": "wbRu",
    }


async def test_get_raises_on_non_200() -> None:
    session = _FakeSession()
    session.get_q.append(_Resp(401, text="unauthorized"))
    with pytest.raises(FeedbacksApiError):
        await _client(session).get_unanswered_feedbacks()


async def test_answer_raises_on_unexpected_status() -> None:
    session = _FakeSession()
    session.patch_q.append(_Resp(403, text="forbidden"))
    with pytest.raises(FeedbacksApiError):
        await _client(session).answer_feedback("F1", "текст")
