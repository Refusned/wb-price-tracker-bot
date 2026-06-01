"""Тесты FeedbackResponder. Фейковые WB-клиент и LLM — НИКАКИХ реальных
вызовов и НИКАКОГО реального автопостинга. Проверяем: публикуем валидный
ответ, пишем в журнал, шлём DM; на сбое LLM/пустом ответе НЕ публикуем.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot

from app.llm.client import LLMError
from app.services.feedback_responder import FeedbackResponder
from app.storage.db import Database
from app.storage.feedback_reply_repository import FeedbackReplyRepository
from app.storage.repositories import SubscriberRepository
from app.wb.feedbacks_client import Feedback, FeedbacksApiError, Question

pytestmark = pytest.mark.asyncio


class FakeFeedbacks:
    def __init__(self, feedbacks: list[Feedback] | None = None,
                 questions: list[Question] | None = None) -> None:
        self._feedbacks = feedbacks or []
        self._questions = questions or []
        self.answered_feedbacks: list[tuple[str, str]] = []
        self.answered_questions: list[tuple[str, str]] = []
        self.fail_publish = False

    async def get_unanswered_feedbacks(self, **kw: Any) -> list[Feedback]:
        return list(self._feedbacks)

    async def get_unanswered_questions(self, **kw: Any) -> list[Question]:
        return list(self._questions)

    async def answer_feedback(self, fid: str, text: str) -> None:
        if self.fail_publish:
            raise FeedbacksApiError("publish boom")
        self.answered_feedbacks.append((fid, text))

    async def answer_question(self, qid: str, text: str) -> None:
        if self.fail_publish:
            raise FeedbacksApiError("publish boom")
        self.answered_questions.append((qid, text))


class FakeLLM:
    def __init__(self, reply: str = "Спасибо за ваш отзыв!", raise_error: bool = False) -> None:
        self._reply = reply
        self._raise = raise_error
        self.calls: list[str] = []

    async def generate(self, *, system: str, user: str, **kw: Any) -> str:
        self.calls.append(user)
        if self._raise:
            raise LLMError("llm down")
        return self._reply


class FakeSubs:
    def __init__(self, ids: list[int]) -> None:
        self._ids = ids

    async def list_active_chat_ids(self) -> list[int]:
        return self._ids


async def _build(
    tmp_path: Path,
    *,
    feedbacks: list[Feedback] | None = None,
    questions: list[Question] | None = None,
    llm: FakeLLM | None = None,
    signature: str = "",
    max_per_cycle: int = 10,
) -> tuple[Database, FakeFeedbacks, FeedbackReplyRepository, FeedbackResponder, AsyncMock]:
    db = Database((tmp_path / "app.db").as_posix())
    await db.connect()
    await db.migrate()
    await db.apply_migrations()
    repo = FeedbackReplyRepository(db)
    fb_client = FakeFeedbacks(feedbacks=feedbacks, questions=questions)
    bot = AsyncMock(spec=Bot)
    responder = FeedbackResponder(
        feedbacks_client=cast(Any, fb_client),
        llm_client=cast(Any, llm or FakeLLM()),
        reply_repo=repo,
        subscriber_repository=cast(SubscriberRepository, FakeSubs([123])),
        bot=cast(Bot, bot),
        config=cast(Any, SimpleNamespace(
            feedback_signature=signature, feedback_max_per_cycle=max_per_cycle)),
    )
    return db, fb_client, repo, responder, bot


def _fb(fid: str = "F1", rating: int = 5) -> Feedback:
    return Feedback(id=fid, text="Хорошая колонка", rating=rating, created_date="",
                    nm_id=1, product_name="Станция Миди", user_name="Иван")


async def test_posts_feedback_records_and_dms(tmp_path: Path) -> None:
    db, fb_client, repo, responder, bot = await _build(
        tmp_path, feedbacks=[_fb()], llm=FakeLLM(reply="Спасибо за тёплый отзыв!"))
    try:
        stats = await responder.run_once()
        assert fb_client.answered_feedbacks == [("F1", "Спасибо за тёплый отзыв!")]
        assert stats["posted"] == 1 and stats["failed"] == 0
        assert await repo.is_handled("feedback", "F1") is True
        bot.send_message.assert_awaited_once()
    finally:
        await db.close()


async def test_skips_already_handled(tmp_path: Path) -> None:
    db, fb_client, repo, responder, bot = await _build(tmp_path, feedbacks=[_fb()])
    try:
        await repo.record(kind="feedback", feedback_id="F1", original_text="x",
                          answer_text="y", status="posted")
        await responder.run_once()
        assert fb_client.answered_feedbacks == []  # повторно не отвечаем
        bot.send_message.assert_not_awaited()
    finally:
        await db.close()


async def test_llm_error_does_not_publish(tmp_path: Path) -> None:
    db, fb_client, repo, responder, bot = await _build(
        tmp_path, feedbacks=[_fb()], llm=FakeLLM(raise_error=True))
    try:
        stats = await responder.run_once()
        assert fb_client.answered_feedbacks == []
        assert stats["failed"] == 1 and stats["posted"] == 0
        assert await repo.is_handled("feedback", "F1") is False  # можно повторить
        bot.send_message.assert_not_awaited()
    finally:
        await db.close()


async def test_empty_draft_is_not_published(tmp_path: Path) -> None:
    db, fb_client, repo, responder, bot = await _build(
        tmp_path, feedbacks=[_fb()], llm=FakeLLM(reply="   "))
    try:
        stats = await responder.run_once()
        assert fb_client.answered_feedbacks == []
        assert stats["failed"] == 1
        bot.send_message.assert_not_awaited()
    finally:
        await db.close()


async def test_publish_failure_recorded_not_handled(tmp_path: Path) -> None:
    db, fb_client, repo, responder, bot = await _build(tmp_path, feedbacks=[_fb()])
    try:
        fb_client.fail_publish = True
        stats = await responder.run_once()
        assert stats["failed"] == 1
        assert await repo.is_handled("feedback", "F1") is False
        bot.send_message.assert_not_awaited()
    finally:
        await db.close()


async def test_question_uses_answer_question(tmp_path: Path) -> None:
    q = Question(id="Q1", text="Когда поставка?", created_date="", nm_id=2, product_name="Колонка")
    db, fb_client, repo, responder, bot = await _build(
        tmp_path, questions=[q], llm=FakeLLM(reply="На следующей неделе"))
    try:
        stats = await responder.run_once()
        assert fb_client.answered_questions == [("Q1", "На следующей неделе")]
        assert stats["posted"] == 1
        assert await repo.is_handled("question", "Q1") is True
    finally:
        await db.close()


async def test_signature_appended(tmp_path: Path) -> None:
    db, fb_client, repo, responder, bot = await _build(
        tmp_path, feedbacks=[_fb()], llm=FakeLLM(reply="Благодарим!"),
        signature="С уважением, Магазин")
    try:
        await responder.run_once()
        assert fb_client.answered_feedbacks[0][1].endswith("С уважением, Магазин")
    finally:
        await db.close()


async def test_max_per_cycle_caps_volume(tmp_path: Path) -> None:
    db, fb_client, repo, responder, bot = await _build(
        tmp_path, feedbacks=[_fb("F1"), _fb("F2"), _fb("F3")], max_per_cycle=2)
    try:
        stats = await responder.run_once()
        assert len(fb_client.answered_feedbacks) == 2
        assert stats["posted"] == 2
    finally:
        await db.close()
