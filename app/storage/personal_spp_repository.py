from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.storage.db import Database


class PersonalSppRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def log_snapshot(
        self,
        spp_percent: float,
        category: str = "default",
        source: str = "manual",
        snapshot_at: str | None = None,
    ) -> int:
        self._validate_spp_percent(spp_percent)
        observed_at = snapshot_at or datetime.now(timezone.utc).isoformat()
        inserted_id = 0

        async def _tx(conn) -> None:
            nonlocal inserted_id
            cursor = await conn.execute(
                """
                INSERT INTO personal_spp_snapshots (
                    snapshot_at, category, spp_percent, source
                ) VALUES (?, ?, ?, ?)
                """,
                (observed_at, category, float(spp_percent), source),
            )
            inserted_id = int(cursor.lastrowid or 0)
            await cursor.close()

        await self._db.transaction(_tx)
        return inserted_id

    async def latest(self, category: str = "default") -> dict | None:
        row = await self._db.fetchone(
            """
            SELECT snapshot_at, spp_percent, source
            FROM personal_spp_snapshots
            WHERE category = ?
            ORDER BY snapshot_at DESC, id DESC
            LIMIT 1
            """,
            (category,),
        )
        if row is None:
            return None
        return {
            "snapshot_at": str(row["snapshot_at"]),
            "spp_percent": float(row["spp_percent"]),
            "source": str(row["source"]),
        }

    async def history(self, days: int = 30, category: str | None = None) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(days, 1))).isoformat()
        params: list[object] = [cutoff]
        category_clause = ""
        if category is not None:
            category_clause = "AND category = ?"
            params.append(category)

        rows = await self._db.fetchall(
            f"""
            SELECT snapshot_at, category, spp_percent, source
            FROM personal_spp_snapshots
            WHERE snapshot_at >= ?
              {category_clause}
            ORDER BY snapshot_at DESC, id DESC
            """,
            params,
        )
        return [
            {
                "snapshot_at": str(row["snapshot_at"]),
                "category": str(row["category"] or "default"),
                "spp_percent": float(row["spp_percent"]),
                "source": str(row["source"]),
            }
            for row in rows
        ]

    async def trend(self, category: str = "default", window_days: int = 7) -> dict | None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(window_days, 1))).isoformat()
        rows = await self._db.fetchall(
            """
            SELECT snapshot_at, spp_percent
            FROM personal_spp_snapshots
            WHERE category = ?
              AND snapshot_at >= ?
            ORDER BY snapshot_at DESC, id DESC
            """,
            (category, cutoff),
        )
        if not rows:
            return None

        values = [float(row["spp_percent"]) for row in rows]
        current = values[0]
        mean = sum(values) / len(values)
        drop_pct_vs_window = ((mean - current) / mean * 100.0) if mean > 0 else 0.0

        return {
            "current": current,
            "mean": mean,
            "min": min(values),
            "max": max(values),
            "drop_pct_vs_window": drop_pct_vs_window,
        }

    @staticmethod
    def _validate_spp_percent(value: float) -> None:
        if value < 0.0 or value > 100.0:
            raise ValueError("spp_percent must be between 0 and 100")
