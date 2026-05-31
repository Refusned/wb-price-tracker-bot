"""Денежная безопасность хранилища: атомарный INSERT и ретеншен."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.arbitrage.repository import ArbitrageRepository
from app.storage.business_repository import BusinessRepository
from app.storage.db import Database

pytestmark = pytest.mark.asyncio


async def _db(tmp_path: Path) -> Database:
    db = Database((tmp_path / "app.db").as_posix())
    await db.connect()
    await db.migrate()
    await db.apply_migrations()
    return db


async def test_add_purchase_concurrent_ids_unique(tmp_path: Path) -> None:
    """execute_insert atomic: конкурентные вставки дают уникальные id.

    Раньше add_purchase делал INSERT + отдельный SELECT last_insert_rowid(),
    и между ними конкурентная вставка могла подменить rowid.
    """
    db = await _db(tmp_path)
    repo = BusinessRepository(db)
    ids = await asyncio.gather(*[
        repo.add_purchase(
            nm_id=1000 + i, supplier_article=None, quantity=1,
            buy_price_per_unit=100.0 + i, spp_at_purchase=None, notes=None,
        )
        for i in range(25)
    ])
    assert len(set(ids)) == 25  # все уникальны — нет подмены rowid
    assert all(i > 0 for i in ids)
    await db.close()


def _cand(nm_id: int) -> dict:
    return dict(
        nm_id=nm_id, query="q", subject_id=1, name="n", brand="b",
        market_price_rub=10000, market_median_rub=10000, market_p25_rub=9000,
        market_min_rub=8000, buyer_price_rub=9400, spp_percent_used=24.0,
        spp_source="category_avg", spp_confidence="medium", listed_price_rub=13000,
        commission_pct=16.0, commission_rub=2000, logistics_rub=500, acquiring_rub=0,
        return_reserve_rub=200, tax_rub=200, holding_rub=70, revenue_after_wb_rub=11000,
        margin_rub=900, margin_percent=9.5, profit_per_ruble_day_pct=0.6,
        expected_hold_days=14, cohort_size=10, url="u",
    )


async def test_cleanup_candidates_removes_old_keeps_recent(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    repo = ArbitrageRepository(db)
    old_id = await repo.record_candidate(**_cand(101))
    await repo.record_candidate(**_cand(102))  # recent
    old_dt = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    await db.execute("UPDATE arb_candidates SET found_at = ? WHERE id = ?", (old_dt, old_id))

    assert await repo.cleanup_candidates(retention_days=90) == 1
    assert await repo.count_candidates() == 1
    await db.close()


async def test_cleanup_candidates_zero_retention_is_noop(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    repo = ArbitrageRepository(db)
    await repo.record_candidate(**_cand(101))
    assert await repo.cleanup_candidates(retention_days=0) == 0
    assert await repo.count_candidates() == 1
    await db.close()


async def test_cleanup_observations_removes_old(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    repo = ArbitrageRepository(db)
    obs_id = await repo.record_spp_observation(
        nm_id=555, subject_id=1, subject_name="cat",
        public_price_rub=15000, my_buyer_price_rub=11000,
        source="checkout_manual", confidence="high",
    )
    await repo.record_spp_observation(
        nm_id=556, subject_id=1, subject_name="cat",
        public_price_rub=15000, my_buyer_price_rub=11000,
        source="checkout_manual", confidence="high",
    )
    old_dt = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    await db.execute(
        "UPDATE arb_buyer_spp_observations SET observed_at = ? WHERE id = ?", (old_dt, obs_id),
    )
    assert await repo.cleanup_observations(retention_days=180) == 1
    await db.close()
