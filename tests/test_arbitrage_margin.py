"""Ground-truth тесты денежной формулы арбитража.

Самая важная функция проекта (`compute_arbitrage_margin`) раньше не имела ни
одного теста, хотя именно она решает, слать ли алерт на реальную покупку, и
уже однажды содержала критический баг с двойным применением СПП (Day 18).

Ground truth — арт. 876392996 (из докстринга margin.py):
    WB API price (market) ≈ 11 189₽; category СПП ≈ 24.4%, WB-Кошелёк 6%
    listed_implied ≈ 14 800₽, checkout(buy) ≈ 10 518₽, net margin ≈ 890₽.
"""
from __future__ import annotations

from app.arbitrage.margin import (
    compute_arbitrage_margin,
    decompose_composite_spp,
    passes_threshold,
)

MARKET = 11189
CAT_SPP = 24.4
WALLET = 6.0


def _gt_margin():
    return compute_arbitrage_margin(
        market_price_rub=MARKET,
        category_spp_pct=CAT_SPP,
        wallet_bonus_pct=WALLET,
        commission_pct=16.0,
        logistics_rub=500.0,
        acquiring_percent=0.0,
        tax_percent=2.0,
        return_rate_percent=3.0,
        storage_cost_per_day_rub=5.0,
        expected_hold_days=14,
    )


def test_buy_price_is_wallet_only_not_double_spp() -> None:
    """Регрессия Day 18: buy = market×(1-wallet), НЕ market×(1-composite)."""
    m = _gt_margin()
    assert m.buy_price_rub == round(MARKET * (1 - WALLET / 100))  # 10518
    # Если бы СПП применялась дважды, buy ≈ 7951 (market×(1-composite≈0.29)).
    # Граница market×(1-0.10)=10070 ловит регрессию НЕЗАВИСИМО от строки выше
    # (раньше было ×(1-0.30)=7832 — слишком слабо, 7951>7832 проходило).
    assert m.buy_price_rub > MARKET * (1 - 0.10)


def test_listed_implied_matches_ground_truth() -> None:
    assert 14780 <= _gt_margin().listed_implied_rub <= 14820


def test_revenue_after_wb_matches_ground_truth() -> None:
    assert 11690 <= _gt_margin().revenue_after_wb_rub <= 11730


def test_net_margin_matches_ground_truth() -> None:
    m = _gt_margin()
    assert 850 <= m.margin_rub <= 950          # модель ~890, реальность ~1000
    assert 7.0 <= m.margin_percent <= 10.0
    assert m.profit_per_ruble_day_pct > 0


def test_decompose_composite_spp_ground_truth() -> None:
    # composite 28.95% при wallet 6% → category 24.4%.
    assert abs(decompose_composite_spp(28.95, wallet_pct=6.0) - 24.4) < 0.3


def test_decompose_roundtrip() -> None:
    cat = 24.4
    composite = (1 - (1 - cat / 100) * (1 - WALLET / 100)) * 100
    assert abs(decompose_composite_spp(composite, wallet_pct=WALLET) - cat) < 0.01


def test_passes_threshold_rejects_low_confidence() -> None:
    m = compute_arbitrage_margin(
        market_price_rub=MARKET, category_spp_pct=CAT_SPP, wallet_bonus_pct=WALLET,
        commission_pct=16.0, logistics_rub=500.0, spp_confidence="low",
    )
    assert passes_threshold(m, min_pprd_percent=0.0, min_profit_rub=0,
                            min_margin_percent=0.0) is False


def test_passes_threshold_respects_min_profit() -> None:
    m = _gt_margin()
    assert passes_threshold(m, min_pprd_percent=0.0, min_profit_rub=100,
                            min_margin_percent=0.0) is True
    assert passes_threshold(m, min_pprd_percent=0.0, min_profit_rub=100000,
                            min_margin_percent=0.0) is False


def test_zero_market_does_not_crash() -> None:
    m = compute_arbitrage_margin(
        market_price_rub=0, category_spp_pct=CAT_SPP, wallet_bonus_pct=WALLET,
        commission_pct=16.0, logistics_rub=500.0,
    )
    assert m.margin_percent == 0.0
    assert m.profit_per_ruble_day_pct == 0.0
