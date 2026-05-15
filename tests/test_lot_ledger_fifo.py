from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.storage.db import Database
from tools.build_lot_ledger import build_lot_ledger


pytestmark = pytest.mark.asyncio


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def make_db(path: Path) -> Database:
    db = Database(path.as_posix())
    await db.connect()
    await db.migrate()
    await db.apply_migrations()
    return db


async def add_purchase(
    db: Database,
    *,
    date: str,
    nm_id: int,
    quantity: int,
    buy_price: float,
) -> None:
    await db.execute(
        """
        INSERT INTO purchases (
            date, nm_id, quantity, buy_price_per_unit, total_cost, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (date, nm_id, quantity, buy_price, quantity * buy_price, now()),
    )


async def add_sale(
    db: Database,
    *,
    srid: str,
    date: str,
    nm_id: int,
    is_return: int = 0,
) -> None:
    await db.execute(
        """
        INSERT INTO own_sales (
            srid, g_number, date, last_change_date, nm_id,
            total_price, for_pay, price_with_disc, is_return, first_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (srid, f"g-{srid}", date, date, nm_id, 1000.0, 900.0, 950.0, is_return, now()),
    )


async def add_order(
    db: Database,
    *,
    srid: str,
    date: str,
    nm_id: int,
    is_cancel: int = 0,
    cancel_date: str | None = None,
) -> None:
    await db.execute(
        """
        INSERT INTO own_orders (
            srid, g_number, date, last_change_date, nm_id,
            total_price, price_with_disc, is_cancel, cancel_date, first_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (srid, f"g-{srid}", date, cancel_date or date, nm_id, 1000.0, 950.0, is_cancel, cancel_date, now()),
    )


async def reopen(path: Path) -> Database:
    db = Database(path.as_posix())
    await db.connect()
    return db


async def test_simple_buy_sell(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    db = await make_db(db_path)
    await add_purchase(db, date="2024-01-01", nm_id=100, quantity=2, buy_price=100.0)
    await add_sale(db, srid="s1", date="2024-01-02", nm_id=100)
    await db.close()

    await build_lot_ledger(db_path.as_posix())

    db = await reopen(db_path)
    lot = await db.fetchone("SELECT * FROM lot_aggregates WHERE lot_id = 'p:1'")
    alloc = await db.fetchone("SELECT * FROM lot_allocations WHERE srid = 's1'")
    await db.close()

    assert lot["qty_sold"] == 1
    assert lot["qty_open"] == 1
    assert alloc["lot_id"] == "p:1"
    assert alloc["allocated_cost"] == 100.0


async def test_partial_sale_then_return(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    db = await make_db(db_path)
    await add_purchase(db, date="2024-01-01", nm_id=100, quantity=1, buy_price=100.0)
    await add_sale(db, srid="s1", date="2024-01-02", nm_id=100)
    await add_sale(db, srid="r1", date="2024-01-03", nm_id=100, is_return=1)
    await db.close()

    await build_lot_ledger(db_path.as_posix())

    db = await reopen(db_path)
    rows = await db.fetchall("SELECT event_type, qty FROM lot_allocations ORDER BY event_type")
    lot = await db.fetchone("SELECT * FROM lot_aggregates WHERE lot_id = 'p:1'")
    await db.close()

    assert {row["event_type"]: row["qty"] for row in rows} == {"return": 1, "sale": 1}
    assert lot["qty_sold"] == 1
    assert lot["qty_returned"] == 1
    assert lot["qty_open"] == 1


async def test_multi_lot_fifo_ordering(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    db = await make_db(db_path)
    await add_purchase(db, date="2024-01-01", nm_id=100, quantity=1, buy_price=100.0)
    await add_purchase(db, date="2024-01-02", nm_id=100, quantity=1, buy_price=200.0)
    await add_sale(db, srid="s1", date="2024-01-03", nm_id=100)
    await add_sale(db, srid="s2", date="2024-01-04", nm_id=100)
    await db.close()

    await build_lot_ledger(db_path.as_posix())

    db = await reopen(db_path)
    rows = await db.fetchall("SELECT srid, lot_id, allocated_cost FROM lot_allocations ORDER BY srid")
    await db.close()

    assert [(row["srid"], row["lot_id"], row["allocated_cost"]) for row in rows] == [
        ("s1", "p:1", 100.0),
        ("s2", "p:2", 200.0),
    ]


async def test_sale_before_purchase_phantom(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    db = await make_db(db_path)
    await add_sale(db, srid="s1", date="2024-01-01", nm_id=100)
    await add_purchase(db, date="2024-01-02", nm_id=100, quantity=1, buy_price=100.0)
    await db.close()

    await build_lot_ledger(db_path.as_posix())

    db = await reopen(db_path)
    lot = await db.fetchone("SELECT * FROM lots WHERE lot_id = 'phantom:100'")
    alloc = await db.fetchone("SELECT * FROM lot_allocations WHERE srid = 's1'")
    await db.close()

    assert lot["status"] == "phantom_opening"
    assert lot["avg_buy_price"] is None
    assert alloc["lot_id"] == "phantom:100"


async def test_cancelled_order_rollback(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    db = await make_db(db_path)
    await add_purchase(db, date="2024-01-01", nm_id=100, quantity=1, buy_price=100.0)
    await add_sale(db, srid="s1", date="2024-01-02", nm_id=100)
    await add_order(db, srid="s1", date="2024-01-02", nm_id=100, is_cancel=1, cancel_date="2024-01-03")
    await db.close()

    await build_lot_ledger(db_path.as_posix())

    db = await reopen(db_path)
    adjustment = await db.fetchone(
        "SELECT * FROM lot_allocations WHERE srid = 's1' AND event_type = 'adjustment'"
    )
    lot = await db.fetchone("SELECT * FROM lot_aggregates WHERE lot_id = 'p:1'")
    await db.close()

    assert adjustment["qty"] == -1
    assert lot["qty_open"] == 1


async def test_idempotent_backfill_rerun(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    db = await make_db(db_path)
    await add_purchase(db, date="2024-01-01", nm_id=100, quantity=1, buy_price=100.0)
    await add_sale(db, srid="s1", date="2024-01-02", nm_id=100)
    await db.close()

    await build_lot_ledger(db_path.as_posix())
    await build_lot_ledger(db_path.as_posix())

    db = await reopen(db_path)
    allocations = await db.fetchone("SELECT COUNT(*) AS c FROM lot_allocations")
    lots = await db.fetchone("SELECT COUNT(*) AS c FROM lots")
    await db.close()

    assert allocations["c"] == 1
    assert lots["c"] == 1


async def test_open_lots_view(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    db = await make_db(db_path)
    await add_purchase(db, date="2024-01-01", nm_id=100, quantity=3, buy_price=100.0)
    await add_sale(db, srid="s1", date="2024-01-02", nm_id=100)
    await add_sale(db, srid="s2", date="2024-01-03", nm_id=100)
    await add_sale(db, srid="r1", date="2024-01-04", nm_id=100, is_return=1)
    await db.close()

    await build_lot_ledger(db_path.as_posix())

    db = await reopen(db_path)
    lot = await db.fetchone("SELECT qty_sold, qty_returned, qty_open FROM lot_aggregates WHERE lot_id = 'p:1'")
    await db.close()

    assert lot["qty_sold"] == 2
    assert lot["qty_returned"] == 1
    assert lot["qty_open"] == 2
