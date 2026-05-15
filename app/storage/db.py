from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite


TransactionCallable = Callable[[aiosqlite.Connection], Awaitable[None]]


class Database:
    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path.as_posix())
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA synchronous=NORMAL;")
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def migrate(self) -> None:
        conn = self._require_conn()
        async with self._lock:
            await conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    nm_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    price_rub REAL NOT NULL,
                    old_price_rub REAL NULL,
                    in_stock INTEGER NOT NULL,
                    stock_qty INTEGER NULL,
                    url TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE TABLE IF NOT EXISTS subscribers (
                    chat_id INTEGER PRIMARY KEY,
                    user_id INTEGER NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS item_price_stats (
                    nm_id TEXT PRIMARY KEY,
                    min_price_rub REAL NOT NULL,
                    last_seen_price_rub REAL NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_alert_price_rub REAL NULL,
                    last_alert_at TEXT NULL
                );

                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nm_id TEXT NOT NULL,
                    price_rub REAL NOT NULL,
                    stock_qty INTEGER,
                    scanned_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_ph_nm_date
                    ON price_history(nm_id, scanned_at);

                CREATE TABLE IF NOT EXISTS tracked_articles (
                    nm_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    miss_count INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1
                );

                -- Own orders (from WB Statistics API supplier/orders)
                CREATE TABLE IF NOT EXISTS own_orders (
                    srid TEXT PRIMARY KEY,
                    g_number TEXT NOT NULL,
                    date TEXT NOT NULL,
                    last_change_date TEXT NOT NULL,
                    nm_id INTEGER NOT NULL,
                    supplier_article TEXT,
                    subject TEXT,
                    warehouse_name TEXT,
                    total_price REAL NOT NULL,
                    price_with_disc REAL NOT NULL,
                    spp_percent REAL NOT NULL DEFAULT 0,
                    discount_percent REAL NOT NULL DEFAULT 0,
                    is_cancel INTEGER NOT NULL DEFAULT 0,
                    cancel_date TEXT,
                    first_seen_at TEXT NOT NULL,
                    notified INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_own_orders_date ON own_orders(date);
                CREATE INDEX IF NOT EXISTS idx_own_orders_nm_id ON own_orders(nm_id);
                CREATE INDEX IF NOT EXISTS idx_own_orders_notified ON own_orders(notified);

                -- Own sales and returns (from WB Statistics API supplier/sales)
                CREATE TABLE IF NOT EXISTS own_sales (
                    srid TEXT PRIMARY KEY,
                    g_number TEXT NOT NULL,
                    date TEXT NOT NULL,
                    last_change_date TEXT NOT NULL,
                    nm_id INTEGER NOT NULL,
                    supplier_article TEXT,
                    subject TEXT,
                    brand TEXT,
                    category TEXT,
                    warehouse_name TEXT,
                    total_price REAL NOT NULL,
                    for_pay REAL NOT NULL,
                    price_with_disc REAL NOT NULL,
                    spp_percent REAL NOT NULL DEFAULT 0,
                    commission_percent REAL NOT NULL DEFAULT 0,
                    discount_percent REAL NOT NULL DEFAULT 0,
                    is_return INTEGER NOT NULL DEFAULT 0,
                    order_type TEXT,
                    first_seen_at TEXT NOT NULL,
                    notified INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_own_sales_date ON own_sales(date);
                CREATE INDEX IF NOT EXISTS idx_own_sales_nm_id ON own_sales(nm_id);
                CREATE INDEX IF NOT EXISTS idx_own_sales_is_return ON own_sales(is_return);
                CREATE INDEX IF NOT EXISTS idx_own_sales_notified ON own_sales(notified);

                -- Stock snapshots (latest per nm+warehouse)
                CREATE TABLE IF NOT EXISTS own_stocks (
                    nm_id INTEGER NOT NULL,
                    supplier_article TEXT,
                    warehouse_name TEXT NOT NULL,
                    quantity INTEGER NOT NULL DEFAULT 0,
                    in_way_to_client INTEGER NOT NULL DEFAULT 0,
                    in_way_from_client INTEGER NOT NULL DEFAULT 0,
                    quantity_full INTEGER NOT NULL DEFAULT 0,
                    subject TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (nm_id, warehouse_name)
                );

                -- Manual purchase records (user's arbitrage purchases)
                CREATE TABLE IF NOT EXISTS purchases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    nm_id INTEGER,
                    supplier_article TEXT,
                    quantity INTEGER NOT NULL,
                    buy_price_per_unit REAL NOT NULL,
                    spp_at_purchase REAL,
                    total_cost REAL NOT NULL,
                    notes TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_purchases_date ON purchases(date);

                -- WB payouts (will be populated from financial reports)
                CREATE TABLE IF NOT EXISTS wb_payouts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    total_revenue REAL NOT NULL DEFAULT 0,
                    total_commission REAL NOT NULL DEFAULT 0,
                    total_logistics REAL NOT NULL DEFAULT 0,
                    total_storage REAL NOT NULL DEFAULT 0,
                    total_fines REAL NOT NULL DEFAULT 0,
                    net_payout REAL NOT NULL DEFAULT 0,
                    paid_at TEXT,
                    created_at TEXT NOT NULL
                );

                -- Финотчёт /api/v5/supplier/reportDetailByPeriod — полный журнал
                CREATE TABLE IF NOT EXISTS finance_journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rrd_id INTEGER UNIQUE,
                    realizationreport_id INTEGER,
                    nm_id INTEGER,
                    supplier_article TEXT,
                    subject_name TEXT,
                    doc_type_name TEXT,
                    supplier_oper_name TEXT,
                    order_dt TEXT,
                    sale_dt TEXT,
                    rr_dt TEXT,
                    quantity INTEGER,
                    retail_amount REAL,
                    retail_price_withdisc_rub REAL,
                    ppvz_for_pay REAL,
                    ppvz_sales_commission REAL,
                    delivery_rub REAL,
                    storage_fee REAL,
                    penalty REAL,
                    acceptance REAL,
                    deduction REAL,
                    additional_payment REAL,
                    srid TEXT,
                    fetched_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_finance_nm_id ON finance_journal(nm_id);
                CREATE INDEX IF NOT EXISTS idx_finance_rrd ON finance_journal(rr_dt);
                CREATE INDEX IF NOT EXISTS idx_finance_oper ON finance_journal(supplier_oper_name);
                """
            )
            await conn.commit()

    async def apply_migrations(self) -> list[int]:
        """Run versioned migrations from app.storage.migrations.

        Creates the ``schema_migrations`` table on first run, then iterates the
        ``MIGRATIONS`` list from ``app.storage.migrations``. Each migration is
        applied at most once (idempotent across restarts).

        Returns the list of newly-applied version numbers. Existing-applied ones
        are silently skipped.

        Call AFTER ``migrate()`` so the base ``CREATE TABLE IF NOT EXISTS``
        schema is in place. Migrations should handle additive schema changes
        (new tables, new columns via ALTER, new indexes).
        """
        # Lazy import to avoid circular dependency at module load
        from app.storage import migrations as _migrations_module

        conn = self._require_conn()
        applied_now: list[int] = []
        async with self._lock:
            # Bootstrap the schema_migrations table itself
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            await conn.commit()

            # Read already-applied versions
            cursor = await conn.execute("SELECT version FROM schema_migrations")
            applied_rows = await cursor.fetchall()
            await cursor.close()
            already_applied = {row[0] for row in applied_rows}

            # Apply pending migrations in registry order
            for mig in _migrations_module.MIGRATIONS:
                if mig.VERSION in already_applied:
                    continue
                await mig.up(conn)
                await conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) "
                    "VALUES (?, ?, ?)",
                    (
                        mig.VERSION,
                        mig.NAME,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                await conn.commit()
                applied_now.append(mig.VERSION)

        return applied_now

    async def execute(self, query: str, params: Sequence[Any] | None = None) -> None:
        conn = self._require_conn()
        async with self._lock:
            await conn.execute(query, params or ())
            await conn.commit()

    async def fetchone(self, query: str, params: Sequence[Any] | None = None) -> aiosqlite.Row | None:
        conn = self._require_conn()
        async with self._lock:
            cursor = await conn.execute(query, params or ())
            row = await cursor.fetchone()
            await cursor.close()
            return row

    async def fetchall(self, query: str, params: Sequence[Any] | None = None) -> list[aiosqlite.Row]:
        conn = self._require_conn()
        async with self._lock:
            cursor = await conn.execute(query, params or ())
            rows = await cursor.fetchall()
            await cursor.close()
            return rows

    async def transaction(self, callback: TransactionCallable) -> None:
        conn = self._require_conn()
        async with self._lock:
            await conn.execute("BEGIN")
            try:
                await callback(conn)
            except Exception:
                await conn.rollback()
                raise
            else:
                await conn.commit()

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn
