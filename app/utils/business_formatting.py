"""
Formatters for business events: orders, sales, returns, briefing.
"""
from __future__ import annotations

from app.services.insight_engine import BriefingData
from app.utils.formatting import format_iso_datetime, format_price_rub


def build_new_order_alert(order: dict) -> str:
    """Alert about a new order (not cancelled)."""
    lines = [
        "📦 Новый заказ",
        "",
        f"Артикул: {order.get('supplier_article') or order.get('nm_id')}",
        f"Товар: {order.get('subject') or 'N/A'}",
        f"Цена: {format_price_rub(order.get('price_with_disc', 0))}",
    ]
    if order.get("total_price") and order.get("total_price") != order.get("price_with_disc"):
        lines.append(f"  Без скидки: {format_price_rub(order['total_price'])}")
    if order.get("spp_percent"):
        lines.append(f"  СПП: {order['spp_percent']}%")
    if order.get("warehouse_name"):
        lines.append(f"Склад: {order['warehouse_name']}")

    lines.extend([
        "",
        f"nm_id: {order.get('nm_id')}",
        f"Дата: {format_iso_datetime(str(order.get('date', '')))}",
    ])
    return "\n".join(lines)


def build_new_sale_alert(sale: dict) -> str:
    """Alert when buyer actually paid (выкуп)."""
    for_pay = float(sale.get("for_pay", 0))
    lines = [
        "💰 Выкуп",
        "",
        f"Артикул: {sale.get('supplier_article') or sale.get('nm_id')}",
        f"Товар: {sale.get('subject') or 'N/A'}",
        f"После комиссии WB: {format_price_rub(for_pay)}",
        f"Цена продажи: {format_price_rub(sale.get('price_with_disc', 0))}",
    ]
    if sale.get("spp_percent"):
        lines.append(f"  СПП покупателя: {sale['spp_percent']}%")
    if sale.get("commission_percent"):
        lines.append(f"  Комиссия WB: {sale['commission_percent']}%")
    if sale.get("warehouse_name"):
        lines.append(f"Склад: {sale['warehouse_name']}")

    lines.extend([
        "",
        f"nm_id: {sale.get('nm_id')}",
        f"Дата: {format_iso_datetime(str(sale.get('date', '')))}",
        "",
        "ℹ️ Логистика, хранение, эквайринг списываются отдельно в финотчёте",
    ])
    return "\n".join(lines)


def build_new_return_alert(sale: dict) -> str:
    for_pay = float(sale.get("for_pay", 0))
    lines = [
        "🔄 Возврат",
        "",
        f"Артикул: {sale.get('supplier_article') or sale.get('nm_id')}",
        f"Товар: {sale.get('subject') or 'N/A'}",
        f"Вернули клиенту: {format_price_rub(sale.get('price_with_disc', 0))}",
        f"Удержано от твоей выплаты: {format_price_rub(abs(for_pay))}",
    ]
    if sale.get("warehouse_name"):
        lines.append(f"Склад: {sale['warehouse_name']}")

    lines.extend([
        "",
        f"nm_id: {sale.get('nm_id')}",
        f"Дата: {format_iso_datetime(str(sale.get('date', '')))}",
        "",
        "💡 Проверь причину через /returns",
    ])
    return "\n".join(lines)


def build_briefing_message(briefing: BriefingData) -> str:
    y = briefing.yesterday
    t = briefing.today
    w = briefing.week

    lines = [
        "☀️ Утренний брифинг",
        "",
        "📊 Вчера:",
        f"  Заказов: {y.orders_count} (отменено {y.orders_canceled})",
        f"  Выкуплено: {y.sales_count} шт ({format_price_rub(y.revenue_net)} к выплате)",
        f"  Возвратов: {y.returns_count}",
        f"  Выкупаемость: {y.buyout_rate}%",
        "",
        "📈 За 7 дней:",
        f"  Выкуплено: {w.sales_count} шт",
        f"  Возвратов: {w.returns_count}",
        f"  Чистая выручка: {format_price_rub(w.revenue_net)}",
        f"  Выкупаемость: {w.buyout_rate}%",
        f"  Скорость: {briefing.velocity} шт/день",
        "",
        "📦 Сейчас на складах:",
        f"  Остаток: {briefing.total_stock} шт",
    ]
    if briefing.in_way_to_client:
        lines.append(f"  В пути к клиенту: {briefing.in_way_to_client} шт")
    if briefing.velocity > 0 and briefing.days_left != float("inf"):
        lines.append(f"  Хватит на: {briefing.days_left:.1f} дн")
    else:
        lines.append("  Хватит на: неопределённо")

    if briefing.market_min_price:
        lines.extend([
            "",
            "🏪 Рынок:",
            f"  Минимальная цена конкурентов: {format_price_rub(briefing.market_min_price)}",
        ])

    if briefing.recommended_buy_count > 0:
        lines.extend([
            "",
            f"💡 Рекомендуемая закупка: {briefing.recommended_buy_count} шт",
            f"   /reorder — детали",
        ])

    if briefing.insights:
        lines.append("")
        lines.append("🎯 Инсайты:")
        for ins in briefing.insights:
            lines.append(f"{ins.emoji} {ins.title}")
            if ins.body:
                lines.append(f"   {ins.body}")
            if ins.action:
                lines.append(f"   → {ins.action}")
            lines.append("")

    return "\n".join(lines).rstrip()


def build_period_metrics_message(label: str, metrics) -> str:
    lines = [
        f"📊 {label}",
        "",
        f"Заказов: {metrics.orders_count} (отменено {metrics.orders_canceled})",
        f"Выкуплено: {metrics.sales_count} шт",
        f"Возвратов: {metrics.returns_count}",
        f"Выручка брутто: {format_price_rub(metrics.revenue_total)}",
        f"К выплате (с учётом возвратов): {format_price_rub(metrics.revenue_net)}",
        f"Выкупаемость: {metrics.buyout_rate}%",
    ]
    if metrics.unique_articles:
        lines.append(f"Уникальных артикулов: {metrics.unique_articles}")
    return "\n".join(lines)


def build_stock_message(stocks: list[dict]) -> str:
    if not stocks:
        return "📦 Остатков нет. Жду следующий скан Seller API (обновляется каждые 30 мин)."

    lines = ["📦 Остатки по складам WB", ""]
    total_all = 0
    total_in_way = 0
    for s in stocks:
        qty = int(s.get("total_qty", 0) or 0)
        in_way_to = int(s.get("total_in_way_to", 0) or 0)
        total_all += qty
        total_in_way += in_way_to
        art = s.get("supplier_article") or s.get("nm_id")
        subject = s.get("subject") or ""
        wh = int(s.get("warehouse_count", 0) or 0)
        lines.append(
            f"Артикул {art} ({subject[:30]}):"
        )
        lines.append(f"  Всего на складах: {qty} шт ({wh} складов)")
        if in_way_to > 0:
            lines.append(f"  В пути к клиенту: {in_way_to} шт")
        lines.append("")

    lines.insert(2, f"Всего: {total_all} шт | В пути: {total_in_way} шт")
    lines.insert(3, "")
    return "\n".join(lines).rstrip()


def build_reorder_message(data: dict) -> str:
    if "error" in data:
        return f"⚠️ {data['error']}"

    lines = [
        "💡 Рекомендация по закупке",
        "",
        f"Скорость продаж: {data['velocity']} шт/день (за 14 дней)",
        f"Текущие остатки: {data['total_stock']} шт",
    ]
    if data["stock_days_left"] != float("inf"):
        lines.append(f"Хватит на: {data['stock_days_left']:.1f} дн")

    lines.extend([
        "",
        "📊 Экономика закупки:",
        f"  Цена на рынке: {format_price_rub(data['market_min_price'])}",
        f"  С СПП (твой%) + кошельком: {format_price_rub(data['buy_price_with_spp'])}",
        f"  Цена продажи: {format_price_rub(data['sell_price'])}",
        f"  Прибыль/шт: {format_price_rub(data['profit_per_unit'])} ({data['margin_percent']}%)",
        "",
    ])

    if data["recommended_count"] > 0:
        lines.extend([
            f"🎯 Купи {data['recommended_count']} шт:",
            f"  Вложение: {format_price_rub(data['total_investment'])}",
            f"  Ожидаемая прибыль: {format_price_rub(data['expected_profit'])}",
            f"  ROI: {data['roi_percent']}% за {data['days_to_sell']} дней",
        ])
    else:
        lines.append("✅ Остатков достаточно, закупка не срочна")

    if not data.get("is_profitable"):
        lines.append("")
        lines.append("⚠️ Маржа ниже порога — пересчитай или пропусти")

    return "\n".join(lines)


def build_abc_message(abc: list[dict], days: int = 30) -> str:
    if not abc:
        return "Нет данных для ABC-анализа"
    lines = [f"📊 ABC-анализ (последние {days} дней)", ""]
    total_rev = sum(float(r.get("net_revenue", 0) or 0) for r in abc)
    cumulative = 0.0
    for i, item in enumerate(abc[:10], 1):
        rev = float(item.get("net_revenue", 0) or 0)
        pct = (rev / total_rev * 100) if total_rev else 0
        cumulative += pct
        marker = "A" if cumulative <= 80 else "B" if cumulative <= 95 else "C"
        art = item.get("supplier_article") or item.get("nm_id")
        returns = int(item.get("returns", 0) or 0)
        lines.append(
            f"{i}) [{marker}] {art} — {format_price_rub(rev)} ({pct:.1f}%, возвратов {returns})"
        )
    return "\n".join(lines)


def build_profit_message(data: dict, period_label: str) -> str:
    """Format profit breakdown into a Telegram message."""
    if data["total_sold"] == 0:
        return f"📊 Прибыль ({period_label})\n\nНет продаж за период."

    gross = data.get("gross_for_pay", 0)
    gross_total = data.get("gross_total_price", 0)
    uncov = data.get("uncovered_revenue", 0)
    tax_p = data.get("tax_percent", 2.0)
    log_u = data.get("logistics_per_unit", 188.0)
    acq_p = data.get("acquiring_percent", 0.0)

    matched = data.get("matched_sold", 0)
    unmatched = data.get("unmatched_sold", 0)

    lines = [
        f"💰 Чистая прибыль ({period_label})",
        "",
        f"Выкуплено: {data['total_sold']} шт",
    ]
    if matched and unmatched:
        lines.append(f"  закрыто финотчётом (точно): {matched} шт")
        lines.append(f"  в процессе закрытия (±2.6%): {unmatched} шт")
    elif matched and not unmatched:
        lines.append(f"  ✅ все закрыто финотчётом — точный расчёт")
    elif unmatched and not matched:
        lines.append(f"  ⚠️ нет финотчёта — оценка по /sales (±2.6%). Запусти /sync_finance")

    lines.extend([
        f"Возвратов: {data['total_returns']} шт",
        f"Цена продажи (price_with_disc): {format_price_rub(gross_total)}",
        f"После комиссии WB (ppvz_for_pay): {format_price_rub(gross)}",
        f"  - налог УСН {tax_p}%: -{format_price_rub(data.get('total_tax', 0))}",
        f"  - логистика: -{format_price_rub(data.get('total_logistics', 0))}",
    ])
    if acq_p > 0:
        lines.append(f"  - эквайринг {acq_p}%: -{format_price_rub(data.get('total_acquiring', 0))}")
    if uncov > 0:
        lines.append(f"  ⚠️ без закупок (исключено): {format_price_rub(uncov)}")

    if data["total_cost"] > 0:
        lines.append(f"Чистая выручка: {format_price_rub(data['total_revenue'])}")
        lines.append(f"Себестоимость закупки: {format_price_rub(data['total_cost'])}")
        lines.append("")
        profit = data["total_profit"]
        if profit > 0:
            lines.append(f"✅ Чистая прибыль: {format_price_rub(profit)}")
        else:
            lines.append(f"🔴 Убыток: {format_price_rub(profit)}")
        lines.append(f"Маржа от выручки WB: {data['margin_pct']}%")
        lines.append(f"ROI на вложения: {data['roi_pct']}%")
    else:
        lines.append("")
        lines.append("⚠️ Нет данных о закупках → невозможно посчитать прибыль")
        lines.append("Записывай через /buy <кол-во> <цена> <артикул>")

    # Per-article breakdown
    if data["breakdown"]:
        lines.append("")
        lines.append("📦 По артикулам:")
        for b in data["breakdown"][:10]:
            art = b.get("supplier_article") or f"nm{b.get('nm_id')}"
            sold = b["sold_qty"]
            if not b["has_purchase_data"]:
                lines.append(
                    f"  {art}: {sold} шт, {format_price_rub(b['revenue'])} "
                    f"(нет закупок)"
                )
            else:
                profit_str = format_price_rub(b["profit"])
                emoji = "✅" if b["profit"] > 0 else "🔴"
                lines.append(
                    f"  {emoji} {art}: {sold} шт × ~{format_price_rub(b['avg_buy_price'])} закупка → "
                    f"прибыль {profit_str} ({b['margin_pct']}%)"
                )

    if data.get("missing_purchase_data"):
        missing = data["missing_purchase_data"]
        if missing:
            lines.append("")
            lines.append(f"ℹ️ Нет закупок для: {', '.join(missing)}")
            lines.append(f"Добавь через /buy для точного расчёта")

    return "\n".join(lines)


def build_returns_message(returns: list[dict]) -> str:
    if not returns:
        return "🟢 Возвратов за период нет"
    lines = [f"🔄 Возвраты ({len(returns)})", ""]
    for r in returns[:15]:
        price = float(r.get("total_price", 0) or 0)
        for_pay = float(r.get("for_pay", 0) or 0)
        art = r.get("supplier_article") or r.get("nm_id")
        date = str(r.get("date", ""))[:10]
        lines.append(f"{date} | {art} | {format_price_rub(price)} | удержано {format_price_rub(abs(for_pay))}")
    return "\n".join(lines)
