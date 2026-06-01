"""Migration m011: счётчик попыток для feedback_replies (из /review-аудита).

Зачем:
  - attempts: ограничить ретраи «ядовитых» отзывов/вопросов. Без лимита один
    отзыв, на который LLM/WB стабильно падает, жёг бы токены каждый цикл.
    is_handled останавливает после MAX_ATTEMPTS.
  - status 'pending' (новых столбцов не требует): резерв ПЕРЕД публикацией.
    Строка пишется до PATCH в WB; если упадём между публикацией и записью
    'posted', pending не даст опубликовать второй (публичный, необратимый)
    ответ. Худший исход — недоответ, а не дубль.

Idempotent: ALTER защищён PRAGMA table_info.
"""
from __future__ import annotations

from typing import Any

VERSION = 11
NAME = "feedback_attempts"


async def up(conn: Any) -> None:
    cursor = await conn.execute("PRAGMA table_info(feedback_replies)")
    existing = {row[1] for row in await cursor.fetchall()}
    await cursor.close()
    if "attempts" not in existing:
        await conn.execute(
            "ALTER TABLE feedback_replies ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
        )
