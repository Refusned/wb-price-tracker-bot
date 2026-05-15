"""Migration 002: lot ledger tables and aggregate view."""
from __future__ import annotations

from typing import Any

VERSION = 2
NAME = "lot_ledger"


async def up(conn: Any) -> None:
    await conn.executescript(
        """
        -- Purchase rows are treated as append-only. A purchase lot id is
        -- deterministic and never re-used: lot_id = 'p:' || purchases.id.
        CREATE TABLE IF NOT EXISTS lots (
            lot_id TEXT PRIMARY KEY,
            purchase_id INTEGER UNIQUE,
            nm_id INTEGER NOT NULL,
            qty INTEGER NOT NULL CHECK (qty > 0),
            avg_buy_price REAL,
            opened_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('open', 'closed', 'phantom_opening')),
            build_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            FOREIGN KEY (purchase_id) REFERENCES purchases(id)
        );

        CREATE INDEX IF NOT EXISTS idx_lots_nm_opened_at
            ON lots(nm_id, opened_at, lot_id);
        CREATE INDEX IF NOT EXISTS idx_lots_status
            ON lots(status);

        CREATE TABLE IF NOT EXISTS lot_allocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lot_id TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK (event_type IN ('sale', 'return', 'adjustment')),
            srid TEXT NOT NULL,
            qty INTEGER NOT NULL CHECK (qty <> 0),
            allocated_cost REAL,
            event_at TEXT NOT NULL,
            build_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            FOREIGN KEY (lot_id) REFERENCES lots(lot_id),
            UNIQUE (lot_id, event_type, srid)
        );

        CREATE INDEX IF NOT EXISTS idx_lot_allocations_lot_event_at
            ON lot_allocations(lot_id, event_at);
        CREATE INDEX IF NOT EXISTS idx_lot_allocations_srid
            ON lot_allocations(srid);
        CREATE INDEX IF NOT EXISTS idx_lot_allocations_event_srid
            ON lot_allocations(event_type, srid);

        CREATE VIEW IF NOT EXISTS lot_aggregates AS
        SELECT
            l.lot_id,
            l.purchase_id,
            l.nm_id,
            l.qty,
            l.avg_buy_price,
            l.opened_at,
            l.status,
            l.build_id,
            COALESCE(SUM(CASE WHEN a.event_type = 'sale' THEN a.qty ELSE 0 END), 0) AS qty_sold,
            COALESCE(SUM(CASE WHEN a.event_type = 'return' THEN a.qty ELSE 0 END), 0) AS qty_returned,
            COALESCE(SUM(CASE WHEN a.event_type = 'adjustment' THEN a.qty ELSE 0 END), 0) AS qty_adjusted,
            l.qty - COALESCE(SUM(
                CASE
                    WHEN a.event_type = 'sale' THEN a.qty
                    WHEN a.event_type = 'return' THEN -a.qty
                    WHEN a.event_type = 'adjustment' THEN a.qty
                    ELSE 0
                END
            ), 0) AS qty_open
        FROM lots l
        LEFT JOIN lot_allocations a ON a.lot_id = l.lot_id
        GROUP BY
            l.lot_id,
            l.purchase_id,
            l.nm_id,
            l.qty,
            l.avg_buy_price,
            l.opened_at,
            l.status,
            l.build_id;
        """
    )
