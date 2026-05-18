"""Telegram alert message builder for arbitrage candidates."""
from __future__ import annotations

from app.arbitrage.margin import MarginBreakdown


def _fmt_rub(value: int) -> str:
    """Format integer rubles with thousand separators: 12345 → '12 345₽'."""
    if value is None:
        return "—"
    return f"{value:,}".replace(",", " ") + "₽"


def _confidence_emoji(confidence: str) -> str:
    return {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(confidence, "⚪️")


def build_alert_message(
    *,
    nm_id: int,
    name: str | None,
    brand: str | None,
    query: str,
    margin: MarginBreakdown,
    cohort_size: int,
    market_median: int,
    market_p25: int,
    market_min: int,
    url: str,
) -> str:
    """Build Telegram alert text with detailed cost breakdown.

    Format keeps single screen of phone view, ~12 lines.
    """
    name_display = (name or f"nm {nm_id}")[:60]
    brand_part = f" • {brand}" if brand else ""
    confidence = _confidence_emoji(margin.spp_confidence)

    lines = [
        f"🎯 Связка | margin {margin.margin_percent:.1f}% | ROI/день {margin.profit_per_ruble_day_pct:.2f}%",
        f"",
        f"📦 {name_display}{brand_part}",
        f"🔗 nm: {nm_id} | запрос: {query}",
        f"{confidence} СПП {margin.spp_percent_used:.1f}% ({margin.spp_source}, conf={margin.spp_confidence})",
        f"",
        f"━━━━━━━━━━━━━━━━━━━",
        f"💰 Купить (с моей СПП): {_fmt_rub(margin.buy_price_rub)}",
        f"💵 Выставить: {_fmt_rub(margin.listed_price_rub)} (P25 cohort)",
        f"   ↳ медиана: {_fmt_rub(market_median)} | мин: {_fmt_rub(market_min)} | конкур: {cohort_size}",
        f"",
        f"📉 Удержания WB:",
        f"  • Комиссия {margin.commission_pct:.1f}%: −{_fmt_rub(margin.commission_rub)}",
        f"  • Логистика: −{_fmt_rub(margin.logistics_rub)}",
        f"  • Эквайринг: −{_fmt_rub(margin.acquiring_rub)}",
        f"  • Резерв возврата: −{_fmt_rub(margin.return_reserve_rub)}",
        f"  • Налог УСН: −{_fmt_rub(margin.tax_rub)}",
        f"  • Hold ({margin.expected_hold_days}d): −{_fmt_rub(margin.holding_rub)}",
        f"",
        f"━━━━━━━━━━━━━━━━━━━",
        f"💚 Чистая прибыль: {_fmt_rub(margin.margin_rub)}",
        f"📈 Выручка после WB: {_fmt_rub(margin.revenue_after_wb_rub)}",
        f"",
        f"🛒 {url}",
    ]
    return "\n".join(lines)
