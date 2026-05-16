"""Day 16: decision_snapshots ↔ purchases linking tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.storage.business_repository import BusinessRepository
from app.storage.db import Database
from app.storage.decision_snapshot_repository import DecisionSnapshotRepository


pytestmark = pytest.mark.asyncio


async def _make_db(tmp_path: Path) -> Database:
    db = Database((tmp_path / "app.db").as_posix())
    await db.connect()
    await db.migrate()
    await db.apply_migrations()
    return db


async def test_m007_adds_purchase_id_column(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    cursor = await db._require_conn().execute("PRAGMA table_info(decision_snapshots)")
    cols = {row[1] for row in await cursor.fetchall()}
    await cursor.close()
    assert "purchase_id" in cols
    await db.close()


async def test_m007_runs_idempotent(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    await db.apply_migrations()
    await db.apply_migrations()
    cursor = await db._require_conn().execute("PRAGMA table_info(decision_snapshots)")
    cols = [row[1] for row in await cursor.fetchall()]
    await cursor.close()
    assert cols.count("purchase_id") == 1
    await db.close()


async def test_link_to_purchase_sets_both_fields(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    repo = DecisionSnapshotRepository(db)
    sid = await repo.record(
        nm_id=12345, observed_price=9500.0, observed_margin_estimate=18.0,
        alert_sent=True, source="test",
    )
    await repo.link_to_purchase(snapshot_id=sid, purchase_id=42, action="bought")
    row = (await repo.recent(limit=1))[0]
    assert row["user_action"] == "bought"
    assert row["purchase_id"] == 42
    await db.close()


async def test_link_validates_action(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    repo = DecisionSnapshotRepository(db)
    sid = await repo.record(
        nm_id=1, observed_price=1.0, observed_margin_estimate=1.0,
        alert_sent=False, source="test",
    )
    with pytest.raises(ValueError):
        await repo.link_to_purchase(sid, 1, action="invalid")
    await db.close()


async def test_find_most_recent_unlinked_returns_newest(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    repo = DecisionSnapshotRepository(db)
    now = datetime.now(timezone.utc)
    s1 = await repo.record(
        nm_id=999, observed_price=100.0, observed_margin_estimate=10.0,
        alert_sent=True, source="t", snapshot_at=(now - timedelta(hours=10)).isoformat(),
    )
    s2 = await repo.record(
        nm_id=999, observed_price=110.0, observed_margin_estimate=12.0,
        alert_sent=True, source="t", snapshot_at=(now - timedelta(hours=2)).isoformat(),
    )
    found = await repo.find_most_recent_unlinked(nm_id=999, within_seconds=86400)
    assert found is not None
    assert found["id"] == s2  # newest
    await db.close()


async def test_find_excludes_already_linked(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    repo = DecisionSnapshotRepository(db)
    s1 = await repo.record(
        nm_id=777, observed_price=100.0, observed_margin_estimate=10.0,
        alert_sent=True, source="t",
    )
    await repo.link_to_purchase(s1, 1, action="bought")
    found = await repo.find_most_recent_unlinked(nm_id=777, within_seconds=86400)
    assert found is None  # already linked → excluded
    await db.close()


async def test_find_excludes_outside_window(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    repo = DecisionSnapshotRepository(db)
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    await repo.record(
        nm_id=555, observed_price=1.0, observed_margin_estimate=1.0,
        alert_sent=True, source="t", snapshot_at=old_ts,
    )
    found = await repo.find_most_recent_unlinked(nm_id=555, within_seconds=86400)  # 24h
    assert found is None
    await db.close()


async def test_end_to_end_purchase_links_snapshot(tmp_path: Path) -> None:
    """Simulates the real flow: snapshot fires → user buys → linking succeeds."""
    db = await _make_db(tmp_path)
    decision_repo = DecisionSnapshotRepository(db)
    business_repo = BusinessRepository(db)

    # 1. Bot alerts on price drop (records snapshot)
    snap_id = await decision_repo.record(
        nm_id=193961961, observed_price=9500.0, observed_margin_estimate=18.0,
        alert_sent=True, source="scheduler.price_drop",
    )

    # 2. Owner buys via /buy
    purchase_id = await business_repo.add_purchase(
        nm_id=193961961, supplier_article=None,
        quantity=10, buy_price_per_unit=9500.0,
        spp_at_purchase=24.0, notes=None,
    )

    # 3. Linking logic
    found = await decision_repo.find_most_recent_unlinked(nm_id=193961961)
    assert found is not None
    assert found["id"] == snap_id
    await decision_repo.link_to_purchase(found["id"], purchase_id, action="bought")

    # Verify
    snap = (await decision_repo.recent(limit=1))[0]
    assert snap["purchase_id"] == purchase_id
    assert snap["user_action"] == "bought"

    # Second buy with no matching alert → no link
    purchase_id_2 = await business_repo.add_purchase(
        nm_id=99999, supplier_article=None,
        quantity=5, buy_price_per_unit=100.0,
        spp_at_purchase=None, notes=None,
    )
    found2 = await decision_repo.find_most_recent_unlinked(nm_id=99999)
    assert found2 is None  # no snapshot ever recorded for 99999

    # Distribution shows the bought one
    dist = await decision_repo.distribution(days=30)
    assert dist["by_action"]["bought"] == 1
    await db.close()
