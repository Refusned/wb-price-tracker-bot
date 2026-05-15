from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.storage.db import Database
from app.storage.missed_deal_repository import MissedDealRepository


pytestmark = pytest.mark.asyncio


async def make_repo(tmp_path: Path) -> tuple[Database, MissedDealRepository]:
    db = Database((tmp_path / "app.db").as_posix())
    await db.connect()
    await db.migrate()
    await db.apply_migrations()
    return db, MissedDealRepository(db)


def iso(days_offset: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days_offset)).isoformat()


async def add_price_drop(db: Database, nm_id: int = 100) -> str:
    t0 = iso(-2)
    t1 = iso(-1)
    await db.execute(
        """
        INSERT INTO items (nm_id, name, price_rub, old_price_rub, in_stock, stock_qty, url, updated_at)
        VALUES (?, ?, ?, NULL, 1, 10, ?, ?)
        """,
        (str(nm_id), f"Item {nm_id}", 900.0, f"https://example.com/{nm_id}", t1),
    )
    await db.execute(
        "INSERT INTO price_history (nm_id, price_rub, stock_qty, scanned_at) VALUES (?, ?, ?, ?)",
        (str(nm_id), 1000.0, 10, t0),
    )
    await db.execute(
        "INSERT INTO price_history (nm_id, price_rub, stock_qty, scanned_at) VALUES (?, ?, ?, ?)",
        (str(nm_id), 900.0, 10, t1),
    )
    return t1[:10]


async def test_find_untagged_filters_purchased(tmp_path: Path) -> None:
    db, repo = await make_repo(tmp_path)
    try:
        candidate_date = await add_price_drop(db, nm_id=100)
        await db.execute(
            """
            INSERT INTO purchases (
                date, nm_id, quantity, buy_price_per_unit, total_cost, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (candidate_date, 100, 1, 900.0, 900.0, iso()),
        )

        rows = await repo.find_untagged_candidates(lookback_days=10000)
        assert rows == []
    finally:
        await db.close()


async def test_find_untagged_filters_already_tagged(tmp_path: Path) -> None:
    db, repo = await make_repo(tmp_path)
    try:
        candidate_date = await add_price_drop(db, nm_id=100)
        await repo.tag(100, candidate_date, "cash")

        rows = await repo.find_untagged_candidates(lookback_days=10000)
        assert rows == []
    finally:
        await db.close()


async def test_tag_upsert_idempotent(tmp_path: Path) -> None:
    db, repo = await make_repo(tmp_path)
    try:
        assert await repo.tag(100, "2024-01-01", "cash") is True
        assert await repo.tag(100, "2024-01-01", "cash") is False
        assert await repo.count_tagged() == 1
    finally:
        await db.close()


async def test_distribution_counts_by_reason(tmp_path: Path) -> None:
    db, repo = await make_repo(tmp_path)
    try:
        await repo.tag(100, "2024-01-01", "cash")
        await repo.tag(101, "2024-01-01", "cash")
        await repo.tag(102, "2024-01-01", "too_slow")

        distribution = await repo.distribution()
        assert distribution["cash"] == 2
        assert distribution["too_slow"] == 1
    finally:
        await db.close()
