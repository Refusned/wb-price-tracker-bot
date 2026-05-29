"""Migration m009: Этап 1 — per-query keyword filter + ground-truth labels.

Day 19 (deterministic baseline, pre-LLM):
    - arb_queries.include_keywords / exclude_keywords (CSV, per-query, nullable).
      Wires color/variant filtering into the arbitrage path WITHOUT an LLM.
      Per-query (not global) so a Станция query can filter by colour while a
      robot-vacuum query stays unfiltered.
    - arb_nm_labels: owner ground-truth labels (wrong_color, wrong_product,
      bought, no_cash, bad_margin) so cohort-filter precision can be measured
      honestly before any LLM is considered (Этап 2).

Idempotent: ALTER guarded by PRAGMA table_info; tables use IF NOT EXISTS.
"""
from __future__ import annotations

from typing import Any

VERSION = 9
NAME = "arb_keywords_labels"


async def up(conn: Any) -> None:
    # ── arb_queries: per-query keyword filter columns ─────────────
    cursor = await conn.execute("PRAGMA table_info(arb_queries)")
    existing = {row[1] for row in await cursor.fetchall()}
    await cursor.close()
    if "include_keywords" not in existing:
        await conn.execute("ALTER TABLE arb_queries ADD COLUMN include_keywords TEXT")
    if "exclude_keywords" not in existing:
        await conn.execute("ALTER TABLE arb_queries ADD COLUMN exclude_keywords TEXT")

    # ── arb_nm_labels: owner ground-truth labels ──────────────────
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS arb_nm_labels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nm_id INTEGER NOT NULL,
            label TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_arb_nm_labels_nm ON arb_nm_labels(nm_id)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_arb_nm_labels_label ON arb_nm_labels(label)"
    )


async def apply(conn: Any) -> None:
    await up(conn)
