"""Migration 004: personal SPP snapshots."""
from __future__ import annotations

from typing import Any

VERSION = 4
NAME = "personal_spp_snapshots"


async def up(conn: Any) -> None:
    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS personal_spp_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_at TEXT NOT NULL,
            category TEXT DEFAULT 'default',
            spp_percent REAL NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual'
        );

        CREATE INDEX IF NOT EXISTS idx_personal_spp_snapshots_snapshot
            ON personal_spp_snapshots(snapshot_at);
        """
    )
