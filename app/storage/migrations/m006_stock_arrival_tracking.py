"""Migration 006: stock arrival baselines and purchase prompts."""
from __future__ import annotations

from typing import Any

VERSION = 6
NAME = "stock_arrival_tracking"


async def up(conn: Any) -> None:
    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS stock_baselines (
            nm_id INTEGER PRIMARY KEY,
            supplier_article TEXT,
            last_total_full INTEGER NOT NULL,
            last_seen_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pending_purchase_prompts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nm_id INTEGER NOT NULL,
            supplier_article TEXT,
            qty_delta INTEGER NOT NULL,
            baseline_total INTEGER NOT NULL,
            current_total INTEGER NOT NULL,
            detected_at TEXT NOT NULL,
            chat_id INTEGER,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','replied','ignored','cancelled','expired')),
            resolved_at TEXT,
            purchase_id INTEGER,
            note TEXT,
            UNIQUE(nm_id, detected_at)
        );

        CREATE INDEX IF NOT EXISTS idx_ppp_status_detected
            ON pending_purchase_prompts(status, detected_at);

        CREATE INDEX IF NOT EXISTS idx_ppp_nm_status
            ON pending_purchase_prompts(nm_id, status);
        """
    )


async def apply(conn: Any) -> None:
    await up(conn)
