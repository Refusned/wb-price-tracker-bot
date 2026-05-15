from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
if ROOT.as_posix() not in sys.path:
    sys.path.insert(0, ROOT.as_posix())

from app.storage.db import Database  # noqa: E402


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * percentile / 100.0
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    weight = pos - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _fmt(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


async def _count(db: Database, query: str, params: tuple = ()) -> int:
    row = await db.fetchone(query, params)
    return int(row["c"] or 0) if row is not None else 0


async def _lot_coverage(db: Database) -> list[str]:
    total_purchases = await _count(db, "SELECT COUNT(*) AS c FROM purchases")
    total_lots = await _count(db, "SELECT COUNT(*) AS c FROM lot_aggregates")
    closed_lots = await _count(db, "SELECT COUNT(*) AS c FROM lot_aggregates WHERE qty_open = 0")
    open_lots = await _count(db, "SELECT COUNT(*) AS c FROM lot_aggregates WHERE qty_open > 0")
    phantom_lots = await _count(
        db,
        "SELECT COUNT(*) AS c FROM lot_aggregates WHERE status = 'phantom_opening'",
    )
    if total_purchases <= 0:
        coverage = 0.0
    else:
        coverage = (total_lots - phantom_lots) / total_purchases * 100.0

    return [
        f"- Total purchases: {total_purchases}",
        f"- Total closed lots: {closed_lots}",
        f"- Total open lots: {open_lots}",
        f"- Total phantom_opening lots: {phantom_lots}",
        f"- Coverage: {_fmt(coverage)}%",
    ]


async def _lot_profit_rows(db: Database) -> list[dict]:
    rows = await db.fetchall(
        """
        SELECT
            la.lot_id,
            COALESCE(NULLIF(os.category, ''), 'unknown') AS category,
            la.opened_at,
            MAX(a.event_at) AS latest_event_at,
            la.qty,
            la.avg_buy_price,
            SUM(CASE
                WHEN COALESCE(os.is_return, 0) = 0 THEN COALESCE(os.for_pay, 0)
                ELSE 0
            END) AS sales_for_pay,
            SUM(CASE
                WHEN COALESCE(os.is_return, 0) = 1 OR a.event_type = 'return'
                THEN ABS(COALESCE(os.for_pay, 0))
                ELSE 0
            END) AS returns_for_pay
        FROM lot_aggregates la
        JOIN lot_allocations a ON a.lot_id = la.lot_id
        LEFT JOIN own_sales os ON os.srid = a.srid
        WHERE la.qty_open = 0
          AND la.avg_buy_price IS NOT NULL
          AND la.status != 'phantom_opening'
        GROUP BY
            la.lot_id,
            COALESCE(NULLIF(os.category, ''), 'unknown'),
            la.opened_at,
            la.qty,
            la.avg_buy_price
        """
    )

    result: list[dict] = []
    for row in rows:
        opened_at = _parse_dt(str(row["opened_at"]))
        latest_event_at = _parse_dt(str(row["latest_event_at"]))
        if opened_at is None or latest_event_at is None:
            continue

        qty = int(row["qty"] or 0)
        avg_buy_price = float(row["avg_buy_price"] or 0)
        if qty <= 0 or avg_buy_price <= 0:
            continue

        hold_days = max((latest_event_at - opened_at).total_seconds() / 86400.0, 0.25)
        capital = avg_buy_price * qty
        actual_profit = float(row["sales_for_pay"] or 0) - float(row["returns_for_pay"] or 0) - capital
        result.append(
            {
                "category": str(row["category"]),
                "hold_days": hold_days,
                "profit_per_ruble_day": actual_profit / (capital * hold_days),
            }
        )
    return result


def _distribution_section(rows: list[dict], key: str, label: str) -> list[str]:
    by_category: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        by_category[row["category"]].append(float(row[key]))

    lines: list[str] = []
    for category in sorted(by_category):
        values = by_category[category]
        if len(values) < 3:
            lines.append(f"- {category}: Insufficient data: {len(values)}")
            continue
        lines.append(
            f"- {category}: median={_fmt(median(values))}, "
            f"P10={_fmt(_percentile(values, 10))}, "
            f"P90={_fmt(_percentile(values, 90))}, "
            f"n_lots={len(values)}"
        )
    if not lines:
        lines.append(f"Insufficient data: 0 {label}")
    return lines


async def _latency_baseline(db: Database) -> list[str]:
    rows = await db.fetchall(
        """
        WITH scans AS (
            SELECT
                nm_id,
                scanned_at,
                price_rub,
                LAG(price_rub) OVER (
                    PARTITION BY nm_id
                    ORDER BY scanned_at ASC, id ASC
                ) AS prev_price,
                LAG(scanned_at) OVER (
                    PARTITION BY nm_id
                    ORDER BY scanned_at ASC, id ASC
                ) AS prev_scanned_at
            FROM price_history
        )
        SELECT scanned_at, prev_scanned_at
        FROM scans
        WHERE prev_price IS NOT NULL
          AND prev_price > 0
          AND ((prev_price - price_rub) / prev_price) * 100.0 > 5.0
        """
    )
    latencies: list[float] = []
    for row in rows:
        current = _parse_dt(str(row["scanned_at"]))
        previous = _parse_dt(str(row["prev_scanned_at"]))
        if current is None or previous is None:
            continue
        latencies.append(max((current - previous).total_seconds() / 60.0, 0.0))

    if len(latencies) < 1:
        return [f"Insufficient data: {len(latencies)}"]
    return [
        f"- Interesting drops: {len(latencies)}",
        f"- Median latency minutes: {_fmt(median(latencies))}",
        f"- P90 latency minutes: {_fmt(_percentile(latencies, 90))}",
    ]


async def _personal_spp_history(db: Database) -> list[str]:
    rows = await db.fetchall(
        """
        SELECT snapshot_at, category, spp_percent, source
        FROM personal_spp_snapshots
        WHERE date(snapshot_at) >= date('now', '-30 days')
        ORDER BY snapshot_at DESC, id DESC
        """
    )
    if not rows:
        return ["Insufficient data: 0"]
    return [
        f"- {str(row['snapshot_at'])[:10]} [{row['category'] or 'default'}]: "
        f"{_fmt(float(row['spp_percent']))}% ({row['source']})"
        for row in rows
    ]


async def _missed_deal_candidate_count(db: Database) -> list[str]:
    row = await db.fetchone(
        """
        WITH price_drops AS (
            SELECT
                ph.nm_id,
                date(ph.scanned_at) AS candidate_date,
                ph.price_rub AS observed_price,
                LAG(ph.price_rub) OVER (
                    PARTITION BY ph.nm_id
                    ORDER BY ph.scanned_at ASC, ph.id ASC
                ) AS prev_price
            FROM price_history ph
            WHERE date(ph.scanned_at) >= date('now', '-60 days')
        )
        SELECT COUNT(*) AS c
        FROM price_drops d
        WHERE d.prev_price IS NOT NULL
          AND d.prev_price > 0
          AND ((d.prev_price - d.observed_price) / d.prev_price) * 100.0 >= 5.0
          AND 20.0 >= 5.0
          AND NOT EXISTS (
              SELECT 1
              FROM purchases p
              WHERE p.nm_id = CAST(d.nm_id AS INTEGER)
                AND date(p.date) BETWEEN date(d.candidate_date, '-3 days')
                                    AND date(d.candidate_date, '+3 days')
          )
          AND NOT EXISTS (
              SELECT 1
              FROM missed_deal_tags t
              WHERE t.nm_id = CAST(d.nm_id AS INTEGER)
                AND t.candidate_date = d.candidate_date
          )
        """
    )
    return [f"- Candidates: {int(row['c'] or 0) if row is not None else 0}"]


async def generate_report(db_path: str, output: str | None) -> Path:
    db = Database(db_path)
    await db.connect()
    try:
        await db.migrate()
        await db.apply_migrations()

        lot_profit_rows = await _lot_profit_rows(db)
        report_date = datetime.now(timezone.utc).strftime("%Y%m%d")
        output_path = Path(output or f"data/reports/baseline-{report_date}.md")

        lines = [
            f"# Baseline Report {report_date}",
            "",
            "## Lot Coverage",
            *_lot_lines(await _lot_coverage(db)),
            "",
            "## profit_per_ruble_day distribution per category",
            *_lot_lines(_distribution_section(lot_profit_rows, "profit_per_ruble_day", "closed lots")),
            "",
            "## Hold-time distribution per category",
            *_lot_lines(_distribution_section(lot_profit_rows, "hold_days", "closed lots")),
            "",
            "## Latency baseline",
            *_lot_lines(await _latency_baseline(db)),
            "",
            "## Personal СПП history",
            *_lot_lines(await _personal_spp_history(db)),
            "",
            "## Missed-deal candidates count",
            *_lot_lines(await _missed_deal_candidate_count(db)),
            "",
        ]

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(lines), encoding="utf-8")
        return output_path
    finally:
        await db.close()


def _lot_lines(lines: list[str]) -> list[str]:
    return lines


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate Day 5 diagnostic baseline report.")
    parser.add_argument("--db-path", default="data/app.db", help="SQLite database path. Default: data/app.db")
    parser.add_argument("--output", default=None, help="Markdown output path. Default: data/reports/baseline-YYYYMMDD.md")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    path = asyncio.run(generate_report(args.db_path, args.output))
    print(path.resolve())


if __name__ == "__main__":
    main()
