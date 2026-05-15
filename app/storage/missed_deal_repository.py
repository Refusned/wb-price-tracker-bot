from __future__ import annotations

from datetime import datetime, timezone

from app.storage.db import Database


MISSED_DEAL_REASONS = {"cash", "too_slow", "bad_margin", "not_interested", "other"}


class MissedDealRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def find_untagged_candidates(
        self,
        limit: int = 15,
        lookback_days: int = 60,
        min_margin_estimate: float = 5.0,
    ) -> list[dict]:
        rows = await self._db.fetchall(
            """
            WITH price_drops AS (
                SELECT
                    ph.nm_id,
                    date(ph.scanned_at) AS candidate_date,
                    ph.scanned_at,
                    ph.price_rub AS observed_price,
                    LAG(ph.price_rub) OVER (
                        PARTITION BY ph.nm_id
                        ORDER BY ph.scanned_at ASC, ph.id ASC
                    ) AS prev_price
                FROM price_history ph
                WHERE date(ph.scanned_at) >= date('now', ?)
            ),
            candidates AS (
                SELECT
                    d.nm_id,
                    d.candidate_date,
                    d.observed_price,
                    d.prev_price,
                    ((d.prev_price - d.observed_price) / d.prev_price) * 100.0 AS drop_pct,
                    20.0 AS observed_margin_estimate
                FROM price_drops d
                WHERE d.prev_price IS NOT NULL
                  AND d.prev_price > 0
                  AND d.observed_price < d.prev_price
                  AND ((d.prev_price - d.observed_price) / d.prev_price) * 100.0 >= 5.0
                  AND 20.0 >= ?
            ),
            -- Codex review fix #10: dedupe candidates by (nm_id, candidate_date).
            -- missed_deal_tags has UNIQUE(nm_id, candidate_date), so showing
            -- multiple drops for the same nm/day means tagging the first silently
            -- no-ops the rest. Pick the LARGEST drop per (nm_id, date).
            best_per_day AS (
                SELECT
                    nm_id,
                    candidate_date,
                    observed_price,
                    prev_price,
                    drop_pct,
                    observed_margin_estimate,
                    ROW_NUMBER() OVER (
                        PARTITION BY nm_id, candidate_date
                        ORDER BY (prev_price - observed_price) DESC, drop_pct DESC
                    ) AS rn
                FROM candidates
            )
            SELECT
                CAST(c.nm_id AS INTEGER) AS nm_id,
                c.candidate_date,
                c.observed_price,
                c.prev_price,
                c.drop_pct,
                c.observed_margin_estimate,
                COALESCE(i.name, 'Без названия') AS name
            FROM best_per_day c
            LEFT JOIN items i ON i.nm_id = c.nm_id
            WHERE c.rn = 1
              AND NOT EXISTS (
                SELECT 1
                FROM purchases p
                WHERE (
                    -- Codex review fix #7 (partial): also match by supplier_article
                    -- for legacy purchases that lack nm_id. The bot's /buy can
                    -- record purchases with NULL nm_id but a valid article that
                    -- corresponds to a specific nm_id via own_sales mapping.
                    p.nm_id = CAST(c.nm_id AS INTEGER)
                    OR (p.nm_id IS NULL AND p.supplier_article IN (
                        SELECT DISTINCT supplier_article FROM own_sales
                        WHERE nm_id = CAST(c.nm_id AS INTEGER)
                          AND supplier_article IS NOT NULL
                    ))
                )
                  AND date(p.date) BETWEEN date(c.candidate_date, '-3 days')
                                      AND date(c.candidate_date, '+3 days')
            )
              AND NOT EXISTS (
                SELECT 1
                FROM missed_deal_tags t
                WHERE t.nm_id = CAST(c.nm_id AS INTEGER)
                  AND t.candidate_date = c.candidate_date
            )
            ORDER BY ABS(c.prev_price - c.observed_price) DESC
            LIMIT ?
            """,
            (f"-{max(lookback_days, 1)} days", float(min_margin_estimate), max(limit, 1)),
        )
        return [
            {
                "nm_id": int(row["nm_id"]),
                "candidate_date": str(row["candidate_date"]),
                "observed_price": float(row["observed_price"]),
                "prev_price": float(row["prev_price"]),
                "drop_pct": float(row["drop_pct"]),
                "observed_margin_estimate": float(row["observed_margin_estimate"]),
                "name": str(row["name"]),
            }
            for row in rows
        ]

    async def tag(
        self,
        nm_id: int,
        candidate_date: str,
        reason: str,
        note: str | None = None,
        observed_price: float | None = None,
        observed_margin_estimate: float | None = None,
    ) -> bool:
        if reason not in MISSED_DEAL_REASONS:
            raise ValueError(f"Unknown missed-deal reason: {reason}")

        inserted = False
        tagged_at = datetime.now(timezone.utc).isoformat()

        async def _tx(conn) -> None:
            nonlocal inserted
            cursor = await conn.execute(
                """
                INSERT OR IGNORE INTO missed_deal_tags (
                    nm_id, candidate_date, observed_price, observed_margin_estimate,
                    reason, note, tagged_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(nm_id),
                    candidate_date,
                    observed_price,
                    observed_margin_estimate,
                    reason,
                    note,
                    tagged_at,
                ),
            )
            inserted = cursor.rowcount == 1
            await cursor.close()

        await self._db.transaction(_tx)
        return inserted

    async def distribution(self) -> dict[str, int]:
        rows = await self._db.fetchall(
            """
            SELECT reason, COUNT(*) AS c
            FROM missed_deal_tags
            GROUP BY reason
            ORDER BY reason ASC
            """
        )
        return {str(row["reason"]): int(row["c"]) for row in rows}

    async def count_tagged(self) -> int:
        row = await self._db.fetchone("SELECT COUNT(*) AS c FROM missed_deal_tags")
        return int(row["c"] or 0) if row is not None else 0
