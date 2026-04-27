from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

from .db import Database
from .models import Item, PriceDropEvent, item_from_row


class ItemRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def replace_all(self, items: Sequence[Item], updated_at: str) -> None:
        async def _replace(conn) -> None:
            await conn.execute("DELETE FROM items")
            if not items:
                return
            payload = [
                (
                    item.nm_id,
                    item.name,
                    item.price_rub,
                    item.old_price_rub,
                    int(item.in_stock),
                    item.stock_qty,
                    item.url,
                    updated_at,
                )
                for item in items
            ]
            await conn.executemany(
                """
                INSERT INTO items (
                    nm_id, name, price_rub, old_price_rub, in_stock, stock_qty, url, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )

        await self._db.transaction(_replace)

    async def count_items(self) -> int:
        row = await self._db.fetchone("SELECT COUNT(*) AS c FROM items")
        return int(row["c"]) if row else 0

    async def get_top_items(self, min_price_rub: int, limit: int = 10) -> list[Item]:
        rows = await self._db.fetchall(
            """
            SELECT
                nm_id,
                name,
                price_rub,
                old_price_rub,
                in_stock,
                stock_qty,
                url
            FROM items
            WHERE in_stock = 1 AND price_rub >= ?
            ORDER BY price_rub ASC
            LIMIT ?
            """,
            (min_price_rub, limit),
        )
        return [item_from_row(row) for row in rows]


class MetaRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def set_value(self, key: str, value: str) -> None:
        await self._db.execute(
            """
            INSERT INTO meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    async def get_value(self, key: str) -> str | None:
        row = await self._db.fetchone("SELECT value FROM meta WHERE key = ?", (key,))
        return str(row["value"]) if row and row["value"] is not None else None


class SettingsRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def set_value(self, key: str, value: str) -> None:
        await self._db.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    async def get_value(self, key: str) -> str | None:
        row = await self._db.fetchone("SELECT value FROM settings WHERE key = ?", (key,))
        return str(row["value"]) if row and row["value"] is not None else None

    async def get_min_price_rub(self, default: int) -> int:
        raw = await self.get_value("min_price_rub")
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            return default
        return value if value > 0 else default

    async def get_float(self, key: str, default: float) -> float:
        raw = await self.get_value(key)
        if raw is None:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    async def ensure_defaults(self, default_min_price: int) -> None:
        existing = await self.get_value("min_price_rub")
        if existing is None:
            await self.set_value("min_price_rub", str(default_min_price))

    async def ensure_margin_defaults(
        self,
        spp_percent: float,
        wb_commission_percent: float,
        logistics_cost_rub: float,
        storage_cost_per_day_rub: float,
        return_rate_percent: float,
        sell_price_rub: float,
        target_margin_percent: float,
        batch_size: int,
    ) -> None:
        defaults = {
            "spp_percent": str(spp_percent),
            "wb_commission_percent": str(wb_commission_percent),
            "logistics_cost_rub": str(logistics_cost_rub),
            "storage_cost_per_day_rub": str(storage_cost_per_day_rub),
            "return_rate_percent": str(return_rate_percent),
            "sell_price_rub": str(sell_price_rub),
            "target_margin_percent": str(target_margin_percent),
            "batch_size": str(batch_size),
        }
        for key, value in defaults.items():
            existing = await self.get_value(key)
            if existing is None:
                await self.set_value(key, value)


class PriceHistoryRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def record_snapshot(self, items: Sequence[Item], scanned_at: str) -> None:
        if not items:
            return

        async def _insert(conn) -> None:
            payload = [
                (item.nm_id, item.price_rub, item.stock_qty, scanned_at)
                for item in items
                if item.in_stock
            ]
            if payload:
                await conn.executemany(
                    "INSERT INTO price_history (nm_id, price_rub, stock_qty, scanned_at) "
                    "VALUES (?, ?, ?, ?)",
                    payload,
                )

        await self._db.transaction(_insert)

    async def cleanup_old(self, days: int = 14) -> None:
        await self._db.execute(
            "DELETE FROM price_history WHERE scanned_at < datetime('now', ?)",
            (f"-{days} days",),
        )


class TrackedArticleRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_active_nm_ids(self) -> list[str]:
        rows = await self._db.fetchall(
            "SELECT nm_id FROM tracked_articles WHERE is_active = 1 ORDER BY nm_id"
        )
        return [str(row["nm_id"]) for row in rows]

    async def count_active(self) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS c FROM tracked_articles WHERE is_active = 1"
        )
        return int(row["c"]) if row else 0

    async def upsert_articles(self, items: Sequence[Item], seen_at: str) -> int:
        if not items:
            return 0

        nm_ids = [item.nm_id for item in items]
        existing_rows = await self._db.fetchall(
            f"SELECT nm_id FROM tracked_articles WHERE nm_id IN ({','.join(['?'] * len(nm_ids))})",
            nm_ids,
        )
        existing_set = {str(r["nm_id"]) for r in existing_rows}
        added = sum(1 for item in items if item.nm_id not in existing_set)

        payload = [
            (item.nm_id, item.name, seen_at, seen_at, item.name, seen_at)
            for item in items
        ]

        async def _tx(conn) -> None:
            await conn.executemany(
                """
                INSERT INTO tracked_articles (nm_id, name, first_seen_at, last_seen_at, miss_count, is_active)
                VALUES (?, ?, ?, ?, 0, 1)
                ON CONFLICT(nm_id) DO UPDATE SET
                    name = ?,
                    last_seen_at = ?,
                    miss_count = 0,
                    is_active = 1
                """,
                payload,
            )

        await self._db.transaction(_tx)
        return added

    async def add_by_nm_id(self, nm_id: str, name: str, seen_at: str) -> bool:
        existing = await self._db.fetchone(
            "SELECT nm_id FROM tracked_articles WHERE nm_id = ?", (nm_id,)
        )
        if existing:
            await self._db.execute(
                "UPDATE tracked_articles SET is_active = 1, miss_count = 0, last_seen_at = ? WHERE nm_id = ?",
                (seen_at, nm_id),
            )
            return False
        await self._db.execute(
            "INSERT INTO tracked_articles (nm_id, name, first_seen_at, last_seen_at, miss_count, is_active) "
            "VALUES (?, ?, ?, ?, 0, 1)",
            (nm_id, name, seen_at, seen_at),
        )
        return True

    async def remove_by_nm_id(self, nm_id: str) -> bool:
        existing = await self._db.fetchone(
            "SELECT nm_id FROM tracked_articles WHERE nm_id = ?", (nm_id,)
        )
        if not existing:
            return False
        await self._db.execute(
            "UPDATE tracked_articles SET is_active = 0 WHERE nm_id = ?", (nm_id,)
        )
        return True

    async def increment_misses(self, missing_nm_ids: set[str], deactivate_threshold: int = 3) -> None:
        if not missing_nm_ids:
            return
        ids = list(missing_nm_ids)
        placeholders = ",".join(["?"] * len(ids))

        async def _tx(conn) -> None:
            # Single query: increment + deactivate if threshold reached
            await conn.execute(
                f"""
                UPDATE tracked_articles
                SET miss_count = miss_count + 1,
                    is_active = CASE WHEN miss_count + 1 >= ? THEN 0 ELSE is_active END
                WHERE nm_id IN ({placeholders})
                """,
                [deactivate_threshold] + ids,
            )

        await self._db.transaction(_tx)

    async def get_active_list(self, limit: int = 50) -> list[tuple[str, str, str]]:
        rows = await self._db.fetchall(
            "SELECT nm_id, name, last_seen_at FROM tracked_articles "
            "WHERE is_active = 1 ORDER BY last_seen_at DESC LIMIT ?",
            (limit,),
        )
        return [(str(r["nm_id"]), str(r["name"]), str(r["last_seen_at"])) for r in rows]

    async def deactivate_all(self) -> int:
        count = await self.count_active()
        await self._db.execute("UPDATE tracked_articles SET is_active = 0")
        return count

    async def seed_from_items(self, seen_at: str) -> int:
        count = await self.count_active()
        if count > 0:
            return 0
        rows = await self._db.fetchall(
            "SELECT nm_id, name FROM items WHERE in_stock = 1"
        )
        if not rows:
            return 0

        payload = [(str(r["nm_id"]), str(r["name"]), seen_at, seen_at) for r in rows]

        async def _tx(conn) -> None:
            await conn.executemany(
                """
                INSERT OR IGNORE INTO tracked_articles
                (nm_id, name, first_seen_at, last_seen_at, miss_count, is_active)
                VALUES (?, ?, ?, ?, 0, 1)
                """,
                payload,
            )

        await self._db.transaction(_tx)
        return len(payload)


class SubscriberRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def upsert_active(self, chat_id: int, user_id: int | None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """
            INSERT INTO subscribers (chat_id, user_id, is_active, created_at, updated_at)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                user_id = excluded.user_id,
                is_active = 1,
                updated_at = excluded.updated_at
            """,
            (chat_id, user_id, now, now),
        )

    async def set_active(self, chat_id: int, is_active: bool) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """
            INSERT INTO subscribers (chat_id, user_id, is_active, created_at, updated_at)
            VALUES (?, NULL, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                is_active = excluded.is_active,
                updated_at = excluded.updated_at
            """,
            (chat_id, int(is_active), now, now),
        )

    async def count_active(self) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS c FROM subscribers WHERE is_active = 1"
        )
        return int(row["c"]) if row else 0

    async def list_active_chat_ids(self) -> list[int]:
        rows = await self._db.fetchall(
            "SELECT chat_id FROM subscribers WHERE is_active = 1 ORDER BY chat_id ASC"
        )
        return [int(row["chat_id"]) for row in rows]


class PriceStatsRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def record_snapshot_and_collect_drops(
        self,
        items: Sequence[Item],
        *,
        alert_nm_ids: set[str],
        drop_threshold_percent: float,
        observed_at: str,
        rank_map: dict[str, int] | None = None,
        max_events: int = 3,
        alert_cooldown_minutes: int = 30,
        stale_data_hours: int = 1,
    ) -> list[PriceDropEvent]:
        """
        Detect price drops by comparing current price against the PREVIOUS SCAN price,
        not the all-time minimum. Only alert for items in alert_nm_ids set.
        Cooldown is time-based (no repeat alert for same item within N minutes).
        Skip alert if last_seen_at is older than stale_data_hours (prevents false positives
        after an item was absent for a long time).
        """
        from datetime import datetime

        threshold_factor = max(0.0, float(drop_threshold_percent)) / 100.0
        threshold_multiplier = 1.0 - threshold_factor
        rank_map = rank_map or {}

        now = datetime.fromisoformat(observed_at)
        cooldown_seconds = alert_cooldown_minutes * 60
        stale_seconds = stale_data_hours * 3600

        captured_events: list[PriceDropEvent] = []

        async def _tx(conn) -> None:
            nonlocal captured_events
            events: list[PriceDropEvent] = []

            for item in items:
                if not item.in_stock:
                    continue

                cursor = await conn.execute(
                    """
                    SELECT min_price_rub, last_seen_price_rub, last_seen_at, last_alert_at
                    FROM item_price_stats
                    WHERE nm_id = ?
                    """,
                    (item.nm_id,),
                )
                row = await cursor.fetchone()
                await cursor.close()

                if row is None:
                    # First time seeing this item — record baseline, don't alert
                    await conn.execute(
                        """
                        INSERT INTO item_price_stats (
                            nm_id, min_price_rub, last_seen_price_rub, last_seen_at,
                            last_alert_price_rub, last_alert_at
                        ) VALUES (?, ?, ?, ?, NULL, NULL)
                        """,
                        (item.nm_id, item.price_rub, item.price_rub, observed_at),
                    )
                    continue

                previous_min = float(row["min_price_rub"])
                previous_price = float(row["last_seen_price_rub"])
                last_seen_at = row["last_seen_at"]
                last_alert_at = row["last_alert_at"]

                # Check data freshness
                data_is_fresh = True
                if last_seen_at:
                    try:
                        seen_dt = datetime.fromisoformat(str(last_seen_at))
                        if (now - seen_dt).total_seconds() > stale_seconds:
                            data_is_fresh = False
                    except ValueError:
                        data_is_fresh = True  # fail open

                # Check cooldown
                cooldown_ok = True
                if last_alert_at:
                    try:
                        alert_dt = datetime.fromisoformat(str(last_alert_at))
                        if (now - alert_dt).total_seconds() < cooldown_seconds:
                            cooldown_ok = False
                    except ValueError:
                        cooldown_ok = True

                should_alert = (
                    previous_price > 0
                    and item.price_rub <= previous_price * threshold_multiplier
                )
                in_alert_pool = item.nm_id in alert_nm_ids

                next_alert_at: str | None = None
                next_alert_price: float | None = None

                if should_alert and in_alert_pool and cooldown_ok and data_is_fresh:
                    drop_percent = (1.0 - (item.price_rub / previous_price)) * 100.0
                    events.append(
                        PriceDropEvent(
                            nm_id=item.nm_id,
                            name=item.name,
                            url=item.url,
                            previous_price_rub=previous_price,
                            new_price_rub=item.price_rub,
                            drop_percent=round(drop_percent, 2),
                            stock_qty=item.stock_qty,
                            top_rank=rank_map.get(item.nm_id),
                        )
                    )
                    next_alert_at = observed_at
                    next_alert_price = item.price_rub

                # Always update last_seen — this is the key change
                await conn.execute(
                    """
                    UPDATE item_price_stats
                    SET min_price_rub = ?,
                        last_seen_price_rub = ?,
                        last_seen_at = ?,
                        last_alert_price_rub = COALESCE(?, last_alert_price_rub),
                        last_alert_at = COALESCE(?, last_alert_at)
                    WHERE nm_id = ?
                    """,
                    (
                        min(previous_min, item.price_rub),
                        item.price_rub,
                        observed_at,
                        next_alert_price,
                        next_alert_at,
                        item.nm_id,
                    ),
                )

            events.sort(key=lambda event: event.drop_percent, reverse=True)
            if max_events > 0:
                events = events[:max_events]
            captured_events = events

        await self._db.transaction(_tx)
        return captured_events
