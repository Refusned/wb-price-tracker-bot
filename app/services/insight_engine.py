"""
InsightEngine — genre умных рекомендаций для арбитражника.
Анализирует данные из BusinessRepository, ценового сканера и настроек,
выдаёт актionable insights.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.margin_calculator import MarginCalculator
from app.storage.business_repository import BusinessRepository, DailyMetrics
from app.storage.repositories import ItemRepository, SettingsRepository


@dataclass(slots=True)
class Insight:
    level: str              # "critical" | "warning" | "info" | "opportunity"
    emoji: str
    title: str
    body: str
    action: str | None = None


@dataclass(slots=True)
class BriefingData:
    yesterday: DailyMetrics
    today: DailyMetrics
    week: DailyMetrics
    velocity: float                  # sales/day за неделю
    total_stock: int                 # сумма по всем складам
    in_way_to_client: int            # в пути к клиенту
    days_left: float                 # на сколько хватит остатков
    market_min_price: float | None   # минимальная цена конкурентов
    recommended_buy_count: int       # сколько стоит купить
    insights: list[Insight]


class InsightEngine:
    def __init__(
        self,
        business_repo: BusinessRepository,
        item_repo: ItemRepository,
        settings_repo: SettingsRepository,
    ) -> None:
        self._business = business_repo
        self._items = item_repo
        self._settings = settings_repo

    async def _get_calculator(self) -> MarginCalculator:
        sr = self._settings
        return MarginCalculator(
            spp_percent=await sr.get_float("spp_percent", 24.0),
            wb_commission_percent=await sr.get_float("wb_commission_percent", 15.0),
            logistics_cost_rub=await sr.get_float("logistics_cost_rub", 400.0),
            storage_cost_per_day_rub=await sr.get_float("storage_cost_per_day_rub", 5.0),
            return_rate_percent=await sr.get_float("return_rate_percent", 3.0),
            target_margin_percent=await sr.get_float("target_margin_percent", 10.0),
        )

    async def build_briefing(self) -> BriefingData:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

        today_m = await self._business.get_daily_metrics(today)
        yesterday_m = await self._business.get_daily_metrics(yesterday)
        week_m = await self._business.get_period_metrics(7)
        velocity = await self._business.get_sales_velocity(days=7)

        stock_summary = await self._business.get_stock_summary()
        total_stock = sum(int(s.get("total_qty", 0) or 0) for s in stock_summary)
        in_way = sum(int(s.get("total_in_way_to", 0) or 0) for s in stock_summary)

        days_left = (total_stock / velocity) if velocity > 0 else float("inf")

        # Рыночный минимум — из ценового сканера
        min_price_rub = await self._settings.get_min_price_rub(9000)
        top_items = await self._items.get_top_items(min_price_rub=min_price_rub, limit=5)
        market_min_price = float(top_items[0].price_rub) if top_items else None

        # Рекомендация по закупке
        sell_price = await self._settings.get_float("sell_price_rub", 0.0)
        recommended_buy_count = 0
        if velocity > 0 and market_min_price and sell_price > 0:
            # Купить столько, чтобы хватило на 14 дней продаж
            target_days = 14
            recommended_buy_count = max(0, int(target_days * velocity - total_stock))

        insights = await self._generate_insights(
            today_m, yesterday_m, week_m, velocity,
            total_stock, days_left, market_min_price, sell_price,
        )

        return BriefingData(
            yesterday=yesterday_m,
            today=today_m,
            week=week_m,
            velocity=round(velocity, 2),
            total_stock=total_stock,
            in_way_to_client=in_way,
            days_left=days_left,
            market_min_price=market_min_price,
            recommended_buy_count=recommended_buy_count,
            insights=insights,
        )

    async def _generate_insights(
        self,
        today: DailyMetrics,
        yesterday: DailyMetrics,
        week: DailyMetrics,
        velocity: float,
        total_stock: int,
        days_left: float,
        market_min_price: float | None,
        sell_price: float,
    ) -> list[Insight]:
        insights: list[Insight] = []

        # 1. Критично: остатки кончаются
        if total_stock == 0:
            insights.append(Insight(
                level="critical",
                emoji="🔴",
                title="Нет остатков на складах WB",
                body="Все склады пусты. Продажи остановлены.",
                action="Срочно закупи партию — рынок работает без тебя",
            ))
        elif days_left < 3 and velocity > 0:
            insights.append(Insight(
                level="critical",
                emoji="🔴",
                title=f"Остатков на {days_left:.1f} дн",
                body=f"Скорость продаж {velocity:.1f} шт/день, на складе {total_stock} шт.",
                action="Закупи в ближайшие сутки, чтобы не уйти в out-of-stock",
            ))
        elif days_left < 7 and velocity > 0:
            insights.append(Insight(
                level="warning",
                emoji="🟡",
                title=f"Остатков на {days_left:.1f} дн",
                body=f"Запас тает. {velocity:.1f} шт/день × {total_stock} шт.",
                action="Планируй закупку на этой неделе",
            ))

        # 2. Выкупаемость
        if week.buyout_rate > 0 and week.buyout_rate < 70:
            insights.append(Insight(
                level="warning",
                emoji="⚠️",
                title=f"Выкупаемость {week.buyout_rate}%",
                body="Норма 85%+. Клиенты не выкупают товары, ты теряешь деньги на возвратах.",
                action="Проверь: описания карточек, качество упаковки, отзывы",
            ))

        # 3. Возвраты резко выросли
        if today.returns_count > 0 and yesterday.returns_count >= 0:
            if today.returns_count > yesterday.returns_count * 2 and today.returns_count >= 3:
                insights.append(Insight(
                    level="warning",
                    emoji="🔄",
                    title=f"Рост возвратов: {today.returns_count} сегодня vs {yesterday.returns_count} вчера",
                    body="Что-то изменилось — качество товара, упаковка, или конкурент сделал что-то.",
                    action="Проверь /returns и отзывы по возвращаемым артикулам",
                ))

        # 4. Продажи упали
        if week.sales_count > 0:
            week_avg = week.sales_count / 7
            if today.sales_count > 0 and today.sales_count < week_avg * 0.5:
                insights.append(Insight(
                    level="warning",
                    emoji="📉",
                    title="Продажи ниже обычного",
                    body=f"Сегодня {today.sales_count} vs средне за неделю {week_avg:.1f}/день.",
                    action="Проверь позицию в выдаче, цены конкурентов",
                ))

        # 5. Возможность: рынок упал
        if market_min_price and sell_price > 0:
            calc = await self._get_calculator()
            try:
                result = calc.calculate(market_min_price, sell_price)
                if result.is_profitable and result.profit_per_unit > 1500:
                    insights.append(Insight(
                        level="opportunity",
                        emoji="💰",
                        title="Выгодное окно закупки",
                        body=f"Цена на рынке {market_min_price:.0f} ₽, прибыль {result.profit_per_unit:.0f} ₽/шт ({result.margin_percent}%).",
                        action=f"Рассмотри закупку — /calc {int(market_min_price)}",
                    ))
            except ValueError:
                pass

        # 6. Просто хорошо — продажи растут
        if today.sales_count > 0 and yesterday.sales_count > 0:
            if today.sales_count > yesterday.sales_count * 1.5:
                insights.append(Insight(
                    level="info",
                    emoji="🚀",
                    title="Продажи растут",
                    body=f"Сегодня {today.sales_count} vs вчера {yesterday.sales_count}.",
                    action=None,
                ))

        # 7. Риск бана: много закупок одним типом
        purchases_30d = await self._business.list_purchases(days=30)
        total_bought_30d = sum(p.get("quantity", 0) for p in purchases_30d)
        if total_bought_30d >= 40:
            insights.append(Insight(
                level="warning",
                emoji="⚠️",
                title="Риск shadow-ban",
                body=f"За 30 дней закуплено {total_bought_30d} шт одного товара с одного аккаунта. WB может отметить как самовыкупы.",
                action="Сбавь темп / используй разные ПВЗ",
            ))

        # 8. Нет закупок давно
        if not purchases_30d and total_stock > 0 and velocity > 0:
            insights.append(Insight(
                level="info",
                emoji="📝",
                title="Нет записей о закупках",
                body="Не записывал свои закупки через /addpurchase? Бот не может точно посчитать P&L.",
                action="Записывай закупки для точной экономики",
            ))

        return insights

    async def get_reorder_recommendation(self) -> dict[str, Any]:
        """Детальная рекомендация по закупке."""
        velocity = await self._business.get_sales_velocity(days=14)
        stock_summary = await self._business.get_stock_summary()
        total_stock = sum(int(s.get("total_qty", 0) or 0) for s in stock_summary)

        min_price_rub = await self._settings.get_min_price_rub(9000)
        top_items = await self._items.get_top_items(min_price_rub=min_price_rub, limit=10)
        if not top_items:
            return {
                "error": "Нет данных о ценах конкурентов. Подожди первого скана или /rescan.",
            }

        market_min = float(top_items[0].price_rub)
        sell_price = await self._settings.get_float("sell_price_rub", 0.0)

        if sell_price <= 0:
            return {
                "error": "Установи цену продажи: /setsellprice <цена>",
            }

        calc = await self._get_calculator()
        try:
            result = calc.calculate(market_min, sell_price)
        except ValueError as e:
            return {"error": str(e)}

        target_days = 14
        if velocity > 0:
            ideal_count = max(0, int(target_days * velocity - total_stock))
        else:
            ideal_count = 0

        total_investment = ideal_count * result.buy_price_with_spp
        expected_profit = ideal_count * result.profit_per_unit
        roi = (expected_profit / total_investment * 100) if total_investment > 0 else 0.0
        days_to_sell = (ideal_count / velocity) if velocity > 0 else 0

        return {
            "velocity": round(velocity, 2),
            "total_stock": total_stock,
            "stock_days_left": (total_stock / velocity) if velocity > 0 else float("inf"),
            "market_min_price": market_min,
            "buy_price_with_spp": result.buy_price_with_spp,
            "sell_price": sell_price,
            "profit_per_unit": result.profit_per_unit,
            "margin_percent": result.margin_percent,
            "recommended_count": ideal_count,
            "total_investment": total_investment,
            "expected_profit": expected_profit,
            "roi_percent": round(roi, 1),
            "days_to_sell": round(days_to_sell, 1),
            "is_profitable": result.is_profitable,
        }

    async def detect_anomalies(self) -> list[Insight]:
        """Detect sudden changes worth attention."""
        briefing = await self.build_briefing()
        return briefing.insights
