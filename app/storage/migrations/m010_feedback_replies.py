"""Migration m010: журнал автоответов на отзывы/вопросы WB (Фаза 1).

feedback_replies решает две задачи:
    - ИДЕМПОТЕНТНОСТЬ: один отзыв/вопрос = один ответ. PK (kind, feedback_id)
      не даёт ответить второй раз даже в окне, пока WB ещё не обновил
      isAnswered=true после нашей публикации.
    - АУДИТ: что именно LLM опубликовала, когда, какой был оригинал/оценка.

Idempotent: CREATE TABLE/INDEX IF NOT EXISTS.
"""
from __future__ import annotations

from typing import Any

VERSION = 10
NAME = "feedback_replies"


async def up(conn: Any) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback_replies (
            kind TEXT NOT NULL,             -- 'feedback' | 'question'
            feedback_id TEXT NOT NULL,      -- WB id отзыва/вопроса
            nm_id INTEGER,
            product_name TEXT,
            rating INTEGER,                 -- оценка отзыва 1..5; NULL для вопросов
            original_text TEXT NOT NULL,
            answer_text TEXT NOT NULL,
            status TEXT NOT NULL,           -- 'posted' | 'failed' | 'skipped'
            error TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (kind, feedback_id)
        )
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedback_replies_created ON feedback_replies(created_at)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedback_replies_status ON feedback_replies(status)"
    )
