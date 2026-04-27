from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.storage.db import Database
from app.storage.models import Item
from app.storage.repositories import PriceStatsRepository


def _item(nm_id: str, price: float, in_stock: bool = True) -> Item:
    return Item(
        nm_id=nm_id,
        name=f"Item {nm_id}",
        price_rub=price,
        old_price_rub=None,
        in_stock=in_stock,
        stock_qty=10 if in_stock else 0,
        url=f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx",
    )


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def test_first_scan_no_alert(tmp_path) -> None:
    db_path = tmp_path / "alerts.db"

    async def scenario() -> None:
        db = Database(db_path.as_posix())
        await db.connect()
        await db.migrate()
        repo = PriceStatsRepository(db)
        now = datetime.now(timezone.utc)

        events = await repo.record_snapshot_and_collect_drops(
            [_item("100", 12000.0)],
            alert_nm_ids={"100"},
            drop_threshold_percent=5.0,
            observed_at=_iso(now),
        )
        assert events == []
        await db.close()

    asyncio.run(scenario())


def test_drop_5_percent_in_top20_alerts(tmp_path) -> None:
    db_path = tmp_path / "alerts.db"

    async def scenario() -> None:
        db = Database(db_path.as_posix())
        await db.connect()
        await db.migrate()
        repo = PriceStatsRepository(db)
        t0 = datetime.now(timezone.utc)

        # Baseline at 12000
        await repo.record_snapshot_and_collect_drops(
            [_item("100", 12000.0)],
            alert_nm_ids={"100"},
            drop_threshold_percent=5.0,
            observed_at=_iso(t0),
        )

        # 5 minutes later, dropped to 11400 (-5%)
        t1 = t0 + timedelta(minutes=5)
        events = await repo.record_snapshot_and_collect_drops(
            [_item("100", 11400.0)],
            alert_nm_ids={"100"},
            drop_threshold_percent=5.0,
            observed_at=_iso(t1),
            rank_map={"100": 3},
        )
        assert len(events) == 1
        assert events[0].nm_id == "100"
        assert events[0].previous_price_rub == 12000.0
        assert events[0].new_price_rub == 11400.0
        assert events[0].top_rank == 3
        await db.close()

    asyncio.run(scenario())


def test_drop_below_threshold_no_alert(tmp_path) -> None:
    db_path = tmp_path / "alerts.db"

    async def scenario() -> None:
        db = Database(db_path.as_posix())
        await db.connect()
        await db.migrate()
        repo = PriceStatsRepository(db)
        t0 = datetime.now(timezone.utc)

        await repo.record_snapshot_and_collect_drops(
            [_item("100", 12000.0)],
            alert_nm_ids={"100"},
            drop_threshold_percent=5.0,
            observed_at=_iso(t0),
        )

        # Drop to 11550 = -3.75% < 5%
        events = await repo.record_snapshot_and_collect_drops(
            [_item("100", 11550.0)],
            alert_nm_ids={"100"},
            drop_threshold_percent=5.0,
            observed_at=_iso(t0 + timedelta(minutes=5)),
        )
        assert events == []
        await db.close()

    asyncio.run(scenario())


def test_drop_outside_top20_no_alert(tmp_path) -> None:
    db_path = tmp_path / "alerts.db"

    async def scenario() -> None:
        db = Database(db_path.as_posix())
        await db.connect()
        await db.migrate()
        repo = PriceStatsRepository(db)
        t0 = datetime.now(timezone.utc)

        await repo.record_snapshot_and_collect_drops(
            [_item("100", 12000.0)],
            alert_nm_ids={"100"},
            drop_threshold_percent=5.0,
            observed_at=_iso(t0),
        )

        # Item 100 NOT in alert_nm_ids — even 10% drop won't alert
        events = await repo.record_snapshot_and_collect_drops(
            [_item("100", 10800.0)],
            alert_nm_ids={"999"},  # different item in top
            drop_threshold_percent=5.0,
            observed_at=_iso(t0 + timedelta(minutes=5)),
        )
        assert events == []
        await db.close()

    asyncio.run(scenario())


def test_cooldown_prevents_second_alert(tmp_path) -> None:
    db_path = tmp_path / "alerts.db"

    async def scenario() -> None:
        db = Database(db_path.as_posix())
        await db.connect()
        await db.migrate()
        repo = PriceStatsRepository(db)
        t0 = datetime.now(timezone.utc)

        # Baseline
        await repo.record_snapshot_and_collect_drops(
            [_item("100", 12000.0)],
            alert_nm_ids={"100"},
            drop_threshold_percent=5.0,
            observed_at=_iso(t0),
        )

        # First drop — alert fires
        events = await repo.record_snapshot_and_collect_drops(
            [_item("100", 11400.0)],
            alert_nm_ids={"100"},
            drop_threshold_percent=5.0,
            observed_at=_iso(t0 + timedelta(minutes=5)),
            alert_cooldown_minutes=30,
        )
        assert len(events) == 1

        # Second drop within cooldown — no alert
        events = await repo.record_snapshot_and_collect_drops(
            [_item("100", 10800.0)],
            alert_nm_ids={"100"},
            drop_threshold_percent=5.0,
            observed_at=_iso(t0 + timedelta(minutes=20)),
            alert_cooldown_minutes=30,
        )
        assert events == []

        # After cooldown — alert OK
        events = await repo.record_snapshot_and_collect_drops(
            [_item("100", 10200.0)],
            alert_nm_ids={"100"},
            drop_threshold_percent=5.0,
            observed_at=_iso(t0 + timedelta(minutes=60)),
            alert_cooldown_minutes=30,
        )
        assert len(events) == 1
        await db.close()

    asyncio.run(scenario())


def test_stale_data_no_alert(tmp_path) -> None:
    """If last_seen_at is older than stale_data_hours, don't alert (item was absent)."""
    db_path = tmp_path / "alerts.db"

    async def scenario() -> None:
        db = Database(db_path.as_posix())
        await db.connect()
        await db.migrate()
        repo = PriceStatsRepository(db)
        t0 = datetime.now(timezone.utc)

        # Baseline at 12000
        await repo.record_snapshot_and_collect_drops(
            [_item("100", 12000.0)],
            alert_nm_ids={"100"},
            drop_threshold_percent=5.0,
            observed_at=_iso(t0),
        )

        # Item disappears for 2 hours, then reappears with 10% drop
        # Since data is stale, we shouldn't alert (could be WB-wide price change)
        events = await repo.record_snapshot_and_collect_drops(
            [_item("100", 10800.0)],
            alert_nm_ids={"100"},
            drop_threshold_percent=5.0,
            observed_at=_iso(t0 + timedelta(hours=2)),
            stale_data_hours=1,
        )
        assert events == []
        await db.close()

    asyncio.run(scenario())


def test_price_drop_alert_triggers_on_5_percent_drop(tmp_path) -> None:
    """Legacy test kept as smoke test."""
    db_path = tmp_path / "alerts.db"

    async def scenario() -> None:
        db = Database(db_path.as_posix())
        await db.connect()
        await db.migrate()
        repo = PriceStatsRepository(db)
        t0 = datetime.now(timezone.utc)

        # First snapshot — baseline
        events = await repo.record_snapshot_and_collect_drops(
            [_item("100", 12000.0)],
            alert_nm_ids={"100"},
            drop_threshold_percent=5.0,
            observed_at=_iso(t0),
        )
        assert events == []

        # Drop <5% — no alert
        events = await repo.record_snapshot_and_collect_drops(
            [_item("100", 11550.0)],
            alert_nm_ids={"100"},
            drop_threshold_percent=5.0,
            observed_at=_iso(t0 + timedelta(minutes=5)),
        )
        assert events == []

        # Drop >=5% from previous (11550) — should alert
        # 11550 * 0.95 = 10972.5, so 10950 triggers
        events = await repo.record_snapshot_and_collect_drops(
            [_item("100", 10950.0)],
            alert_nm_ids={"100"},
            drop_threshold_percent=5.0,
            observed_at=_iso(t0 + timedelta(minutes=10)),
        )
        assert len(events) == 1
        assert events[0].nm_id == "100"

        await db.close()

    asyncio.run(scenario())
