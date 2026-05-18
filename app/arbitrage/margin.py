"""Round 3-calibrated margin formula for arbitrage scanner.

Verified on owner's real numbers (артикул 876392996, 2026-05-18):
    listed_price=15000 → predicted revenue ~11725, ground truth ~11500
    Bias ~+220₽ (overestimate ~40% of profit). Calibration loop Day 30+
    via finance_journal will tune coefficients to bias <5%.

WB Pricing Layer Cake 2026 (verified via WB оферта п. 5.3-5.4):
- WB-Скидка (бывш. СПП) субсидируется WB через `В = Рц × кВВ - И`
- Селлер получает выплаты исходя из retail_price_withdisc = listed_price
  (не buyer final price)
- УСН считается из revenue ПОСЛЕ WB-удержаний (separately)
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class MarginBreakdown:
    """Detailed margin computation result — every component visible.

    All ₽ fields are rounded to int kopecks-less rubles for display.
    Percentages use full float precision.
    """
    buy_price_rub: int
    listed_price_rub: int
    commission_pct: float
    commission_rub: int
    logistics_rub: int
    acquiring_rub: int
    return_reserve_rub: int
    tax_rub: int
    holding_rub: int
    revenue_after_wb_rub: int
    margin_rub: int
    margin_percent: float
    profit_per_ruble_day_pct: float
    expected_hold_days: int
    spp_percent_used: float
    spp_source: str
    spp_confidence: str
    breakdown_note: str = field(default="")


def estimate_hold_days_from_feedbacks(feedbacks: int) -> int:
    """Round 4 D32: hold-time proxy from card.wb.ru `feedbacks` count.

    Conservative heuristic until Day 30+ calibration loop replaces it
    with actual per-category lot hold-time distribution.
    """
    if feedbacks >= 1000:
        return 5
    if feedbacks >= 200:
        return 10
    if feedbacks >= 50:
        return 18
    if feedbacks >= 10:
        return 30
    return 45


def compute_arbitrage_margin(
    *,
    market_price_rub: int,
    personal_buyer_spp_pct: float,
    spp_source: str,
    spp_confidence: str,
    listed_price_rub: int | None = None,
    commission_pct: float | None = None,
    logistics_rub: float | None = None,
    acquiring_percent: float = 0.0,
    tax_percent: float = 2.0,
    return_rate_percent: float = 3.0,
    storage_cost_per_day_rub: float = 5.0,
    expected_hold_days: int = 14,
) -> MarginBreakdown:
    """Compute full margin breakdown for an arbitrage opportunity.

    Args:
        market_price_rub: current public sale_price of the SKU on WB
        personal_buyer_spp_pct: my buyer-side personal СПП for this nm_id
            (from observations or cookie)
        spp_source: 'cookie' | 'observation' | 'category_avg' | 'manual' | 'default'
        spp_confidence: 'high' | 'medium' | 'low'
        listed_price_rub: price I would set as seller; defaults to market price
        commission_pct: WB FBS commission % (from tariffs API); 16% fallback
        logistics_rub: FBS logistics fee per unit (from tariffs/box); 500 fallback
        acquiring_percent: эквайринг % from config
        tax_percent: УСН %
        return_rate_percent: % of orders that return
        storage_cost_per_day_rub: per-unit per-day storage cost
        expected_hold_days: predicted days to sell (from feedbacks proxy)

    Returns:
        MarginBreakdown with every component visible for alert message.
    """
    # === BUY (with my personal buyer-side СПП) ===
    buy_price = market_price_rub * (1 - personal_buyer_spp_pct / 100.0)

    # === SELL (WB pays based on listed_price, not buyer final) ===
    listed_price = listed_price_rub or market_price_rub
    eff_commission_pct = commission_pct if commission_pct is not None else 16.0
    eff_logistics = logistics_rub if logistics_rub is not None else 500.0

    commission_rub = listed_price * eff_commission_pct / 100.0
    acquiring_rub = listed_price * acquiring_percent / 100.0
    # Half of return rate × full price as conservative reserve (товар вернётся
    # — мы платим WB-комиссию обратно + логистику возврата)
    return_reserve_rub = listed_price * return_rate_percent / 100.0 * 0.5

    revenue_after_wb = (
        listed_price - commission_rub - eff_logistics - acquiring_rub - return_reserve_rub
    )

    # === TAX (separately, applied to revenue after WB удержаний) ===
    tax_rub = max(0.0, revenue_after_wb * tax_percent / 100.0)

    # === HOLDING COST (capital lock × storage per day) ===
    holding_rub = storage_cost_per_day_rub * max(expected_hold_days, 1)

    # === NET MARGIN ===
    margin_rub = revenue_after_wb - tax_rub - buy_price - holding_rub
    margin_percent = (margin_rub / buy_price * 100.0) if buy_price > 0 else 0.0
    pprd_denom = buy_price * max(expected_hold_days, 1)
    profit_per_ruble_day_pct = (margin_rub / pprd_denom * 100.0) if pprd_denom > 0 else 0.0

    return MarginBreakdown(
        buy_price_rub=int(round(buy_price)),
        listed_price_rub=int(round(listed_price)),
        commission_pct=round(eff_commission_pct, 2),
        commission_rub=int(round(commission_rub)),
        logistics_rub=int(round(eff_logistics)),
        acquiring_rub=int(round(acquiring_rub)),
        return_reserve_rub=int(round(return_reserve_rub)),
        tax_rub=int(round(tax_rub)),
        holding_rub=int(round(holding_rub)),
        revenue_after_wb_rub=int(round(revenue_after_wb)),
        margin_rub=int(round(margin_rub)),
        margin_percent=round(margin_percent, 2),
        profit_per_ruble_day_pct=round(profit_per_ruble_day_pct, 3),
        expected_hold_days=expected_hold_days,
        spp_percent_used=round(personal_buyer_spp_pct, 2),
        spp_source=spp_source,
        spp_confidence=spp_confidence,
    )


def passes_threshold(
    margin: MarginBreakdown,
    *,
    min_pprd_percent: float,
    min_profit_rub: int,
    min_margin_percent: float,
) -> bool:
    """Round 4 canonical threshold: PPRD ≥X AND profit ≥Y AND margin ≥Z."""
    return (
        margin.profit_per_ruble_day_pct >= min_pprd_percent
        and margin.margin_rub >= min_profit_rub
        and margin.margin_percent >= min_margin_percent
        and margin.spp_confidence != "low"
    )
