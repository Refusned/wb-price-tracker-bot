from __future__ import annotations

from datetime import datetime, timezone

from app.storage.db import Database


VALID_KINDS = {"feedback", "question"}
VALID_STATUSES = {"posted", "pending", "failed", "skipped"}
# После стольких попыток перестаём ретраить (защита от «ядовитых» отзывов,
# на которых LLM/WB стабильно падает и жгут токены каждый цикл).
MAX_ATTEMPTS = 3


class FeedbackReplyRepository:
    """Журнал автоответов: идемпотентность (резерв перед публикацией + лимит
    попыток) + аудит."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def is_handled(self, kind: str, feedback_id: str) -> bool:
        """True, если отвечать больше НЕ нужно:
          - ответ уже опубликован ('posted') ИЛИ резервируется ('pending'), либо
          - исчерпан лимит попыток (attempts >= MAX_ATTEMPTS).

        'pending' блокирует повтор в окне между PATCH в WB и записью 'posted' —
        чтобы публичный необратимый ответ не ушёл покупателю дважды.
        """
        self._validate_kind(kind)
        row = await self._db.fetchone(
            "SELECT 1 FROM feedback_replies "
            "WHERE kind = ? AND feedback_id = ? "
            "AND (status IN ('posted', 'pending') OR attempts >= ?)",
            (kind, feedback_id, MAX_ATTEMPTS),
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
        increment: bool = True,
    ) -> None:
        """UPSERT по (kind, feedback_id).

        increment=True — это новая попытка (attempts += 1). increment=False —
        только смена статуса без новой попытки (например pending → posted, где
        попытку уже посчитал резерв).
        """
        self._validate_kind(kind)
        if status not in VALID_STATUSES:
            raise ValueError(f"status must be one of {VALID_STATUSES}")
        if not feedback_id:
            raise ValueError("feedback_id must be non-empty")
        delta = 1 if increment else 0
        await self._db.execute(
            """
            INSERT INTO feedback_replies (
                kind, feedback_id, nm_id, product_name, rating,
                original_text, answer_text, status, error, attempts, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(kind, feedback_id) DO UPDATE SET
                nm_id = excluded.nm_id,
                product_name = excluded.product_name,
                rating = excluded.rating,
                original_text = excluded.original_text,
                answer_text = excluded.answer_text,
                status = excluded.status,
                error = excluded.error,
                attempts = feedback_replies.attempts + ?,
                created_at = excluded.created_at
            """,
            (
                kind, feedback_id, nm_id, product_name, rating,
                original_text, answer_text, status, error, delta,
                datetime.now(timezone.utc).isoformat(), delta,
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
