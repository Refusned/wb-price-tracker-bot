"""Тесты FeedbackResponder. Фейковые WB-клиент и LLM — НИКАКИХ реальных
вызовов и НИКАКОГО реального автопостинга. Проверяем: публикуем валидный
ответ, пишем в журнал, шлём DM; на сбое LLM/пустом ответе НЕ публикуем.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot

from app.llm.client import LLMError
from app.services.feedback_responder import FeedbackResponder
from app.storage.db import Database
from app.storage.feedback_reply_repository import FeedbackReplyRepository
from app.storage.repositories import SubscriberRepository
from app.wb.feedbacks_client import ANSWER_MAX_LEN, Feedback, FeedbacksApiError, Question

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
    allowed_user_ids: set[int] | None = None,
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
            feedback_signature=signature, feedback_max_per_cycle=max_per_cycle,
            allowed_user_ids=allowed_user_ids or set())),
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


async def test_overlong_draft_truncated_signature_kept(tmp_path: Path) -> None:
    # длинный ответ обрезается ДО подписи: итог ≤ лимита И подпись сохранена
    db, fb_client, repo, responder, bot = await _build(
        tmp_path, feedbacks=[_fb()], llm=FakeLLM(reply="а" * 6000),
        signature="С уважением, Магазин")
    try:
        await responder.run_once()
        published = fb_client.answered_feedbacks[0][1]
        assert len(published) <= ANSWER_MAX_LEN
        assert published.endswith("С уважением, Магазин")
    finally:
        await db.close()


async def test_wrapping_quotes_stripped(tmp_path: Path) -> None:
    db, fb_client, repo, responder, bot = await _build(
        tmp_path, feedbacks=[_fb()], llm=FakeLLM(reply="«Спасибо за отзыв»"))
    try:
        await responder.run_once()
        assert fb_client.answered_feedbacks[0][1] == "Спасибо за отзыв"
    finally:
        await db.close()


async def test_inner_quotes_not_corrupted(tmp_path: Path) -> None:
    # внутри есть », крайние кавычки НЕ снимаем — текст не должен повредиться
    db, fb_client, repo, responder, bot = await _build(
        tmp_path, feedbacks=[_fb()], llm=FakeLLM(reply="«А» и «Б»"))
    try:
        await responder.run_once()
        assert fb_client.answered_feedbacks[0][1] == "«А» и «Б»"
    finally:
        await db.close()


async def test_too_short_draft_rejected(tmp_path: Path) -> None:
    db, fb_client, repo, responder, bot = await _build(
        tmp_path, feedbacks=[_fb()], llm=FakeLLM(reply="X"))
    try:
        stats = await responder.run_once()
        assert fb_client.answered_feedbacks == []
        assert stats["failed"] == 1
        assert await repo.is_handled("feedback", "F1") is False
    finally:
        await db.close()


# ── #1 контент-гейт: ответ с контактами НЕ публикуется ──────────────

async def test_reply_with_url_not_published(tmp_path: Path) -> None:
    db, fb_client, repo, responder, bot = await _build(
        tmp_path, feedbacks=[_fb()],
        llm=FakeLLM(reply="Спасибо! Подробнее на http://shop.example"))
    try:
        stats = await responder.run_once()
        assert fb_client.answered_feedbacks == []  # гейт не пустил ссылку
        assert stats["failed"] == 1
        assert await repo.is_handled("feedback", "F1") is False
    finally:
        await db.close()


async def test_reply_with_phone_not_published(tmp_path: Path) -> None:
    db, fb_client, repo, responder, bot = await _build(
        tmp_path, feedbacks=[_fb()],
        llm=FakeLLM(reply="Звоните: +7 900 123 45 67, поможем"))
    try:
        stats = await responder.run_once()
        assert fb_client.answered_feedbacks == []  # телефон не публикуем
        assert stats["failed"] == 1
    finally:
        await db.close()


# ── #4 не перебиваем уже отвеченные ─────────────────────────────────

async def test_already_answered_feedback_skipped(tmp_path: Path) -> None:
    fb = Feedback(id="F9", text="ок", rating=5, created_date="", nm_id=1,
                  product_name="X", user_name="", answered=True)
    db, fb_client, repo, responder, bot = await _build(tmp_path, feedbacks=[fb])
    try:
        stats = await responder.run_once()
        assert fb_client.answered_feedbacks == []  # уже отвечено — не трогаем
        assert stats["posted"] == 0 and stats["failed"] == 0
        bot.send_message.assert_not_awaited()
    finally:
        await db.close()


# ── #5 DM об автоответе — только владельцу (allowed_user_ids) ────────

async def test_owner_dm_goes_to_allowed_users_only(tmp_path: Path) -> None:
    db, fb_client, repo, responder, bot = await _build(
        tmp_path, feedbacks=[_fb()], llm=FakeLLM(reply="Спасибо!"),
        allowed_user_ids={999})
    try:
        await responder.run_once()
        bot.send_message.assert_awaited_once()
        assert bot.send_message.await_args.kwargs["chat_id"] == 999
    finally:
        await db.close()


# ── #6 kill-switch: цикл автоответов НЕ стартует при выключенном флаге ─

async def test_feedback_loop_gated_by_flag() -> None:
    from app.scheduler import WbUpdateScheduler

    def _mk(flag: bool, responder: Any) -> WbUpdateScheduler:
        return WbUpdateScheduler(
            config=cast(Any, SimpleNamespace(feedback_auto_reply_enabled=flag)),
            wb_client=cast(Any, MagicMock()),
            bot=cast(Any, AsyncMock()),
            item_repository=cast(Any, MagicMock()),
            meta_repository=cast(Any, MagicMock()),
            settings_repository=cast(Any, MagicMock()),
            subscriber_repository=cast(Any, MagicMock()),
            price_stats_repository=cast(Any, MagicMock()),
            price_history_repository=cast(Any, MagicMock()),
            tracked_article_repository=cast(Any, MagicMock()),
            feedback_responder=responder,
        )

    assert _mk(True, object())._feedback_loop_enabled() is True   # respondер + флаг
    assert _mk(False, object())._feedback_loop_enabled() is False  # флаг OFF
    assert _mk(True, None)._feedback_loop_enabled() is False       # нет респондера
