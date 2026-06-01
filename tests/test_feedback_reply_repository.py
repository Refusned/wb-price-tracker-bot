from __future__ import annotations

from pathlib import Path

import pytest

from app.storage.db import Database
from app.storage.feedback_reply_repository import FeedbackReplyRepository

pytestmark = pytest.mark.asyncio


async def _repo(tmp_path: Path) -> tuple[Database, FeedbackReplyRepository]:
    db = Database((tmp_path / "app.db").as_posix())
    await db.connect()
    await db.migrate()
    await db.apply_migrations()
    return db, FeedbackReplyRepository(db)


async def test_record_and_is_handled(tmp_path: Path) -> None:
    db, repo = await _repo(tmp_path)
    try:
        assert await repo.is_handled("feedback", "F1") is False
        await repo.record(
            kind="feedback", feedback_id="F1", original_text="ок",
            answer_text="Спасибо!", status="posted", nm_id=1, product_name="X", rating=5,
        )
        assert await repo.is_handled("feedback", "F1") is True
        assert await repo.count_posted() == 1
    finally:
        await db.close()


async def test_failed_not_handled_then_retry_to_posted(tmp_path: Path) -> None:
    db, repo = await _repo(tmp_path)
    try:
        await repo.record(
            kind="question", feedback_id="Q1", original_text="?",
            answer_text="", status="failed", error="boom",
        )
        # failed → ещё НЕ обработан, повтор разрешён
        assert await repo.is_handled("question", "Q1") is False
        await repo.record(
            kind="question", feedback_id="Q1", original_text="?",
            answer_text="ответ", status="posted",
        )
        # перезаписалось на posted (PK kind+feedback_id)
        assert await repo.is_handled("question", "Q1") is True
        assert await repo.count_posted() == 1
    finally:
        await db.close()


async def test_validation(tmp_path: Path) -> None:
    db, repo = await _repo(tmp_path)
    try:
        with pytest.raises(ValueError):
            await repo.record(kind="bad", feedback_id="X", original_text="o",
                              answer_text="a", status="posted")
        with pytest.raises(ValueError):
            await repo.record(kind="feedback", feedback_id="X", original_text="o",
                              answer_text="a", status="bogus")
    finally:
        await db.close()
