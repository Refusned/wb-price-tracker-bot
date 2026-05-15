from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if ROOT.as_posix() not in sys.path:
    sys.path.insert(0, ROOT.as_posix())

from app.storage.db import Database
from app.storage.lot_ledger_repository import (  # noqa: E402
    Allocation,
    Lot,
    LotLedgerRepository,
    allocation_from_row,
)


@dataclass(slots=True)
class BuildSummary:
    build_id: str
    lots_created: int
    allocations_created: int
    total_lots: int
    total_allocations: int
    phantom_opening_count: int
    elapsed_seconds: float


def _backup_database(db_path: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = Path(db_path.as_posix() + f".bak-build-{timestamp}")
    shutil.copy2(db_path, backup_path)
    print(f"Backup created: {backup_path}")
    return backup_path


def _valid_date_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return text


def _fifo_date(value: Any) -> str:
    return _valid_date_or_none(value) or "9999-12-31T23:59:59+00:00"


def _event_at(value: Any, fallback: str) -> str:
    return _valid_date_or_none(value) or fallback


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _allocated_cost(lot: Lot, qty: int) -> float | None:
    if lot.avg_buy_price is None:
        return None
    return lot.avg_buy_price * qty


async def _count(db: Database, table: str, where: str = "1 = 1") -> int:
    row = await db.fetchone(f"SELECT COUNT(*) AS c FROM {table} WHERE {where}")
    return int(row["c"] if row is not None else 0)


async def _lot_by_id(db: Database, lot_id: str) -> Lot | None:
    row = await db.fetchone("SELECT * FROM lot_aggregates WHERE lot_id = ?", (lot_id,))
    if row is None:
        return None
    from app.storage.lot_ledger_repository import lot_from_row

    return lot_from_row(row)


async def _sale_allocations_for_return(
    db: Database,
    repo: LotLedgerRepository,
    nm_id: int,
    return_srid: str,
    return_event_at: str,
) -> list[Allocation]:
    # Case 1: return shares srid with the original sale (rare in WB API but allowed).
    same_srid = [
        allocation
        for allocation in await repo.list_allocations_by_srid(return_srid)
        if allocation.event_type == "sale"
    ]
    if same_srid:
        return same_srid

    # Case 2: separate-srid return (WB common pattern). Match FIFO style — find the
    # oldest sale of this nm_id that still has un-returned units (units sold minus
    # units already returned from same sale srid > 0). This preserves cost-basis
    # accuracy: returns reduce the same lot the original sale consumed.
    rows = await db.fetchall(
        """
        SELECT a.*
        FROM lot_allocations a
        JOIN lots l ON l.lot_id = a.lot_id
        LEFT JOIN (
            SELECT srid AS ret_srid, SUM(qty) AS returned_qty
            FROM lot_allocations
            WHERE event_type = 'return'
            GROUP BY srid
        ) r ON r.ret_srid = a.srid
        WHERE l.nm_id = ?
          AND a.event_type = 'sale'
          AND a.event_at <= ?
          AND (a.qty - COALESCE(r.returned_qty, 0)) > 0
        ORDER BY a.event_at ASC, a.id ASC
        LIMIT 1
        """,
        (nm_id, return_event_at),
    )
    return [allocation_from_row(row) for row in rows]


async def _insert_purchase_lots(db: Database, repo: LotLedgerRepository, build_id: str) -> None:
    rows = await db.fetchall(
        """
        SELECT id, date, nm_id, quantity, buy_price_per_unit
        FROM purchases
        ORDER BY date ASC, id ASC
        """
    )
    for row in rows:
        purchase_id = _int_or_none(row["id"])
        nm_id = _int_or_none(row["nm_id"])
        qty = _int_or_none(row["quantity"])
        if purchase_id is None or nm_id is None or qty is None or qty <= 0:
            continue

        await repo.insert_lot(
            lot_id=f"p:{purchase_id}",
            nm_id=nm_id,
            qty=qty,
            avg_buy_price=_float_or_none(row["buy_price_per_unit"]),
            opened_at=_event_at(row["date"], build_id),
            status="open",
            build_id=build_id,
        )


async def _insert_sale_allocations(db: Database, repo: LotLedgerRepository, build_id: str) -> None:
    rows = await db.fetchall(
        """
        SELECT srid, date, nm_id
        FROM own_sales
        WHERE COALESCE(is_return, 0) = 0
        ORDER BY date ASC, srid ASC
        """
    )
    for row in rows:
        srid = str(row["srid"] or "").strip()
        nm_id = _int_or_none(row["nm_id"])
        if not srid or nm_id is None:
            continue

        # Idempotency guard: if this srid is already allocated as 'sale' anywhere,
        # skip re-allocation. Otherwise re-runs would attribute the same sale to a
        # different lot (the next open one) on each invocation.
        existing_sale = [
            a for a in await repo.list_allocations_by_srid(srid) if a.event_type == "sale"
        ]
        if existing_sale:
            continue

        sale_fifo_date = _fifo_date(row["date"])
        sale_event_at = _event_at(row["date"], build_id)
        lot = await repo.find_lot_for_sale(nm_id, sale_fifo_date)

        if lot is None:
            phantom_id = await repo.ensure_phantom_lot(nm_id, sale_fifo_date, build_id)
            lot = await _lot_by_id(db, phantom_id) if phantom_id else None

        if lot is None:
            print(f"Skipped sale without open lot: srid={srid} nm_id={nm_id}")
            continue

        await repo.insert_allocation(
            lot_id=lot.lot_id,
            event_type="sale",
            srid=srid,
            qty=1,
            allocated_cost=_allocated_cost(lot, 1),
            event_at=sale_event_at,
            build_id=build_id,
        )


async def _insert_return_allocations(db: Database, repo: LotLedgerRepository, build_id: str) -> None:
    rows = await db.fetchall(
        """
        SELECT srid, date, nm_id
        FROM own_sales
        WHERE COALESCE(is_return, 0) = 1
        ORDER BY date ASC, srid ASC
        """
    )
    for row in rows:
        srid = str(row["srid"] or "").strip()
        nm_id = _int_or_none(row["nm_id"])
        if not srid or nm_id is None:
            continue

        return_event_at = _event_at(row["date"], build_id)
        sale_allocations = await _sale_allocations_for_return(db, repo, nm_id, srid, return_event_at)
        if not sale_allocations:
            print(f"Skipped return without matching sale allocation: srid={srid} nm_id={nm_id}")
            continue

        for sale_allocation in sale_allocations:
            await repo.insert_allocation(
                lot_id=sale_allocation.lot_id,
                event_type="return",
                srid=srid,
                qty=abs(sale_allocation.qty),
                allocated_cost=sale_allocation.allocated_cost,
                event_at=return_event_at,
                build_id=build_id,
            )


async def _insert_cancel_adjustments(db: Database, repo: LotLedgerRepository, build_id: str) -> None:
    rows = await db.fetchall(
        """
        SELECT srid, date, cancel_date
        FROM own_orders
        WHERE COALESCE(is_cancel, 0) = 1
        ORDER BY COALESCE(cancel_date, date) ASC, srid ASC
        """
    )
    for row in rows:
        srid = str(row["srid"] or "").strip()
        if not srid:
            continue

        sale_allocations = [
            allocation
            for allocation in await repo.list_allocations_by_srid(srid)
            if allocation.event_type == "sale"
        ]
        if not sale_allocations:
            continue

        event_at = _event_at(row["cancel_date"] or row["date"], build_id)
        for sale_allocation in sale_allocations:
            cost = -sale_allocation.allocated_cost if sale_allocation.allocated_cost is not None else None
            await repo.insert_allocation(
                lot_id=sale_allocation.lot_id,
                event_type="adjustment",
                srid=srid,
                qty=-abs(sale_allocation.qty),
                allocated_cost=cost,
                event_at=event_at,
                build_id=build_id,
            )


async def build_lot_ledger(db_path: str = "data/app.db") -> BuildSummary:
    started = time.monotonic()
    build_id = datetime.now(timezone.utc).isoformat()
    path = Path(db_path)

    _backup_database(path)

    db = Database(path.as_posix())
    await db.connect()
    try:
        await db.migrate()
        await db.apply_migrations()

        before_lots = await _count(db, "lots")
        before_allocations = await _count(db, "lot_allocations")

        repo = LotLedgerRepository(db)
        await _insert_purchase_lots(db, repo, build_id)
        await _insert_sale_allocations(db, repo, build_id)
        await _insert_return_allocations(db, repo, build_id)
        await _insert_cancel_adjustments(db, repo, build_id)

        total_lots = await _count(db, "lots")
        total_allocations = await _count(db, "lot_allocations")
        phantom_count = await _count(db, "lots", "status = 'phantom_opening'")
    finally:
        await db.close()

    summary = BuildSummary(
        build_id=build_id,
        lots_created=total_lots - before_lots,
        allocations_created=total_allocations - before_allocations,
        total_lots=total_lots,
        total_allocations=total_allocations,
        phantom_opening_count=phantom_count,
        elapsed_seconds=time.monotonic() - started,
    )
    print(
        "Lot ledger build complete: "
        f"lots_created={summary.lots_created}, "
        f"allocations_created={summary.allocations_created}, "
        f"total_allocations={summary.total_allocations}, "
        f"phantom_opening_count={summary.phantom_opening_count}, "
        f"elapsed_seconds={summary.elapsed_seconds:.3f}"
    )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the lot ledger from purchases, sales, returns, and cancels.")
    parser.add_argument("--db-path", default="data/app.db", help="SQLite database path. Default: data/app.db")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(build_lot_ledger(args.db_path))
    except OSError as exc:
        print(f"Backup failed; aborting: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
