from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.storage.db import Database

PHANTOM_LOT_QTY = 1_000_000_000


@dataclass(slots=True)
class Lot:
    lot_id: str
    purchase_id: int | None
    nm_id: int
    qty: int
    avg_buy_price: float | None
    opened_at: str
    status: str
    build_id: str
    qty_sold: int = 0
    qty_returned: int = 0
    qty_adjusted: int = 0
    qty_open: int = 0


@dataclass(slots=True)
class Allocation:
    id: int
    lot_id: str
    event_type: str
    srid: str
    qty: int
    allocated_cost: float | None
    event_at: str
    build_id: str


def lot_from_row(row: Any) -> Lot:
    return Lot(
        lot_id=str(row["lot_id"]),
        purchase_id=int(row["purchase_id"]) if row["purchase_id"] is not None else None,
        nm_id=int(row["nm_id"]),
        qty=int(row["qty"]),
        avg_buy_price=float(row["avg_buy_price"]) if row["avg_buy_price"] is not None else None,
        opened_at=str(row["opened_at"]),
        status=str(row["status"]),
        build_id=str(row["build_id"]),
        qty_sold=int(row["qty_sold"] or 0),
        qty_returned=int(row["qty_returned"] or 0),
        qty_adjusted=int(row["qty_adjusted"] or 0),
        qty_open=int(row["qty_open"] or 0),
    )


def allocation_from_row(row: Any) -> Allocation:
    return Allocation(
        id=int(row["id"]),
        lot_id=str(row["lot_id"]),
        event_type=str(row["event_type"]),
        srid=str(row["srid"]),
        qty=int(row["qty"]),
        allocated_cost=float(row["allocated_cost"]) if row["allocated_cost"] is not None else None,
        event_at=str(row["event_at"]),
        build_id=str(row["build_id"]),
    )


class LotLedgerRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def insert_lot(
        self,
        lot_id: str,
        nm_id: int,
        qty: int,
        avg_buy_price: float | None,
        opened_at: str,
        status: str,
        build_id: str,
    ) -> None:
        purchase_id = self._purchase_id_from_lot_id(lot_id)
        await self._db.execute(
            """
            INSERT INTO lots (
                lot_id, purchase_id, nm_id, qty, avg_buy_price,
                opened_at, status, build_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(lot_id) DO UPDATE SET
                purchase_id = excluded.purchase_id,
                nm_id = excluded.nm_id,
                qty = excluded.qty,
                avg_buy_price = excluded.avg_buy_price,
                opened_at = excluded.opened_at,
                status = excluded.status,
                build_id = excluded.build_id
            """,
            (lot_id, purchase_id, nm_id, qty, avg_buy_price, opened_at, status, build_id),
        )

    async def insert_allocation(
        self,
        lot_id: str,
        event_type: str,
        srid: str,
        qty: int,
        allocated_cost: float | None,
        event_at: str,
        build_id: str,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO lot_allocations (
                lot_id, event_type, srid, qty, allocated_cost, event_at, build_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(lot_id, event_type, srid) DO UPDATE SET
                qty = excluded.qty,
                allocated_cost = excluded.allocated_cost,
                event_at = excluded.event_at,
                build_id = excluded.build_id
            """,
            (lot_id, event_type, srid, qty, allocated_cost, event_at, build_id),
        )

    async def open_lots_for(self, nm_id: int) -> list[Lot]:
        rows = await self._db.fetchall(
            """
            SELECT *
            FROM lot_aggregates
            WHERE nm_id = ? AND qty_open > 0
            ORDER BY opened_at ASC, lot_id ASC
            """,
            (nm_id,),
        )
        return [lot_from_row(row) for row in rows]

    async def find_lot_for_sale(self, nm_id: int, sale_date: str) -> Lot | None:
        row = await self._db.fetchone(
            """
            SELECT *
            FROM lot_aggregates
            WHERE nm_id = ?
              AND qty_open > 0
              AND status != 'phantom_opening'
              AND opened_at <= ?
            ORDER BY opened_at ASC, lot_id ASC
            LIMIT 1
            """,
            (nm_id, sale_date),
        )
        if row is not None:
            return lot_from_row(row)

        phantom = await self._db.fetchone(
            """
            SELECT *
            FROM lot_aggregates
            WHERE nm_id = ?
              AND qty_open > 0
              AND status = 'phantom_opening'
            ORDER BY opened_at ASC, lot_id ASC
            LIMIT 1
            """,
            (nm_id,),
        )
        return lot_from_row(phantom) if phantom is not None else None

    async def list_allocations_by_srid(self, srid: str) -> list[Allocation]:
        rows = await self._db.fetchall(
            """
            SELECT *
            FROM lot_allocations
            WHERE srid = ?
            ORDER BY event_at ASC, id ASC
            """,
            (srid,),
        )
        return [allocation_from_row(row) for row in rows]

    async def ensure_phantom_lot(self, nm_id: int, first_sale_date: str, build_id: str) -> str:
        lot_id = f"phantom:{nm_id}"
        existing = await self._db.fetchone("SELECT lot_id FROM lots WHERE lot_id = ?", (lot_id,))
        if existing is not None:
            return lot_id

        purchase_before_sale = await self._db.fetchone(
            """
            SELECT id
            FROM purchases
            WHERE nm_id = ? AND date <= ?
            ORDER BY date ASC, id ASC
            LIMIT 1
            """,
            (nm_id, first_sale_date),
        )
        if purchase_before_sale is not None:
            return ""

        await self.insert_lot(
            lot_id=lot_id,
            nm_id=nm_id,
            qty=PHANTOM_LOT_QTY,
            avg_buy_price=None,
            opened_at=first_sale_date,
            status="phantom_opening",
            build_id=build_id,
        )
        return lot_id

    @staticmethod
    def _purchase_id_from_lot_id(lot_id: str) -> int | None:
        if not lot_id.startswith("p:"):
            return None
        raw = lot_id.removeprefix("p:")
        return int(raw) if raw.isdigit() else None
