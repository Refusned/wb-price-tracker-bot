"""
BusinessRepository — работа с данными бизнеса (свои продажи/заказы/остатки/закупки).
Отделён от основных репозиториев для ясности.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.storage.db import Database
from app.wb.seller_client import OrderEvent, SaleEvent, StockEntry


@dataclass(slots=True)
class DailyMetrics:
    date: str
    orders_count: int
    orders_canceled: int
    sales_count: int
    returns_count: int
    revenue_total: float      # totalPrice sum
    revenue_net: float        # forPay sum (что WB реально платит)
    unique_articles: int
    buyout_rate: float        # sales / (sales + returns + canceled orders)


class BusinessRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ---------- Orders ----------

    async def upsert_orders(self, orders: Sequence[OrderEvent], seen_at: str) -> list[str]:
        """Insert new orders, return list of NEW srids (not seen before)."""
        if not orders:
            return []

        # Find which ones are new
        srids = [o.srid for o in orders if o.srid]
        if not srids:
            return []

        placeholders = ",".join(["?"] * len(srids))
        existing_rows = await self._db.fetchall(
            f"SELECT srid FROM own_orders WHERE srid IN ({placeholders})",
            srids,
        )
        existing = {str(r["srid"]) for r in existing_rows}
        new_srids = [s for s in srids if s not in existing]

        async def _tx(conn) -> None:
            payload_insert = []
            payload_update = []
            for o in orders:
                if not o.srid:
                    continue
                row = (
                    o.srid, o.g_number, o.date, o.last_change_date,
                    o.nm_id, o.supplier_article, o.subject, o.warehouse_name,
                    o.total_price, o.price_with_disc, o.spp_percent, o.discount_percent,
                    int(o.is_cancel), o.cancel_date, seen_at, 0,
                )
                if o.srid in existing:
                    payload_update.append((
                        o.last_change_date, int(o.is_cancel), o.cancel_date, o.srid,
                    ))
                else:
                    payload_insert.append(row)
            if payload_insert:
                await conn.executemany(
                    """
                    INSERT INTO own_orders (
                        srid, g_number, date, last_change_date, nm_id, supplier_article,
                        subject, warehouse_name, total_price, price_with_disc,
                        spp_percent, discount_percent, is_cancel, cancel_date,
                        first_seen_at, notified
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload_insert,
                )
            if payload_update:
                await conn.executemany(
                    "UPDATE own_orders SET last_change_date = ?, is_cancel = ?, cancel_date = ? WHERE srid = ?",
                    payload_update,
                )

        await self._db.transaction(_tx)
        return new_srids

    async def get_unnotified_orders(self, limit: int = 20) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM own_orders WHERE notified = 0 ORDER BY date ASC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    async def mark_orders_notified(self, srids: list[str]) -> None:
        if not srids:
            return
        placeholders = ",".join(["?"] * len(srids))
        await self._db.execute(
            f"UPDATE own_orders SET notified = 1 WHERE srid IN ({placeholders})",
            srids,
        )

    async def mark_all_orders_notified(self) -> int:
        """Mark every order as notified. Used on startup to avoid re-alert spam."""
        await self._db.execute("UPDATE own_orders SET notified = 1 WHERE notified = 0")
        return 0

    # ---------- Sales ----------

    async def upsert_sales(self, sales: Sequence[SaleEvent], seen_at: str) -> list[str]:
        """Insert sales/returns, return list of NEW srids."""
        if not sales:
            return []

        srids = [s.srid for s in sales if s.srid]
        if not srids:
            return []

        placeholders = ",".join(["?"] * len(srids))
        existing_rows = await self._db.fetchall(
            f"SELECT srid FROM own_sales WHERE srid IN ({placeholders})",
            srids,
        )
        existing = {str(r["srid"]) for r in existing_rows}
        new_srids = [s for s in srids if s not in existing]

        async def _tx(conn) -> None:
            payload = []
            for s in sales:
                if not s.srid or s.srid in existing:
                    continue
                payload.append((
                    s.srid, s.g_number, s.date, s.last_change_date,
                    s.nm_id, s.supplier_article, s.subject, s.brand, s.category,
                    s.warehouse_name, s.total_price, s.for_pay, s.price_with_disc,
                    s.spp_percent, s.commission_percent, s.discount_percent,
                    int(s.is_return), s.order_type, seen_at, 0,
                ))
            if payload:
                await conn.executemany(
                    """
                    INSERT INTO own_sales (
                        srid, g_number, date, last_change_date, nm_id, supplier_article,
                        subject, brand, category, warehouse_name, total_price, for_pay,
                        price_with_disc, spp_percent, commission_percent, discount_percent,
                        is_return, order_type, first_seen_at, notified
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload,
                )

        await self._db.transaction(_tx)
        return new_srids

    async def get_unnotified_sales(self, limit: int = 20) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM own_sales WHERE notified = 0 ORDER BY date ASC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    async def mark_sales_notified(self, srids: list[str]) -> None:
        if not srids:
            return
        placeholders = ",".join(["?"] * len(srids))
        await self._db.execute(
            f"UPDATE own_sales SET notified = 1 WHERE srid IN ({placeholders})",
            srids,
        )

    async def mark_all_sales_notified(self) -> int:
        """Mark every sale/return as notified. Used on startup."""
        await self._db.execute("UPDATE own_sales SET notified = 1 WHERE notified = 0")
        return 0

    # ---------- Finance journal (from /reportDetailByPeriod) ----------

    async def upsert_finance_journal(self, rows: Sequence[dict], fetched_at: str) -> int:
        """Insert rows from financial report. Upsert by rrd_id (unique per row)."""
        if not rows:
            return 0
        async def _tx(conn) -> None:
            payload = []
            for r in rows:
                payload.append((
                    r.get("rrd_id"),
                    r.get("realizationreport_id"),
                    r.get("nm_id"),
                    r.get("sa_name") or r.get("supplier_article"),
                    r.get("subject_name"),
                    r.get("doc_type_name"),
                    r.get("supplier_oper_name"),
                    r.get("order_dt"),
                    r.get("sale_dt"),
                    r.get("rr_dt"),
                    int(r.get("quantity") or 0),
                    float(r.get("retail_amount") or 0),
                    float(r.get("retail_price_withdisc_rub") or 0),
                    float(r.get("ppvz_for_pay") or 0),
                    float(r.get("ppvz_sales_commission") or 0),
                    float(r.get("delivery_rub") or 0),
                    float(r.get("storage_fee") or 0),
                    float(r.get("penalty") or 0),
                    float(r.get("acceptance") or 0),
                    float(r.get("deduction") or 0),
                    float(r.get("additional_payment") or 0),
                    r.get("srid"),
                    fetched_at,
                ))
            await conn.executemany(
                """
                INSERT INTO finance_journal (
                    rrd_id, realizationreport_id, nm_id, supplier_article, subject_name,
                    doc_type_name, supplier_oper_name, order_dt, sale_dt, rr_dt, quantity,
                    retail_amount, retail_price_withdisc_rub, ppvz_for_pay, ppvz_sales_commission,
                    delivery_rub, storage_fee, penalty, acceptance, deduction, additional_payment,
                    srid, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(rrd_id) DO UPDATE SET
                    ppvz_for_pay = excluded.ppvz_for_pay,
                    delivery_rub = excluded.delivery_rub,
                    storage_fee = excluded.storage_fee,
                    penalty = excluded.penalty,
                    fetched_at = excluded.fetched_at
                """,
                payload,
            )
        await self._db.transaction(_tx)
        return len(rows)

    async def get_finance_summary(self, days: int) -> dict:
        """Aggregate finance journal for the last N days by rr_dt."""
        start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        row = await self._db.fetchone(
            """
            SELECT
                SUM(CASE WHEN doc_type_name='Продажа' THEN 1 ELSE 0 END) as sales_count,
                SUM(CASE WHEN doc_type_name='Возврат' THEN 1 ELSE 0 END) as returns_count,
                SUM(CASE WHEN doc_type_name='Продажа' THEN ppvz_for_pay ELSE 0 END) as sales_for_pay,
                SUM(CASE WHEN doc_type_name='Возврат' THEN ppvz_for_pay ELSE 0 END) as returns_for_pay,
                SUM(CASE WHEN doc_type_name='Продажа' THEN retail_amount ELSE 0 END) as sales_retail,
                SUM(CASE WHEN doc_type_name='Возврат' THEN retail_amount ELSE 0 END) as returns_retail,
                SUM(delivery_rub) as total_logistics,
                SUM(storage_fee) as total_storage,
                SUM(penalty) as total_penalty,
                SUM(acceptance) as total_acceptance,
                SUM(deduction) as total_deduction,
                SUM(additional_payment) as total_additional,
                COUNT(*) as rows_count
            FROM finance_journal
            WHERE rr_dt >= ?
            """,
            (start,),
        )
        if not row:
            return {}
        return {k: (row[k] or 0) for k in row.keys()}

    async def count_sales(self) -> int:
        row = await self._db.fetchone("SELECT COUNT(*) as c FROM own_sales")
        return int(row["c"] or 0) if row else 0

    async def count_orders(self) -> int:
        row = await self._db.fetchone("SELECT COUNT(*) as c FROM own_orders")
        return int(row["c"] or 0) if row else 0

    # ---------- Stocks ----------

    async def upsert_stocks(self, stocks: Sequence[StockEntry], updated_at: str) -> None:
        if not stocks:
            return

        async def _tx(conn) -> None:
            payload = [
                (
                    s.nm_id, s.supplier_article, s.warehouse_name,
                    s.quantity, s.in_way_to_client, s.in_way_from_client,
                    s.quantity_full, s.subject, updated_at,
                )
                for s in stocks
            ]
            await conn.executemany(
                """
                INSERT INTO own_stocks (
                    nm_id, supplier_article, warehouse_name, quantity,
                    in_way_to_client, in_way_from_client, quantity_full,
                    subject, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(nm_id, warehouse_name) DO UPDATE SET
                    supplier_article = excluded.supplier_article,
                    quantity = excluded.quantity,
                    in_way_to_client = excluded.in_way_to_client,
                    in_way_from_client = excluded.in_way_from_client,
                    quantity_full = excluded.quantity_full,
                    updated_at = excluded.updated_at
                """,
                payload,
            )
            # Purge records not in this snapshot — товар ушёл со склада.
            # Safety: only если snapshot содержит хотя бы 5 записей
            # (rate-limit/частичный fetch → не 0 или 1-2 случайных)
            if len(stocks) >= 5:
                await conn.execute(
                    "DELETE FROM own_stocks WHERE updated_at < ?",
                    (updated_at,),
                )

        await self._db.transaction(_tx)

    async def get_stock_summary(self) -> list[dict]:
        rows = await self._db.fetchall(
            """
            SELECT nm_id, supplier_article, subject,
                   SUM(quantity) as total_qty,
                   SUM(in_way_to_client) as total_in_way_to,
                   SUM(in_way_from_client) as total_in_way_from,
                   COUNT(*) as warehouse_count,
                   MAX(updated_at) as last_update
            FROM own_stocks
            GROUP BY nm_id, supplier_article
            ORDER BY total_qty DESC
            """
        )
        return [dict(r) for r in rows]

    # ---------- Aggregations ----------

    async def get_daily_metrics(self, date: str) -> DailyMetrics:
        """Get metrics for a specific date (YYYY-MM-DD)."""
        date_prefix = date + "%"

        orders_row = await self._db.fetchone(
            "SELECT COUNT(*) as c, SUM(is_cancel) as canceled FROM own_orders WHERE date LIKE ?",
            (date_prefix,),
        )
        orders_count = int(orders_row["c"] or 0)
        orders_canceled = int(orders_row["canceled"] or 0)

        sales_row = await self._db.fetchone(
            """
            SELECT
                SUM(CASE WHEN is_return = 0 THEN 1 ELSE 0 END) as sales,
                SUM(CASE WHEN is_return = 1 THEN 1 ELSE 0 END) as returns,
                SUM(CASE WHEN is_return = 0 THEN total_price ELSE -total_price END) as revenue,
                SUM(CASE WHEN is_return = 0 THEN for_pay ELSE -for_pay END) as for_pay
            FROM own_sales WHERE date LIKE ?
            """,
            (date_prefix,),
        )
        sales_count = int(sales_row["sales"] or 0)
        returns_count = int(sales_row["returns"] or 0)
        revenue_total = float(sales_row["revenue"] or 0)
        revenue_net = float(sales_row["for_pay"] or 0)

        articles_row = await self._db.fetchone(
            "SELECT COUNT(DISTINCT nm_id) as c FROM own_sales WHERE date LIKE ? AND is_return = 0",
            (date_prefix,),
        )
        unique_articles = int(articles_row["c"] or 0)

        denom = sales_count + returns_count + orders_canceled
        buyout_rate = (sales_count / denom * 100) if denom > 0 else 0.0

        return DailyMetrics(
            date=date,
            orders_count=orders_count,
            orders_canceled=orders_canceled,
            sales_count=sales_count,
            returns_count=returns_count,
            revenue_total=revenue_total,
            revenue_net=revenue_net,
            unique_articles=unique_articles,
            buyout_rate=round(buyout_rate, 1),
        )

    async def get_period_metrics(self, days: int) -> DailyMetrics:
        """Aggregated metrics for last N days."""
        start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

        row = await self._db.fetchone(
            """
            SELECT
                SUM(CASE WHEN is_return = 0 THEN 1 ELSE 0 END) as sales,
                SUM(CASE WHEN is_return = 1 THEN 1 ELSE 0 END) as returns,
                SUM(CASE WHEN is_return = 0 THEN total_price ELSE -total_price END) as revenue,
                SUM(CASE WHEN is_return = 0 THEN for_pay ELSE -for_pay END) as for_pay,
                COUNT(DISTINCT nm_id) as articles
            FROM own_sales WHERE date >= ?
            """,
            (start,),
        )
        orders_row = await self._db.fetchone(
            "SELECT COUNT(*) as c, SUM(is_cancel) as canceled FROM own_orders WHERE date >= ?",
            (start,),
        )

        sales_count = int((row["sales"] if row else None) or 0)
        returns_count = int((row["returns"] if row else None) or 0)
        orders_count = int((orders_row["c"] if orders_row else None) or 0)
        orders_canceled = int((orders_row["canceled"] if orders_row else None) or 0)
        denom = sales_count + returns_count + orders_canceled
        buyout_rate = (sales_count / denom * 100) if denom > 0 else 0.0

        return DailyMetrics(
            date=f"last-{days}d",
            orders_count=orders_count,
            orders_canceled=orders_canceled,
            sales_count=sales_count,
            returns_count=returns_count,
            revenue_total=float((row["revenue"] if row else None) or 0),
            revenue_net=float((row["for_pay"] if row else None) or 0),
            unique_articles=int((row["articles"] if row else None) or 0),
            buyout_rate=round(buyout_rate, 1),
        )

    async def get_sales_velocity(self, days: int = 7) -> float:
        """Average sales per day over last N days (actual buyouts, not orders)."""
        start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        row = await self._db.fetchone(
            "SELECT COUNT(*) as c FROM own_sales WHERE is_return = 0 AND date >= ?",
            (start,),
        )
        sales = int((row["c"] if row else None) or 0)
        return sales / days if days > 0 else 0.0

    async def get_abc_analysis(self, days: int = 30) -> list[dict]:
        """Rank articles by revenue for last N days."""
        start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = await self._db.fetchall(
            """
            SELECT nm_id, supplier_article, subject,
                   COUNT(*) as sale_count,
                   SUM(CASE WHEN is_return = 0 THEN for_pay ELSE -for_pay END) as net_revenue,
                   SUM(CASE WHEN is_return = 1 THEN 1 ELSE 0 END) as returns,
                   AVG(CASE WHEN is_return = 0 THEN for_pay END) as avg_for_pay
            FROM own_sales
            WHERE date >= ?
            GROUP BY nm_id, supplier_article
            ORDER BY net_revenue DESC
            """,
            (start,),
        )
        return [dict(r) for r in rows]

    async def get_returns(self, days: int = 30, limit: int = 20) -> list[dict]:
        start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = await self._db.fetchall(
            """
            SELECT srid, date, nm_id, supplier_article, subject, total_price, for_pay, warehouse_name
            FROM own_sales
            WHERE is_return = 1 AND date >= ?
            ORDER BY date DESC LIMIT ?
            """,
            (start, limit),
        )
        return [dict(r) for r in rows]

    # ---------- Purchases ----------

    async def add_purchase(
        self, nm_id: int | None, supplier_article: str | None,
        quantity: int, buy_price_per_unit: float,
        spp_at_purchase: float | None, notes: str | None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        total = quantity * buy_price_per_unit
        date = now[:10]
        await self._db.execute(
            """
            INSERT INTO purchases (
                date, nm_id, supplier_article, quantity, buy_price_per_unit,
                spp_at_purchase, total_cost, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (date, nm_id, supplier_article, quantity, buy_price_per_unit,
             spp_at_purchase, total, notes, now),
        )
        row = await self._db.fetchone("SELECT last_insert_rowid() as id")
        return int(row["id"]) if row else 0

    async def list_purchases(self, days: int = 30) -> list[dict]:
        start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = await self._db.fetchall(
            "SELECT * FROM purchases WHERE date >= ? ORDER BY date DESC",
            (start,),
        )
        return [dict(r) for r in rows]

    async def total_invested_last(self, days: int) -> float:
        start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        row = await self._db.fetchone(
            "SELECT SUM(total_cost) as s FROM purchases WHERE date >= ?",
            (start,),
        )
        return float((row["s"] if row else None) or 0)

    # ---------- Profit calculations ----------

    async def get_avg_buy_price_per_article(self, days: int | None = None) -> dict[str, float]:
        """
        Weighted average buy price per supplier_article.
        If days is None — across all time.
        """
        if days is not None:
            start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
            rows = await self._db.fetchall(
                """
                SELECT supplier_article, nm_id,
                       SUM(quantity) as total_qty,
                       SUM(total_cost) as total_cost
                FROM purchases
                WHERE date >= ? AND (supplier_article IS NOT NULL OR nm_id IS NOT NULL)
                GROUP BY supplier_article, nm_id
                """,
                (start,),
            )
        else:
            rows = await self._db.fetchall(
                """
                SELECT supplier_article, nm_id,
                       SUM(quantity) as total_qty,
                       SUM(total_cost) as total_cost
                FROM purchases
                WHERE supplier_article IS NOT NULL OR nm_id IS NOT NULL
                GROUP BY supplier_article, nm_id
                """
            )
        avg_prices: dict[str, float] = {}
        for r in rows:
            qty = int(r["total_qty"] or 0)
            cost = float(r["total_cost"] or 0)
            if qty <= 0:
                continue
            avg = cost / qty
            key = str(r["supplier_article"]) if r["supplier_article"] else f"nm:{r['nm_id']}"
            avg_prices[key] = avg
        return avg_prices

    async def get_profit_breakdown(
        self,
        days: int | None = None,
        *,
        tax_percent: float = 2.0,
        logistics_per_unit_rub: float = 182.0,
        acquiring_percent: float = 0.0,
    ) -> list[dict]:
        """
        Per-article profit HYBRID calculation:
        - Для продаж с записью в finance_journal (srid matched):
            revenue = fj.ppvz_for_pay (точно, включая все ppvz_reward/vw_nds/корректировки)
            logistics = fj.delivery_rub (точно, per-srid sum)
            tax_base = fj.retail_price_withdisc_rub (fallback на os.price_with_disc)
        - Для продаж без finance_journal (свежие < 14 дней, не закрыты):
            revenue = os.for_pay (упрощ. оценка, 2.6% погрешность)
            logistics = logistics_per_unit_rub × qty (fallback 182₽)
            tax_base = os.price_with_disc
        - Налог: tax_percent × tax_base
        """
        date_cond = ""
        params: list = []
        if days is not None:
            start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
            date_cond = "AND date >= ?"
            params = [start]

        # Sales aggregated per article — считаем 3 бакета:
        # matched (есть finance_journal), unmatched (нет), return
        sales_rows = await self._db.fetchall(
            f"""
            WITH fj_sum AS (
                SELECT srid,
                       SUM(CASE WHEN supplier_oper_name IN ('Продажа', 'Коррекция продаж', 'Компенсация скидки по программе лояльности', 'Добровольная компенсация при возврате') THEN ppvz_for_pay ELSE 0 END) as fj_pay,
                       SUM(delivery_rub) as fj_delivery,
                       SUM(CASE WHEN supplier_oper_name='Продажа' AND doc_type_name='Продажа' THEN retail_price_withdisc_rub ELSE 0 END) as fj_pwd,
                       SUM(CASE WHEN doc_type_name='Продажа' AND supplier_oper_name='Продажа' THEN 1 ELSE 0 END) as has_sale_row
                FROM finance_journal
                GROUP BY srid
            )
            SELECT os.supplier_article, os.nm_id, os.subject,
                SUM(CASE WHEN os.is_return = 0 THEN 1 ELSE 0 END) as sold_qty,
                SUM(CASE WHEN os.is_return = 1 THEN 1 ELSE 0 END) as returns_qty,
                -- Matched (есть в финотчёте с строкой Продажа+Продажа)
                SUM(CASE WHEN os.is_return = 0 AND fj.has_sale_row > 0 THEN 1 ELSE 0 END) as matched_sold,
                SUM(CASE WHEN os.is_return = 0 AND fj.has_sale_row > 0 THEN fj.fj_pay ELSE 0 END) as matched_fj_pay,
                SUM(CASE WHEN os.is_return = 0 AND fj.has_sale_row > 0 THEN fj.fj_delivery ELSE 0 END) as matched_fj_delivery,
                SUM(CASE WHEN os.is_return = 0 AND fj.has_sale_row > 0 THEN fj.fj_pwd ELSE 0 END) as matched_fj_pwd,
                -- Unmatched (нет закрытой продажи в финотчёте)
                SUM(CASE WHEN os.is_return = 0 AND (fj.has_sale_row IS NULL OR fj.has_sale_row = 0) THEN 1 ELSE 0 END) as unmatched_sold,
                SUM(CASE WHEN os.is_return = 0 AND (fj.has_sale_row IS NULL OR fj.has_sale_row = 0) THEN os.for_pay ELSE 0 END) as unmatched_os_pay,
                SUM(CASE WHEN os.is_return = 0 AND (fj.has_sale_row IS NULL OR fj.has_sale_row = 0) THEN os.price_with_disc ELSE 0 END) as unmatched_os_pwd,
                -- Returns
                SUM(CASE WHEN os.is_return = 1 THEN COALESCE(fj.fj_pay, os.for_pay) ELSE 0 END) as returned_pay,
                SUM(CASE WHEN os.is_return = 1 THEN COALESCE(fj.fj_pwd, os.price_with_disc) ELSE 0 END) as returned_pwd,
                SUM(CASE WHEN os.is_return = 1 THEN COALESCE(fj.fj_delivery, 0) ELSE 0 END) as returned_delivery
            FROM own_sales os
            LEFT JOIN fj_sum fj ON fj.srid = os.srid
            WHERE 1=1 {date_cond.replace('date', 'os.date')}
            GROUP BY os.supplier_article, os.nm_id
            ORDER BY (matched_fj_pay + unmatched_os_pay) DESC
            """,
            params,
        )

        avg_all_time = await self.get_avg_buy_price_per_article(days=None)
        avg_period = await self.get_avg_buy_price_per_article(days=days) if days else avg_all_time

        result: list[dict] = []
        for r in sales_rows:
            sold = int(r["sold_qty"] or 0)
            returns = int(r["returns_qty"] or 0)
            matched_sold = int(r["matched_sold"] or 0)
            unmatched_sold = int(r["unmatched_sold"] or 0)

            # Revenue: finance для matched, own_sales для unmatched
            matched_pay = float(r["matched_fj_pay"] or 0)
            unmatched_pay = float(r["unmatched_os_pay"] or 0)
            returned_pay = float(r["returned_pay"] or 0)
            gross_for_pay = matched_pay + unmatched_pay - abs(returned_pay)

            # Logistics: finance для matched, дефолт для unmatched
            matched_logistics = float(r["matched_fj_delivery"] or 0)
            unmatched_logistics = unmatched_sold * logistics_per_unit_rub
            returned_logistics = float(r["returned_delivery"] or 0)
            logistics = matched_logistics + unmatched_logistics + returned_logistics

            # Tax base: retail_price_withdisc_rub из финотчёта (точно) или os.price_with_disc
            matched_pwd = float(r["matched_fj_pwd"] or 0)
            unmatched_pwd = float(r["unmatched_os_pwd"] or 0)
            returned_pwd = float(r["returned_pwd"] or 0)
            gross_total_net = matched_pwd + unmatched_pwd - abs(returned_pwd)

            tax = gross_total_net * (tax_percent / 100.0)
            acquiring = gross_for_pay * (acquiring_percent / 100.0)
            revenue_net = gross_for_pay - tax - logistics - acquiring

            art = r["supplier_article"]
            nm_id = r["nm_id"]
            key = str(art) if art else f"nm:{nm_id}"

            avg_buy = avg_period.get(key) or avg_all_time.get(key) or 0.0
            cost = sold * avg_buy
            profit = revenue_net - cost
            margin_pct = (profit / gross_for_pay * 100) if gross_for_pay > 0 else 0
            roi_pct = (profit / cost * 100) if cost > 0 else 0

            result.append({
                "supplier_article": art,
                "nm_id": nm_id,
                "subject": r["subject"],
                "sold_qty": sold,
                "returns_qty": returns,
                "matched_sold": matched_sold,
                "unmatched_sold": unmatched_sold,
                "revenue": round(revenue_net, 2),
                "gross_for_pay": round(gross_for_pay, 2),
                "gross_price_with_disc": round(gross_total_net, 2),
                "gross_total_price": round(gross_total_net, 2),
                "tax": round(tax, 2),
                "logistics": round(logistics, 2),
                "acquiring": round(acquiring, 2),
                "avg_buy_price": round(avg_buy, 2),
                "cost": round(cost, 2),
                "profit": round(profit, 2),
                "margin_pct": round(margin_pct, 1),
                "roi_pct": round(roi_pct, 1),
                "has_purchase_data": avg_buy > 0,
            })
        return result

    async def get_total_profit(
        self,
        days: int | None = None,
        *,
        tax_percent: float = 2.0,
        logistics_per_unit_rub: float = 188.0,
        acquiring_percent: float = 0.0,
    ) -> dict:
        """Aggregated profit across all articles after taxes/logistics/acquiring."""
        breakdown = await self.get_profit_breakdown(
            days=days,
            tax_percent=tax_percent,
            logistics_per_unit_rub=logistics_per_unit_rub,
            acquiring_percent=acquiring_percent,
        )
        total_sold = sum(b["sold_qty"] for b in breakdown)
        total_returns = sum(b["returns_qty"] for b in breakdown)
        total_matched = sum(b.get("matched_sold", 0) for b in breakdown)
        total_unmatched = sum(b.get("unmatched_sold", 0) for b in breakdown)
        covered = [b for b in breakdown if b["has_purchase_data"]]
        uncovered = [b for b in breakdown if not b["has_purchase_data"] and b["sold_qty"] > 0]
        total_revenue = sum(b["revenue"] for b in covered)
        total_gross = sum(b["gross_for_pay"] for b in covered)
        total_gross_total_price = sum(b["gross_price_with_disc"] for b in covered)
        total_tax = sum(b["tax"] for b in covered)
        total_logistics = sum(b["logistics"] for b in covered)
        total_acquiring = sum(b["acquiring"] for b in covered)
        total_cost = sum(b["cost"] for b in covered)
        total_profit = total_revenue - total_cost
        uncovered_revenue = sum(b["revenue"] for b in uncovered)
        total_revenue_all = total_revenue + uncovered_revenue
        total_gross_all = total_gross + sum(b["gross_for_pay"] for b in uncovered)

        return {
            "period_days": days,
            "total_sold": total_sold,
            "total_returns": total_returns,
            "matched_sold": total_matched,
            "unmatched_sold": total_unmatched,
            "total_revenue": round(total_revenue, 2),
            "total_revenue_all": round(total_revenue_all, 2),
            "gross_for_pay": round(total_gross_all, 2),
            "gross_total_price": round(total_gross_total_price, 2),
            "uncovered_revenue": round(uncovered_revenue, 2),
            "total_tax": round(total_tax, 2),
            "total_logistics": round(total_logistics, 2),
            "total_acquiring": round(total_acquiring, 2),
            "tax_percent": tax_percent,
            "logistics_per_unit": logistics_per_unit_rub,
            "acquiring_percent": acquiring_percent,
            "total_cost": round(total_cost, 2),
            "total_profit": round(total_profit, 2),
            "margin_pct": round(total_profit / total_gross * 100, 1) if total_gross > 0 else 0,
            "roi_pct": round(total_profit / total_cost * 100, 1) if total_cost > 0 else 0,
            "articles_count": len(breakdown),
            "missing_purchase_data": [b["supplier_article"] or f"nm:{b['nm_id']}" for b in uncovered],
            "breakdown": breakdown,
        }
