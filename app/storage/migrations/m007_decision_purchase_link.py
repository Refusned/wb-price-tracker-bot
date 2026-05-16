"""Migration m007: link decision_snapshots to purchases.

Day 16: add purchase_id FK column to decision_snapshots. When a purchase
is created (manual /buy, /addpurchase, or auto via stock arrival prompt),
the bot will look up recent matching decision_snapshot and link it.

This is the data backbone for Day 22 counterfactual:
- "Bot fired N alerts → owner bought M of them → K closed with profit"
- Without this link, we only have temporal correlation (weak).
"""
from __future__ import annotations

from typing import Any

VERSION = 7
NAME = "decision_purchase_link"


async def up(conn: Any) -> None:
    # ALTER TABLE ADD COLUMN is not idempotent under SQLite, so check first.
    cursor = await conn.execute("PRAGMA table_info(decision_snapshots)")
    columns = await cursor.fetchall()
    await cursor.close()
    column_names = {row[1] for row in columns}

    if "purchase_id" not in column_names:
        await conn.execute(
            "ALTER TABLE decision_snapshots ADD COLUMN purchase_id INTEGER"
        )

    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_decision_snapshots_purchase "
        "ON decision_snapshots(purchase_id) WHERE purchase_id IS NOT NULL"
    )


async def apply(conn: Any) -> None:
    await up(conn)
