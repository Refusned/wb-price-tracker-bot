from __future__ import annotations

from datetime import datetime, timezone

from app.storage.db import Database


VALID_KINDS = {"feedback", "question"}
VALID_STATUSES = {"posted", "failed", "skipped"}


class FeedbackReplyRepository:
    """Журнал автоответов: идемпотентность (что уже опубликовано) + аудит."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def is_handled(self, kind: str, feedback_id: str) -> bool:
        """True, если на этот отзыв/вопрос ответ уже УСПЕШНО опубликован.

        Проверяем только status='posted': провалившиеся (failed) попытки
        можно повторить на следующем цикле.
        """
        self._validate_kind(kind)
        row = await self._db.fetchone(
            "SELECT 1 FROM feedback_replies "
            "WHERE kind = ? AND feedback_id = ? AND status = 'posted'",
            (kind, feedback_id),
        )
        return row is not None

    async def record(
        self,
        *,
        kind: str,
        feedback_id: str,
        original_text: str,
        answer_text: str,
        status: str,
        nm_id: int | None = None,
        product_name: str | None = None,
        rating: int | None = None,
        error: str | None = None,
    ) -> None:
        """Записать результат. INSERT OR REPLACE: failed → может стать posted."""
        self._validate_kind(kind)
        if status not in VALID_STATUSES:
            raise ValueError(f"status must be one of {VALID_STATUSES}")
        if not feedback_id:
            raise ValueError("feedback_id must be non-empty")

        await self._db.execute(
            """
            INSERT OR REPLACE INTO feedback_replies (
                kind, feedback_id, nm_id, product_name, rating,
                original_text, answer_text, status, error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                kind,
                feedback_id,
                nm_id,
                product_name,
                rating,
                original_text,
                answer_text,
                status,
                error,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    async def count_posted(self) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS c FROM feedback_replies WHERE status = 'posted'"
        )
        return int(row["c"] or 0) if row is not None else 0

    @staticmethod
    def _validate_kind(kind: str) -> None:
        if kind not in VALID_KINDS:
            raise ValueError(f"kind must be one of {VALID_KINDS}")
