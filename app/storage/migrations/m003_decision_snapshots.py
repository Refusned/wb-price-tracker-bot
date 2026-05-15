"""Migration 003: decision snapshots."""
from __future__ import annotations

from typing import Any

VERSION = 3
NAME = "decision_snapshots"


async def up(conn: Any) -> None:
    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS decision_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_at TEXT NOT NULL,
            nm_id INTEGER NOT NULL,
            observed_price REAL NOT NULL,
            observed_margin_estimate REAL NOT NULL,
            capital_available_estimate REAL,
            alert_sent INTEGER NOT NULL DEFAULT 0 CHECK (alert_sent IN (0, 1)),
            user_action TEXT,
            source TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_decision_snapshots_nm_snapshot
            ON decision_snapshots(nm_id, snapshot_at);
        """
    )
