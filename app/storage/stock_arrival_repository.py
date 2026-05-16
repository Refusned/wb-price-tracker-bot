from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.storage.db import Database


RESOLVED_STATUSES = {"replied", "ignored", "cancelled", "expired"}


class StockArrivalRepository:
    """Tracks stock arrival baselines and pending purchase prompts."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_baselines(self) -> dict[int, dict]:
        """Returns {nm_id: {supplier_article, last_total_full, last_seen_at}}."""
        rows = await self._db.fetchall(
            """
            SELECT nm_id, supplier_article, last_total_full, last_seen_at
            FROM stock_baselines
            """
        )
        return {
            int(row["nm_id"]): {
                "supplier_article": (
                    str(row["supplier_article"])
                    if row["supplier_article"] is not None
                    else None
                ),
                "last_total_full": int(row["last_total_full"]),
                "last_seen_at": str(row["last_seen_at"]),
            }
            for row in rows
        }

    async def upsert_baselines(self, items: list[dict]) -> None:
        """Bulk INSERT OR REPLACE.

        items: [{nm_id, supplier_article, last_total_full, last_seen_at}]
        """
        if not items:
            return

        payload = []
        for item in items:
            nm_id = int(item["nm_id"])
            total = int(item["last_total_full"])
            supplier_article = item.get("supplier_article")
            last_seen_at = str(item["last_seen_at"])

            self._validate_nm_id(nm_id)
            if total < 0:
                raise ValueError("last_total_full must be >= 0")
            if not last_seen_at:
                raise ValueError("last_seen_at must be non-empty")

            payload.append((nm_id, supplier_article, total, last_seen_at))

        async def _tx(conn: Any) -> None:
            await conn.executemany(
                """
                INSERT OR REPLACE INTO stock_baselines (
                    nm_id, supplier_article, last_total_full, last_seen_at
                ) VALUES (?, ?, ?, ?)
                """,
                payload,
            )

        await self._db.transaction(_tx)

    async def create_prompt(
        self,
        *,
        nm_id: int,
        supplier_article: str | None,
        qty_delta: int,
        baseline_total: int,
        current_total: int,
        detected_at: str,
        chat_id: int | None,
    ) -> int | None:
        """INSERT OR IGNORE.

        Returns id of inserted row, or None if it was a duplicate.
        """
        self._validate_nm_id(nm_id)
        if qty_delta <= 0:
            raise ValueError("qty_delta must be > 0")
        if baseline_total < 0:
            raise ValueError("baseline_total must be >= 0")
        if current_total < 0:
            raise ValueError("current_total must be >= 0")
        if current_total < baseline_total:
            raise ValueError("current_total must be >= baseline_total")
        if not detected_at:
            raise ValueError("detected_at must be non-empty")

        inserted_id: int | None = None

        async def _tx(conn: Any) -> None:
            nonlocal inserted_id
            cursor = await conn.execute(
                """
                INSERT OR IGNORE INTO pending_purchase_prompts (
                    nm_id, supplier_article, qty_delta, baseline_total,
                    current_total, detected_at, chat_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    nm_id,
                    supplier_article,
                    qty_delta,
                    baseline_total,
                    current_total,
                    detected_at,
                    chat_id,
                ),
            )
            if cursor.rowcount > 0:
                inserted_id = int(cursor.lastrowid)
            await cursor.close()

        await self._db.transaction(_tx)
        return inserted_id

    async def get_prompt(self, prompt_id: int) -> dict | None:
        """Full row."""
        self._validate_prompt_id(prompt_id)
        row = await self._db.fetchone(
            """
            SELECT *
            FROM pending_purchase_prompts
            WHERE id = ?
            """,
            (prompt_id,),
        )
        return self._row_to_prompt(row) if row is not None else None

    async def get_pending(self, limit: int = 20) -> list[dict]:
        """Status='pending', ORDER BY detected_at DESC."""
        if limit <= 0:
            raise ValueError("limit must be > 0")

        rows = await self._db.fetchall(
            """
            SELECT *
            FROM pending_purchase_prompts
            WHERE status = 'pending'
            ORDER BY detected_at DESC, id DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        return [self._row_to_prompt(row) for row in rows]

    async def resolve(
        self,
        prompt_id: int,
        status: str,
        purchase_id: int | None = None,
        note: str | None = None,
    ) -> None:
        """Update status + resolved_at + purchase_id."""
        self._validate_prompt_id(prompt_id)
        if status not in RESOLVED_STATUSES:
            raise ValueError("status must be one of: replied, ignored, cancelled, expired")
        if purchase_id is not None and purchase_id <= 0:
            raise ValueError("purchase_id must be > 0")

        await self._db.execute(
            """
            UPDATE pending_purchase_prompts
            SET status = ?,
                resolved_at = ?,
                purchase_id = ?,
                note = ?
            WHERE id = ?
            """,
            (
                status,
                datetime.now(timezone.utc).isoformat(),
                purchase_id,
                note,
                prompt_id,
            ),
        )

    async def count_pending(self) -> int:
        row = await self._db.fetchone(
            """
            SELECT COUNT(*) AS c
            FROM pending_purchase_prompts
            WHERE status = 'pending'
            """
        )
        return int(row["c"] or 0) if row is not None else 0

    async def expire_old(self, hours: int = 72) -> int:
        """Mark prompts older than N hours as 'expired'. Returns count."""
        if hours <= 0:
            raise ValueError("hours must be > 0")

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        expired = 0

        async def _tx(conn: Any) -> None:
            nonlocal expired
            cursor = await conn.execute(
                """
                UPDATE pending_purchase_prompts
                SET status = 'expired',
                    resolved_at = ?
                WHERE status = 'pending'
                  AND detected_at < ?
                """,
                (datetime.now(timezone.utc).isoformat(), cutoff),
            )
            expired = int(cursor.rowcount or 0)
            await cursor.close()

        await self._db.transaction(_tx)
        return expired

    @staticmethod
    def _validate_nm_id(nm_id: int) -> None:
        if not isinstance(nm_id, int) or nm_id <= 0:
            raise ValueError("nm_id must be int > 0")

    @staticmethod
    def _validate_prompt_id(prompt_id: int) -> None:
        if not isinstance(prompt_id, int) or prompt_id <= 0:
            raise ValueError("prompt_id must be int > 0")

    @staticmethod
    def _row_to_prompt(row: Any) -> dict:
        return {
            "id": int(row["id"]),
            "nm_id": int(row["nm_id"]),
            "supplier_article": (
                str(row["supplier_article"])
                if row["supplier_article"] is not None
                else None
            ),
            "qty_delta": int(row["qty_delta"]),
            "baseline_total": int(row["baseline_total"]),
            "current_total": int(row["current_total"]),
            "detected_at": str(row["detected_at"]),
            "chat_id": int(row["chat_id"]) if row["chat_id"] is not None else None,
            "status": str(row["status"]),
            "resolved_at": str(row["resolved_at"]) if row["resolved_at"] is not None else None,
            "purchase_id": int(row["purchase_id"]) if row["purchase_id"] is not None else None,
            "note": str(row["note"]) if row["note"] is not None else None,
        }
