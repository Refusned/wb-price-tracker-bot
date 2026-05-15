"""Migration 005: manual missed-deal tags."""
from __future__ import annotations

from typing import Any

VERSION = 5
NAME = "missed_deal_tags"


async def up(conn: Any) -> None:
    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS missed_deal_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nm_id INTEGER NOT NULL,
            candidate_date TEXT NOT NULL,
            observed_price REAL,
            observed_margin_estimate REAL,
            reason TEXT NOT NULL CHECK (
                reason IN ('cash', 'too_slow', 'bad_margin', 'not_interested', 'other')
            ),
            note TEXT,
            tagged_at TEXT NOT NULL,
            UNIQUE(nm_id, candidate_date)
        );

        CREATE INDEX IF NOT EXISTS idx_missed_deal_tags_reason_tagged_at
            ON missed_deal_tags(reason, tagged_at);

        CREATE INDEX IF NOT EXISTS idx_missed_deal_tags_candidate_date
            ON missed_deal_tags(candidate_date);
        """
    )
