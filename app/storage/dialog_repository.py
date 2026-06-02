"""DialogRepository — история интерактивного диалога LLM-агента (Фаза 3).

Одна логическая сессия на chat_id. Хранит только текстовые ходы (user/assistant),
переживает рестарт процесса (в отличие от FSM в MemoryStorage). Схема — m012.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.storage.db import Database

_VALID_ROLES = {"user", "assistant"}
# Жёсткий потолок контекста: сколько последних ходов отдавать модели. Контролирует
# и стоимость токенов, и длительность tool-loop.
DEFAULT_HISTORY_LIMIT = 16


class DialogRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def append(self, chat_id: int, role: str, content: str) -> int:
        if role not in _VALID_ROLES:
            raise ValueError(f"role must be one of {_VALID_ROLES}")
        return await self._db.execute_insert(
            "INSERT INTO agent_dialog (chat_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, role, content, datetime.now(timezone.utc).isoformat()),
        )

    async def get_recent(self, chat_id: int, limit: int = DEFAULT_HISTORY_LIMIT) -> list[dict]:
        """Последние `limit` ходов в ХРОНОЛОГИЧЕСКОМ порядке (oldest→newest),
        готовые к мапу в Ollama messages[]. Берём свежий хвост (id DESC LIMIT) и
        разворачиваем — обрезается старая середина, а не актуальный конец диалога.
        """
        rows = await self._db.fetchall(
            "SELECT role, content FROM agent_dialog WHERE chat_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
        out = [{"role": r["role"], "content": r["content"]} for r in rows]
        out.reverse()
        return out

    async def clear(self, chat_id: int) -> None:
        await self._db.execute("DELETE FROM agent_dialog WHERE chat_id = ?", (chat_id,))

    async def count(self, chat_id: int) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS c FROM agent_dialog WHERE chat_id = ?", (chat_id,)
        )
        return int(row["c"] or 0) if row is not None else 0
