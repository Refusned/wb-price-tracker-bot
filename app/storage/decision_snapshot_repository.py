from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.storage.db import Database


DECISION_ACTIONS = {"bought", "ignored", "too_late"}


class DecisionSnapshotRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(
        self,
        *,
        nm_id: int,
        observed_price: float,
        observed_margin_estimate: float | None,
        alert_sent: bool,
        source: str = "scheduler",
        snapshot_at: str | None = None,
        capital_available_estimate: float | None = None,
    ) -> int:
        self._validate_nm_id(nm_id)
        self._validate_price(observed_price)

        observed_at = snapshot_at or datetime.now(timezone.utc).isoformat()
        inserted_id = 0

        async def _tx(conn) -> None:
            nonlocal inserted_id
            cursor = await conn.execute(
                """
                INSERT INTO decision_snapshots (
                    snapshot_at, nm_id, observed_price, observed_margin_estimate,
                    capital_available_estimate, alert_sent, user_action, source
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    observed_at,
                    int(nm_id),
                    float(observed_price),
                    0.0 if observed_margin_estimate is None else float(observed_margin_estimate),
                    capital_available_estimate,
                    int(alert_sent),
                    source,
                ),
            )
            inserted_id = int(cursor.lastrowid or 0)
            await cursor.close()

        await self._db.transaction(_tx)
        return inserted_id

    async def update_user_action(self, snapshot_id: int, action: str) -> None:
        if action not in DECISION_ACTIONS:
            raise ValueError("action must be one of: bought, ignored, too_late")

        await self._db.execute(
            """
            UPDATE decision_snapshots
            SET user_action = ?
            WHERE id = ?
            """,
            (action, int(snapshot_id)),
        )

    async def find_recent_for_nm(self, nm_id: int, within_seconds: int = 3600) -> list[dict]:
        self._validate_nm_id(nm_id)
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max(within_seconds, 1))).isoformat()
        rows = await self._db.fetchall(
            """
            SELECT *
            FROM decision_snapshots
            WHERE nm_id = ?
              AND snapshot_at >= ?
            ORDER BY snapshot_at DESC, id DESC
            """,
            (int(nm_id), cutoff),
        )
        return [self._row_to_dict(row) for row in rows]

    async def recent(self, limit: int = 20, only_alerted: bool = False) -> list[dict]:
        safe_limit = max(1, int(limit))
        alert_clause = "WHERE alert_sent = ?" if only_alerted else ""
        params: list[object] = []
        if only_alerted:
            params.append(1)
        params.append(safe_limit)

        rows = await self._db.fetchall(
            f"""
            SELECT *
            FROM decision_snapshots
            {alert_clause}
            ORDER BY snapshot_at DESC, id DESC
            LIMIT ?
            """,
            params,
        )
        return [self._row_to_dict(row) for row in rows]

    async def distribution(self, days: int = 30) -> dict:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(days, 1))).isoformat()

        totals_row = await self._db.fetchone(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN alert_sent = 1 THEN 1 ELSE 0 END) AS alerted
            FROM decision_snapshots
            WHERE snapshot_at >= ?
            """,
            (cutoff,),
        )

        action_rows = await self._db.fetchall(
            """
            SELECT user_action, COUNT(*) AS c
            FROM decision_snapshots
            WHERE snapshot_at >= ?
            GROUP BY user_action
            """,
            (cutoff,),
        )

        nm_rows = await self._db.fetchall(
            """
            SELECT nm_id, COUNT(*) AS c
            FROM decision_snapshots
            WHERE snapshot_at >= ?
            GROUP BY nm_id
            ORDER BY c DESC, nm_id ASC
            LIMIT 10
            """,
            (cutoff,),
        )

        by_action = {"bought": 0, "ignored": 0, "too_late": 0, None: 0}
        for row in action_rows:
            action = row["user_action"]
            key = str(action) if action is not None else None
            by_action[key] = int(row["c"])

        return {
            "total": int(totals_row["total"] or 0) if totals_row else 0,
            "alerted": int(totals_row["alerted"] or 0) if totals_row else 0,
            "by_action": by_action,
            "by_nm_id_top10": [
                {"nm_id": int(row["nm_id"]), "count": int(row["c"])}
                for row in nm_rows
            ],
        }

    async def count(self) -> int:
        row = await self._db.fetchone("SELECT COUNT(*) AS c FROM decision_snapshots")
        return int(row["c"] or 0) if row is not None else 0

    @staticmethod
    def _validate_nm_id(nm_id: int) -> None:
        if not isinstance(nm_id, int) or nm_id <= 0:
            raise ValueError("nm_id must be int > 0")

    @staticmethod
    def _validate_price(observed_price: float) -> None:
        if observed_price < 0:
            raise ValueError("observed_price must be >= 0")

    @staticmethod
    def _row_to_dict(row) -> dict:
        return {
            "id": int(row["id"]),
            "snapshot_at": str(row["snapshot_at"]),
            "nm_id": int(row["nm_id"]),
            "observed_price": float(row["observed_price"]),
            "observed_margin_estimate": (
                float(row["observed_margin_estimate"])
                if row["observed_margin_estimate"] is not None
                else None
            ),
            "capital_available_estimate": (
                float(row["capital_available_estimate"])
                if row["capital_available_estimate"] is not None
                else None
            ),
            "alert_sent": int(row["alert_sent"]),
            "user_action": str(row["user_action"]) if row["user_action"] is not None else None,
            "source": str(row["source"]),
        }
