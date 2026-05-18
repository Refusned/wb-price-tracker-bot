"""Migration m008: Autonomous arbitrage scanner schema.

Day 18: tables for the WB-to-WB arbitrage scanner submodule.

Five new tables (all prefixed ``arb_*`` to isolate from core schema):
    - arb_queries: user-curated search phrases for the scanner.
    - arb_candidates: per-scan findings with margin breakdown.
    - arb_buyer_spp_observations: my personal buyer-side СПП samples
      (manual /arb_observe + auto on /buy with RRC prompt).
    - arb_tariffs_commission: cached WB FBS commission per subjectID
      with effective_from for versioning (Round 4 D33).
    - arb_tariffs_box: cached FBS logistics rates per warehouse.

See plan: /Users/refusned/.claude/plans/...whimsical-hopcroft.md
"""
from __future__ import annotations

from typing import Any

VERSION = 8
NAME = "arbitrage"


async def up(conn: Any) -> None:
    # ── arb_queries ───────────────────────────────────────────────
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS arb_queries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT UNIQUE NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            subject_id INTEGER,
            subject_name TEXT,
            created_at TEXT NOT NULL,
            last_scanned_at TEXT,
            last_found_count INTEGER DEFAULT 0
        )
        """
    )

    # ── arb_candidates ────────────────────────────────────────────
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS arb_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nm_id INTEGER NOT NULL,
            query TEXT NOT NULL,
            subject_id INTEGER,
            name TEXT,
            brand TEXT,
            market_price_rub INTEGER NOT NULL,
            market_median_rub INTEGER,
            market_p25_rub INTEGER,
            market_min_rub INTEGER,
            buyer_price_rub INTEGER NOT NULL,
            spp_percent_used REAL NOT NULL,
            spp_source TEXT NOT NULL,
            spp_confidence TEXT NOT NULL,
            listed_price_rub INTEGER NOT NULL,
            commission_pct REAL,
            commission_rub INTEGER,
            logistics_rub INTEGER,
            acquiring_rub INTEGER,
            return_reserve_rub INTEGER,
            tax_rub INTEGER,
            holding_rub INTEGER,
            revenue_after_wb_rub INTEGER NOT NULL,
            margin_rub INTEGER NOT NULL,
            margin_percent REAL NOT NULL,
            profit_per_ruble_day_pct REAL NOT NULL,
            expected_hold_days INTEGER NOT NULL,
            cohort_size INTEGER,
            found_at TEXT NOT NULL,
            alerted_at TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            url TEXT
        )
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_arb_cand_nm ON arb_candidates(nm_id)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_arb_cand_alerted "
        "ON arb_candidates(alerted_at DESC) WHERE alerted_at IS NOT NULL"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_arb_cand_found "
        "ON arb_candidates(found_at DESC)"
    )

    # ── arb_buyer_spp_observations (Codex Round 4 CRITICAL #2) ─────
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS arb_buyer_spp_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nm_id INTEGER NOT NULL,
            subject_id INTEGER,
            subject_name TEXT,
            public_price_rub INTEGER NOT NULL,
            my_buyer_price_rub INTEGER NOT NULL,
            spp_percent_observed REAL NOT NULL,
            source TEXT NOT NULL,
            confidence TEXT NOT NULL,
            sample_count INTEGER NOT NULL DEFAULT 1,
            cookie_age_minutes INTEGER,
            observed_at TEXT NOT NULL,
            note TEXT
        )
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_arb_spp_subject "
        "ON arb_buyer_spp_observations(subject_id, observed_at DESC)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_arb_spp_nm "
        "ON arb_buyer_spp_observations(nm_id, observed_at DESC)"
    )

    # ── arb_tariffs_commission (versioned per D33) ────────────────
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS arb_tariffs_commission (
            subject_id INTEGER NOT NULL,
            subject_name TEXT,
            parent_id INTEGER,
            parent_name TEXT,
            kgvp_marketplace REAL,
            kgvp_supplier REAL,
            kgvp_booking REAL,
            kgvp_pickup REAL,
            paid_storage_kgvp REAL,
            effective_from TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (subject_id, effective_from)
        )
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_arb_tariffs_subject "
        "ON arb_tariffs_commission(subject_id, effective_from DESC)"
    )

    # ── arb_tariffs_box (FBS logistics) ───────────────────────────
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS arb_tariffs_box (
            warehouse_name TEXT NOT NULL,
            geo_name TEXT NOT NULL DEFAULT '',
            box_delivery_base REAL,
            box_delivery_liter REAL,
            box_delivery_marketplace_base REAL,
            box_delivery_marketplace_liter REAL,
            box_storage_base REAL,
            box_storage_liter REAL,
            effective_date TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (warehouse_name, geo_name, effective_date)
        )
        """
    )


async def apply(conn: Any) -> None:
    await up(conn)
