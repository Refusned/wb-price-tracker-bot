from __future__ import annotations

import pytest

from app.services.margin_calculator import MarginCalculator


def _make_calc(**kwargs) -> MarginCalculator:
    defaults = dict(
        spp_percent=24.0,
        wb_commission_percent=15.0,
        logistics_cost_rub=400.0,
        storage_cost_per_day_rub=5.0,
        avg_storage_days=14.0,
        return_rate_percent=3.0,
        target_margin_percent=10.0,
    )
    defaults.update(kwargs)
    return MarginCalculator(**defaults)


class TestMarginCalculatorHappyPath:
    def test_profitable_deal(self) -> None:
        calc = _make_calc()
        result = calc.calculate(buy_price=9500, sell_price=12000)

        assert result.buy_price == 9500
        assert result.buy_price_with_spp == 7220.0  # 9500 * 0.76
        assert result.wb_commission == 1800.0  # 12000 * 0.15
        assert result.logistics == 400.0
        assert result.storage == 70.0  # 5 * 14
        assert result.return_cost == 12.0  # 0.03 * 400
        assert result.profit_per_unit == 12000 - 7220 - 1800 - 400 - 70 - 12
        assert result.profit_per_unit == 2498.0
        assert result.margin_percent > 10
        assert result.is_profitable is True


class TestMarginCalculatorEdgeCases:
    def test_zero_margin_boundary(self) -> None:
        calc = _make_calc()
        # Find a buy price where profit is near zero
        # total_cost = buy*0.76 + sell*0.15 + 400 + 70 + 12
        # profit = sell - total_cost = 0
        # sell = buy*0.76 + sell*0.15 + 482
        # 0.85*sell = buy*0.76 + 482
        # For sell=12000: buy*0.76 = 0.85*12000 - 482 = 9718
        # buy = 9718 / 0.76 = 12786.84
        result = calc.calculate(buy_price=12787, sell_price=12000)
        assert result.profit_per_unit < 0
        assert result.is_profitable is False

    def test_unprofitable_deal(self) -> None:
        calc = _make_calc()
        result = calc.calculate(buy_price=15000, sell_price=12000)
        assert result.profit_per_unit < 0
        assert result.is_profitable is False

    def test_spp_zero(self) -> None:
        calc = _make_calc(spp_percent=0)
        result = calc.calculate(buy_price=9500, sell_price=12000)
        assert result.buy_price_with_spp == 9500.0  # no discount
        assert result.profit_per_unit < 2498.0  # less profit without SPP

    def test_spp_maximum(self) -> None:
        calc = _make_calc(spp_percent=50)
        result = calc.calculate(buy_price=9500, sell_price=12000)
        assert result.buy_price_with_spp == 4750.0  # 9500 * 0.5
        assert result.profit_per_unit > 2498.0  # more profit with max SPP

    def test_sell_price_zero_raises(self) -> None:
        calc = _make_calc()
        with pytest.raises(ValueError, match="sell_price must be greater than 0"):
            calc.calculate(buy_price=9500, sell_price=0)

    def test_return_rate_zero(self) -> None:
        calc = _make_calc(return_rate_percent=0)
        result = calc.calculate(buy_price=9500, sell_price=12000)
        assert result.return_cost == 0.0
        assert result.profit_per_unit == 2510.0  # 2498 + 12 saved on returns


class TestMarginCalculatorClamping:
    def test_spp_clamped_above_50(self) -> None:
        calc = _make_calc(spp_percent=80)
        assert calc.spp_percent == 50.0

    def test_commission_clamped_above_30(self) -> None:
        calc = _make_calc(wb_commission_percent=50)
        assert calc.wb_commission_percent == 30.0

    def test_negative_spp_clamped_to_zero(self) -> None:
        calc = _make_calc(spp_percent=-10)
        assert calc.spp_percent == 0.0
