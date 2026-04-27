from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class MarginResult:
    buy_price: float
    buy_price_with_spp: float
    sell_price: float
    wb_commission: float
    logistics: float
    storage: float
    return_cost: float
    profit_per_unit: float
    margin_percent: float
    roi_percent: float
    is_profitable: bool


class MarginCalculator:
    def __init__(
        self,
        *,
        spp_percent: float = 24.0,
        wb_commission_percent: float = 15.0,
        logistics_cost_rub: float = 400.0,
        storage_cost_per_day_rub: float = 5.0,
        avg_storage_days: float = 14.0,
        return_rate_percent: float = 3.0,
        target_margin_percent: float = 10.0,
    ) -> None:
        self.spp_percent = max(0.0, min(50.0, spp_percent))
        self.wb_commission_percent = max(0.0, min(30.0, wb_commission_percent))
        self.logistics_cost_rub = max(0.0, logistics_cost_rub)
        self.storage_cost_per_day_rub = max(0.0, storage_cost_per_day_rub)
        self.avg_storage_days = max(0.0, avg_storage_days)
        self.return_rate_percent = max(0.0, min(100.0, return_rate_percent))
        self.target_margin_percent = target_margin_percent

    def calculate(self, buy_price: float, sell_price: float) -> MarginResult:
        if sell_price <= 0:
            raise ValueError("sell_price must be greater than 0")
        if buy_price < 0:
            raise ValueError("buy_price must be non-negative")

        buy_with_spp = buy_price * (1.0 - self.spp_percent / 100.0)
        wb_commission = sell_price * self.wb_commission_percent / 100.0
        storage = self.storage_cost_per_day_rub * self.avg_storage_days
        return_cost = self.return_rate_percent / 100.0 * self.logistics_cost_rub

        total_cost = buy_with_spp + wb_commission + self.logistics_cost_rub + storage + return_cost
        profit = sell_price - total_cost
        margin_percent = (profit / sell_price) * 100.0 if sell_price > 0 else 0.0
        roi_percent = (profit / buy_with_spp) * 100.0 if buy_with_spp > 0 else 0.0

        return MarginResult(
            buy_price=buy_price,
            buy_price_with_spp=round(buy_with_spp, 2),
            sell_price=sell_price,
            wb_commission=round(wb_commission, 2),
            logistics=self.logistics_cost_rub,
            storage=round(storage, 2),
            return_cost=round(return_cost, 2),
            profit_per_unit=round(profit, 2),
            margin_percent=round(margin_percent, 2),
            roi_percent=round(roi_percent, 2),
            is_profitable=profit > 0 and margin_percent >= self.target_margin_percent,
        )
