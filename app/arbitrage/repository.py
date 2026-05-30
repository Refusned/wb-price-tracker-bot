"""CRUD for arb_queries, arb_candidates, arb_buyer_spp_observations."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.storage.db import Database


_SPP_CONFIDENCE = {"high", "medium", "low"}
_SPP_SOURCES = {"cookie", "observation", "category_avg", "manual", "default", "checkout_manual", "purchase"}

# Этап 1: owner ground-truth labels on an nm_id. Used to measure how often
# the cohort produced wrong-product/wrong-color alerts (the deterministic
# filter's precision) BEFORE any LLM is considered.
NM_LABELS = {"bought", "wrong_color", "wrong_product", "no_cash", "bad_margin"}


class ArbitrageRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ── arb_queries ─────────────────────────────────────────────
    async def add_query(self, query: str, *, subject_id: int | None = None,
                        subject_name: str | None = None) -> int:
        cleaned = query.strip()
        if not cleaned:
            raise ValueError("query cannot be empty")
        now = datetime.now(timezone.utc).isoformat()
        inserted_id = 0

        async def _tx(conn) -> None:
            nonlocal inserted_id
            cursor = await conn.execute(
                """
                INSERT INTO arb_queries (query, enabled, subject_id, subject_name, created_at)
                VALUES (?, 1, ?, ?, ?)
                ON CONFLICT(query) DO UPDATE SET
                    enabled = 1,
                    subject_id = COALESCE(excluded.subject_id, arb_queries.subject_id),
                    subject_name = COALESCE(excluded.subject_name, arb_queries.subject_name)
                """,
                (cleaned, subject_id, subject_name, now),
            )
            await cursor.close()
            cursor = await conn.execute(
                "SELECT id FROM arb_queries WHERE query = ?", (cleaned,)
            )
            row = await cursor.fetchone()
            inserted_id = int(row["id"]) if row else 0
            await cursor.close()

        await self._db.transaction(_tx)
        return inserted_id

    async def remove_query(self, ident: str) -> bool:
        """Soft-disable by id (digit) or by exact query text."""
        async def _tx(conn) -> None:
            if ident.isdigit():
                await conn.execute("UPDATE arb_queries SET enabled = 0 WHERE id = ?", (int(ident),))
            else:
                await conn.execute("UPDATE arb_queries SET enabled = 0 WHERE query = ?", (ident.strip(),))
        await self._db.transaction(_tx)
        return True

    async def list_queries(self, *, only_enabled: bool = True) -> list[dict[str, Any]]:
        where = "WHERE enabled = 1" if only_enabled else ""
        rows = await self._db.fetchall(
            f"""
            SELECT id, query, enabled, subject_id, subject_name,
                   created_at, last_scanned_at, last_found_count,
                   include_keywords, exclude_keywords
            FROM arb_queries {where}
            ORDER BY id ASC
            """
        )
        return [
            {
                "id": int(r["id"]),
                "query": str(r["query"]),
                "enabled": bool(r["enabled"]),
                "subject_id": int(r["subject_id"]) if r["subject_id"] is not None else None,
                "subject_name": str(r["subject_name"]) if r["subject_name"] else None,
                "created_at": str(r["created_at"]),
                "last_scanned_at": str(r["last_scanned_at"]) if r["last_scanned_at"] else None,
                "last_found_count": int(r["last_found_count"] or 0),
                "include_keywords": str(r["include_keywords"]) if r["include_keywords"] else None,
                "exclude_keywords": str(r["exclude_keywords"]) if r["exclude_keywords"] else None,
            }
            for r in rows
        ]

    async def set_query_keywords(
        self, query_id: int, *, include: str | None, exclude: str | None,
    ) -> None:
        """Set per-query color/variant keyword filter (CSV). Empty → NULL.

        Per-query (not global): a Станция query filters by colour, a
        robot-vacuum query stays unfiltered. Applied by the scanner to the
        cohort BEFORE price metrics.
        """
        inc = (include or "").strip() or None
        exc = (exclude or "").strip() or None
        await self._db.execute(
            "UPDATE arb_queries SET include_keywords = ?, exclude_keywords = ? WHERE id = ?",
            (inc, exc, int(query_id)),
        )

    async def mark_scanned(self, query_id: int, found_count: int) -> None:
        await self._db.execute(
            """
            UPDATE arb_queries
            SET last_scanned_at = ?, last_found_count = ?
            WHERE id = ?
            """,
            (datetime.now(timezone.utc).isoformat(), int(found_count), int(query_id)),
        )

    async def update_query_subject(
        self, query_id: int, *, subject_id: int | None, subject_name: str | None,
    ) -> None:
        """Persist dominant subject discovered by scanner. Lets /arb_list show
        which WB subject the query maps to + auto-link observations.
        """
        await self._db.execute(
            """
            UPDATE arb_queries
            SET subject_id = COALESCE(?, subject_id),
                subject_name = COALESCE(?, subject_name)
            WHERE id = ?
            """,
            (subject_id, subject_name, int(query_id)),
        )

    # ── arb_candidates ──────────────────────────────────────────
    async def record_candidate(self, **fields: Any) -> int:
        """Insert a candidate row. Returns new id.

        Expected fields:
            nm_id, query, subject_id, name, brand,
            market_price_rub, market_median_rub, market_p25_rub, market_min_rub,
            buyer_price_rub, spp_percent_used, spp_source, spp_confidence,
            listed_price_rub, commission_pct, commission_rub, logistics_rub,
            acquiring_rub, return_reserve_rub, tax_rub, holding_rub,
            revenue_after_wb_rub, margin_rub, margin_percent, profit_per_ruble_day_pct,
            expected_hold_days, cohort_size, url
        """
        now = datetime.now(timezone.utc).isoformat()
        cols = (
            "nm_id, query, subject_id, name, brand, "
            "market_price_rub, market_median_rub, market_p25_rub, market_min_rub, "
            "buyer_price_rub, spp_percent_used, spp_source, spp_confidence, "
            "listed_price_rub, commission_pct, commission_rub, logistics_rub, "
            "acquiring_rub, return_reserve_rub, tax_rub, holding_rub, "
            "revenue_after_wb_rub, margin_rub, margin_percent, profit_per_ruble_day_pct, "
            "expected_hold_days, cohort_size, found_at, status, url"
        )
        placeholders = ", ".join(["?"] * 30)
        values = (
            int(fields["nm_id"]),
            str(fields["query"]),
            fields.get("subject_id"),
            fields.get("name"),
            fields.get("brand"),
            int(fields["market_price_rub"]),
            fields.get("market_median_rub"),
            fields.get("market_p25_rub"),
            fields.get("market_min_rub"),
            int(fields["buyer_price_rub"]),
            float(fields["spp_percent_used"]),
            str(fields["spp_source"]),
            str(fields["spp_confidence"]),
            int(fields["listed_price_rub"]),
            fields.get("commission_pct"),
            fields.get("commission_rub"),
            fields.get("logistics_rub"),
            fields.get("acquiring_rub"),
            fields.get("return_reserve_rub"),
            fields.get("tax_rub"),
            fields.get("holding_rub"),
            int(fields["revenue_after_wb_rub"]),
            int(fields["margin_rub"]),
            float(fields["margin_percent"]),
            float(fields["profit_per_ruble_day_pct"]),
            int(fields["expected_hold_days"]),
            fields.get("cohort_size"),
            now,
            fields.get("status", "open"),
            fields.get("url"),
        )
        inserted_id = 0

        async def _tx(conn) -> None:
            nonlocal inserted_id
            cursor = await conn.execute(
                f"INSERT INTO arb_candidates ({cols}) VALUES ({placeholders})", values
            )
            inserted_id = int(cursor.lastrowid or 0)
            await cursor.close()

        await self._db.transaction(_tx)
        return inserted_id

    async def mark_alerted(self, candidate_id: int) -> None:
        await self._db.execute(
            "UPDATE arb_candidates SET alerted_at = ?, status = 'alerted' WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), int(candidate_id)),
        )

    async def recently_alerted(self, nm_id: int, hours: int = 6) -> bool:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(hours, 1))).isoformat()
        row = await self._db.fetchone(
            """
            SELECT 1 AS x
            FROM arb_candidates
            WHERE nm_id = ? AND alerted_at IS NOT NULL AND alerted_at >= ?
            LIMIT 1
            """,
            (int(nm_id), cutoff),
        )
        return row is not None

    async def alerts_today_count(self) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        row = await self._db.fetchone(
            """
            SELECT COUNT(*) AS c
            FROM arb_candidates
            WHERE alerted_at IS NOT NULL AND alerted_at >= ?
            """,
            (cutoff,),
        )
        return int(row["c"] or 0) if row else 0

    async def recent_candidates(self, *, hours: int = 24, limit: int = 20) -> list[dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(hours, 1))).isoformat()
        rows = await self._db.fetchall(
            """
            SELECT *
            FROM arb_candidates
            WHERE found_at >= ?
            ORDER BY profit_per_ruble_day_pct DESC, margin_rub DESC
            LIMIT ?
            """,
            (cutoff, int(limit)),
        )
        return [dict(r) for r in rows]

    # ── arb_buyer_spp_observations ──────────────────────────────
    async def record_spp_observation(
        self,
        *,
        nm_id: int,
        subject_id: int | None,
        subject_name: str | None,
        public_price_rub: int,
        my_buyer_price_rub: int,
        source: str,
        confidence: str = "medium",
        sample_count: int = 1,
        cookie_age_minutes: int | None = None,
        note: str | None = None,
    ) -> int:
        if source not in _SPP_SOURCES:
            raise ValueError(f"source must be one of {_SPP_SOURCES}")
        if confidence not in _SPP_CONFIDENCE:
            raise ValueError(f"confidence must be one of {_SPP_CONFIDENCE}")
        if public_price_rub <= 0:
            raise ValueError("public_price_rub must be > 0")
        if my_buyer_price_rub < 0 or my_buyer_price_rub > public_price_rub:
            raise ValueError("my_buyer_price_rub must be in [0, public_price_rub]")

        spp_pct = (1 - my_buyer_price_rub / public_price_rub) * 100.0
        now = datetime.now(timezone.utc).isoformat()
        inserted_id = 0

        async def _tx(conn) -> None:
            nonlocal inserted_id
            cursor = await conn.execute(
                """
                INSERT INTO arb_buyer_spp_observations (
                    nm_id, subject_id, subject_name,
                    public_price_rub, my_buyer_price_rub, spp_percent_observed,
                    source, confidence, sample_count, cookie_age_minutes, observed_at, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(nm_id), subject_id, subject_name,
                    int(public_price_rub), int(my_buyer_price_rub), spp_pct,
                    source, confidence, int(sample_count), cookie_age_minutes, now, note,
                ),
            )
            inserted_id = int(cursor.lastrowid or 0)
            await cursor.close()

        await self._db.transaction(_tx)
        return inserted_id

    async def get_category_avg_spp(
        self, subject_id: int, *, days: int = 30, min_samples: int = 3,
    ) -> dict[str, Any] | None:
        """Round 4: AVG observed buyer-side СПП per category (D27, D35).

        Returns None if fewer than ``min_samples`` observations exist in window.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(days, 1))).isoformat()
        row = await self._db.fetchone(
            """
            SELECT
                AVG(spp_percent_observed) AS avg_spp,
                COUNT(*) AS samples,
                MAX(observed_at) AS last_observed,
                MIN(spp_percent_observed) AS min_spp,
                MAX(spp_percent_observed) AS max_spp
            FROM arb_buyer_spp_observations
            WHERE subject_id = ? AND observed_at >= ?
            """,
            (int(subject_id), cutoff),
        )
        if not row or (row["samples"] or 0) < min_samples:
            return None
        return {
            "subject_id": int(subject_id),
            "avg_spp": float(row["avg_spp"] or 0.0),
            "samples": int(row["samples"]),
            "last_observed": str(row["last_observed"]) if row["last_observed"] else None,
            "min_spp": float(row["min_spp"] or 0.0),
            "max_spp": float(row["max_spp"] or 0.0),
        }

    async def get_nm_recent_spp(self, nm_id: int, *, hours: int = 24) -> dict[str, Any] | None:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(hours, 1))).isoformat()
        row = await self._db.fetchone(
            """
            SELECT spp_percent_observed, source, confidence, observed_at, sample_count
            FROM arb_buyer_spp_observations
            WHERE nm_id = ? AND observed_at >= ?
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            (int(nm_id), cutoff),
        )
        if not row:
            return None
        return {
            "spp_percent": float(row["spp_percent_observed"]),
            "source": str(row["source"]),
            "confidence": str(row["confidence"]),
            "observed_at": str(row["observed_at"]),
            "sample_count": int(row["sample_count"] or 1),
        }

    async def top_categories_by_spp(self, *, days: int = 30, min_samples: int = 3,
                                      limit: int = 10) -> list[dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(days, 1))).isoformat()
        rows = await self._db.fetchall(
            """
            SELECT subject_id, subject_name,
                   AVG(spp_percent_observed) AS avg_spp,
                   COUNT(*) AS samples,
                   MAX(observed_at) AS last_observed
            FROM arb_buyer_spp_observations
            WHERE observed_at >= ? AND subject_id IS NOT NULL
            GROUP BY subject_id, subject_name
            HAVING COUNT(*) >= ?
            ORDER BY avg_spp DESC
            LIMIT ?
            """,
            (cutoff, int(min_samples), int(limit)),
        )
        return [
            {
                "subject_id": int(r["subject_id"]),
                "subject_name": str(r["subject_name"]) if r["subject_name"] else "?",
                "avg_spp": float(r["avg_spp"]),
                "samples": int(r["samples"]),
                "last_observed": str(r["last_observed"]) if r["last_observed"] else None,
            }
            for r in rows
        ]

    # ── arb_nm_labels (Этап 1 ground-truth) ─────────────────────
    async def add_nm_label(
        self, nm_id: int, label: str, *, note: str | None = None,
    ) -> int:
        if label not in NM_LABELS:
            raise ValueError(f"label must be one of {sorted(NM_LABELS)}")
        now = datetime.now(timezone.utc).isoformat()
        inserted_id = 0

        async def _tx(conn) -> None:
            nonlocal inserted_id
            cursor = await conn.execute(
                "INSERT INTO arb_nm_labels (nm_id, label, note, created_at) "
                "VALUES (?, ?, ?, ?)",
                (int(nm_id), label, note, now),
            )
            inserted_id = int(cursor.lastrowid or 0)
            await cursor.close()

        await self._db.transaction(_tx)
        return inserted_id

    async def label_counts(self, *, days: int = 30) -> dict[str, int]:
        """Counts per label over the window (for the Этап 1 measurement)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(days, 1))).isoformat()
        rows = await self._db.fetchall(
            """
            SELECT label, COUNT(*) AS c
            FROM arb_nm_labels
            WHERE created_at >= ?
            GROUP BY label
            """,
            (cutoff,),
        )
        return {str(r["label"]): int(r["c"]) for r in rows}

    # ──────────────────────── retention ────────────────────────
    async def count_candidates(self) -> int:
        row = await self._db.fetchone("SELECT COUNT(*) AS c FROM arb_candidates")
        return int(row["c"]) if row else 0

    async def cleanup_candidates(self, retention_days: int) -> int:
        """Удалить arb_candidates старше N дней. Возвращает кол-во удалённых.

        arb_candidates пишется на КАЖДОМ скане (без upsert), поэтому без
        ретеншена БД растёт безгранично. Вызывается из scan_once.
        """
        if retention_days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS c FROM arb_candidates WHERE found_at < ?", (cutoff,)
        )
        n = int(row["c"]) if row else 0
        if n:
            await self._db.execute(
                "DELETE FROM arb_candidates WHERE found_at < ?", (cutoff,)
            )
        return n

    async def cleanup_observations(self, retention_days: int) -> int:
        """Удалить наблюдения СПП старше N дней. Возвращает кол-во удалённых."""
        if retention_days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS c FROM arb_buyer_spp_observations WHERE observed_at < ?",
            (cutoff,),
        )
        n = int(row["c"]) if row else 0
        if n:
            await self._db.execute(
                "DELETE FROM arb_buyer_spp_observations WHERE observed_at < ?", (cutoff,)
            )
        return n
