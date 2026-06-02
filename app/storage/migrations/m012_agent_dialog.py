"""Migration m012: история интерактивного диалога LLM-агента по кабинету (Фаза 3).

agent_dialog — журнал ходов диалога (режим «🤖 Ассистент») для восстановления
контекста между сообщениями и после рестарта процесса (FSM в MemoryStorage
теряется, история — нет). Храним только ТЕКСТОВЫЕ ходы:
    role='user'      — запрос владельца,
    role='assistant' — финальный ответ агента.
Промежуточные tool-вызовы внутри одного хода живут в RAM и в БД НЕ пишутся:
контекст между ходами — это диалог, а не трасса инструментов.

Порядок ходов — по автоинкрементному id (монотонен). Idempotent: IF NOT EXISTS.
"""
from __future__ import annotations

from typing import Any

VERSION = 12
NAME = "agent_dialog"


async def up(conn: Any) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_dialog (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            role TEXT NOT NULL,          -- 'user' | 'assistant'
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_dialog_chat ON agent_dialog(chat_id, id)"
    )
