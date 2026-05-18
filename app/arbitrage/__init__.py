"""Autonomous WB→WB arbitrage scanner (Day 18+).

See /Users/refusned/.claude/plans/...whimsical-hopcroft.md for full design.

Public API:
    ArbitrageScanner — main orchestrator (called by scheduler loop)
    PersonalSppResolver — buyer-side СПП lookup (Plan B: observations + manual)
    TariffsCache — daily refresh of WB FBS commission and box tariffs
    ArbitrageRepository — CRUD for arb_queries, arb_candidates, arb_buyer_spp_observations
    TariffsRepository — CRUD for arb_tariffs_commission, arb_tariffs_box
    compute_arbitrage_margin — Round 3 calibrated margin formula
    build_alert_message — formats Telegram alert with breakdown

Round 4 paradigm: category-first scanner with manual /arb_observe bootstrap.
"""
from app.arbitrage.margin import compute_arbitrage_margin, MarginBreakdown
from app.arbitrage.repository import ArbitrageRepository
from app.arbitrage.tariffs_repository import TariffsRepository

__all__ = [
    "ArbitrageRepository",
    "MarginBreakdown",
    "TariffsRepository",
    "compute_arbitrage_margin",
]
