"""WB-arbitrage margin formula (Final model, verified 2026-05-18).

Verified via WB оферта п. 5.3-5.4 + owner ground truth on art 876392996:

PRICING CASCADE (per WB official mechanics 2026):
    RRC                            29 999₽   (selker's published recommended)
       ↓ seller_discount (50%)              selker controls
    listed_price                   15 000₽   ← BASE for seller revenue
       ↓ WB-Скидка (СПП)              ── WB pays this, NOT selker (оферта п. 5.4)
       ↓ category-wide, varies monthly 21-25% for колонки, 30% for пылесосы
    buyer_price_after_spp          11 250₽   ← what WB API returns in sizes[0].price.product
       ↓ wallet_bonus (6%)             ── personal, applies if paying via WB-Кошелёк
    buyer_pays_at_checkout         10 658₽   ← owner's actual checkout

SELLER REVENUE (separate from buyer price — WB subsidizes СПП):
    ppvz_for_pay = listed_price - WB_commission - logistics - acquiring - return - tax
                 ≈ listed_price × 0.78 (varies by category)
    For ground truth: 15000 - удержания ≈ 11 000-12 000₽ (owner verified)

ARBITRAGE OPPORTUNITY:
    As BUYER: pay = chuzhoi_market_price × (1 - wallet/100)
             where chuzhoi_market_price = chuzhoi_listed × (1 - cat_spp/100)
    As SELLER: revenue = my_listed × 0.78
    If I match competitor's listed (my_listed ≈ chuzhoi_listed):
        margin ≈ chuzhoi_listed × (0.78 - (1 - cat_spp) × (1 - wallet))
        For cat_spp=25%, wallet=6%:
            margin ≈ chuzhoi_listed × (0.78 - 0.705) = chuzhoi_listed × 7.5%
        For cat_spp=30%, wallet=6%:
            margin ≈ chuzhoi_listed × (0.78 - 0.658) = chuzhoi_listed × 12.2%
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class MarginBreakdown:
    """Detailed margin computation — every component visible.

    All ₽ fields rounded to int. Percentages use full float precision.
    """
    # Inputs (resolved)
    market_price_rub: int           # WB API price (already after seller_discount + WB-СПП)
    listed_implied_rub: int         # reverse-engineered: listed = market / (1 - cat_spp)
    category_spp_pct: float         # WB-Скидка категории (платформенный дисконт)
    wallet_bonus_pct: float         # WB-Кошелёк дисконт (личный, 6%)
    # Costs
    buy_price_rub: int              # what I pay as buyer = market × (1 - wallet)
    commission_pct: float           # WB FBS commission per subject
    commission_rub: int             # = listed × commission_pct
    logistics_rub: int              # FBS box logistics
    acquiring_rub: int              # эквайринг
    return_reserve_rub: int         # резерв возвратов
    tax_rub: int                    # УСН на revenue
    holding_rub: int                # хранение × дней
    # Outputs
    revenue_after_wb_rub: int       # listed - WB удержания
    margin_rub: int                 # revenue - tax - buy_price - holding
    margin_percent: float           # margin / buy_price × 100
    profit_per_ruble_day_pct: float  # ROI/day
    expected_hold_days: int
    # Provenance
    spp_source: str
    spp_confidence: str
    breakdown_note: str = field(default="")


def estimate_hold_days_from_feedbacks(feedbacks: int) -> int:
    """Hold-time proxy from card.wb.ru `feedbacks`. Calibration loop Day 30+
    will replace with actual per-category lot hold-time distribution.
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


def decompose_composite_spp(composite_pct: float, wallet_pct: float = 6.0) -> float:
    """Decompose observed composite buyer discount into category-only СПП.

    composite = 1 - (1 - cat_spp) × (1 - wallet)
    cat_spp = 1 - (1 - composite) / (1 - wallet)

    For owner's Station Midi observation (composite=28.95%, wallet=6%):
        cat_spp = 1 - 0.7105 / 0.94 = 24.4% ← matches WB-СПП "21-25%"
    """
    if wallet_pct <= 0 or wallet_pct >= 100:
        return composite_pct
    composite = max(0.0, min(99.99, composite_pct)) / 100.0
    wallet = wallet_pct / 100.0
    one_minus_cat = (1.0 - composite) / (1.0 - wallet)
    cat = 1.0 - max(0.0, min(0.99, one_minus_cat))
    return cat * 100.0


def compute_arbitrage_margin(
    *,
    market_price_rub: int,
    category_spp_pct: float,
    wallet_bonus_pct: float = 6.0,
    spp_source: str = "category_avg",
    spp_confidence: str = "medium",
    listed_price_rub: int | None = None,
    commission_pct: float | None = None,
    logistics_rub: float | None = None,
    acquiring_percent: float = 0.0,
    tax_percent: float = 2.0,
    return_rate_percent: float = 3.0,
    storage_cost_per_day_rub: float = 5.0,
    expected_hold_days: int = 14,
) -> MarginBreakdown:
    """Compute full margin for an arbitrage opportunity.

    Args:
        market_price_rub: WB-API price (after seller_discount + WB-СПП). The
            value returned by sizes[0].price.product. NOT the listed price.
        category_spp_pct: WB platform discount for this category (21-30%).
        wallet_bonus_pct: My personal WB-Кошелёк bonus (~6%).
        listed_price_rub: My chosen listing price as a seller. Defaults to
            chuzhoi_listed_implied (match cheapest competitor exactly).
        commission_pct: FBS commission% (from tariffs API).
        logistics_rub: FBS box logistics (from tariffs/box).

    See module docstring for the full pricing cascade and arbitrage logic.
    """
    # === BUY (as personal buyer) ===
    # market_price is already buyer-price-after-WB-СПП, so I only apply wallet:
    buy_price = market_price_rub * (1 - wallet_bonus_pct / 100.0)

    # === IMPLIED LISTED PRICE (reverse-engineered) ===
    # The competitor's listed_price = what they set as a seller, before СПП.
    cat_factor = max(0.01, 1.0 - category_spp_pct / 100.0)
    listed_implied = market_price_rub / cat_factor

    # If owner provides explicit my_listed (their strategy), use it.
    # Otherwise default = match cheapest competitor exactly.
    my_listed = listed_price_rub or listed_implied

    # === SELL (WB pays from my listed, NOT buyer final) ===
    eff_commission_pct = commission_pct if commission_pct is not None else 16.0
    eff_logistics = logistics_rub if logistics_rub is not None else 500.0

    commission_rub = my_listed * eff_commission_pct / 100.0
    acquiring_rub = my_listed * acquiring_percent / 100.0
    return_reserve_rub = my_listed * return_rate_percent / 100.0 * 0.5

    revenue_after_wb = (
        my_listed - commission_rub - eff_logistics - acquiring_rub - return_reserve_rub
    )

    # === TAX (УСН — separately, on revenue) ===
    tax_rub = max(0.0, revenue_after_wb * tax_percent / 100.0)

    # === HOLDING COST ===
    holding_rub = storage_cost_per_day_rub * max(expected_hold_days, 1)

    # === NET MARGIN ===
    margin_rub = revenue_after_wb - tax_rub - buy_price - holding_rub
    margin_percent = (margin_rub / buy_price * 100.0) if buy_price > 0 else 0.0
    pprd_denom = buy_price * max(expected_hold_days, 1)
    profit_per_ruble_day_pct = (margin_rub / pprd_denom * 100.0) if pprd_denom > 0 else 0.0

    return MarginBreakdown(
        market_price_rub=int(round(market_price_rub)),
        listed_implied_rub=int(round(listed_implied)),
        category_spp_pct=round(category_spp_pct, 2),
        wallet_bonus_pct=round(wallet_bonus_pct, 2),
        buy_price_rub=int(round(buy_price)),
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
    """Canonical Round 4 threshold + accept category_avg as alertable
    (WB-СПП is category-wide, NOT per-buyer, so category_avg is valid).
    """
    return (
        margin.profit_per_ruble_day_pct >= min_pprd_percent
        and margin.margin_rub >= min_profit_rub
        and margin.margin_percent >= min_margin_percent
        and margin.spp_confidence != "low"
    )
