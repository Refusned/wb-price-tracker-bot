"""
Formatters for business events: orders, sales, returns, briefing.

Все шаблоны здесь — parse_mode="HTML": жирные акценты на ключевых цифрах,
любые строки из WB (названия, склады, артикулы) экранируются html.escape().
Отправители обязаны передавать parse_mode="HTML".
"""
from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone

from app.services.insight_engine import BriefingData
from app.utils.formatting import format_iso_datetime, format_percent, format_price_rub, shorten

_MSK = timezone(timedelta(hours=3))


def _format_msk_short(iso_value: str | None) -> str | None:
    """ISO-время (UTC) → 'ДД.ММ ЧЧ:ММ' по МСК. None при пустом/битом значении."""
    if not iso_value:
        return None
    try:
        dt = datetime.fromisoformat(iso_value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_MSK).strftime("%d.%m %H:%M")


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def build_new_order_alert(order: dict) -> str:
    """Alert about a new order (not cancelled)."""
    lines = [
        "<b>📦 Новый заказ</b>",
        "",
        f"Артикул: <b>{_esc(order.get('supplier_article') or order.get('nm_id'))}</b>",
        f"Товар: {_esc(order.get('subject') or 'N/A')}",
        f"Цена: <b>{format_price_rub(order.get('price_with_disc', 0))}</b>",
    ]
    if order.get("total_price") and order.get("total_price") != order.get("price_with_disc"):
        lines.append(f"  Без скидки: {format_price_rub(order['total_price'])}")
    if order.get("spp_percent"):
        lines.append(f"  СПП: {format_percent(order['spp_percent'])}")
    if order.get("warehouse_name"):
        lines.append(f"Склад: {_esc(order['warehouse_name'])}")

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
        "<b>💰 Выкуп</b>",
        "",
        f"Артикул: <b>{_esc(sale.get('supplier_article') or sale.get('nm_id'))}</b>",
        f"Товар: {_esc(sale.get('subject') or 'N/A')}",
        f"После комиссии WB: <b>{format_price_rub(for_pay)}</b>",
        f"Цена продажи: {format_price_rub(sale.get('price_with_disc', 0))}",
    ]
    if sale.get("spp_percent"):
        lines.append(f"  СПП покупателя: {format_percent(sale['spp_percent'])}")
    if sale.get("commission_percent"):
        lines.append(f"  Комиссия WB: {format_percent(sale['commission_percent'])}")
    if sale.get("warehouse_name"):
        lines.append(f"Склад: {_esc(sale['warehouse_name'])}")

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
        "<b>🔄 Возврат</b>",
        "",
        f"Артикул: <b>{_esc(sale.get('supplier_article') or sale.get('nm_id'))}</b>",
        f"Товар: {_esc(sale.get('subject') or 'N/A')}",
        f"Вернули клиенту: {format_price_rub(sale.get('price_with_disc', 0))}",
        f"Удержано от твоей выплаты: <b>{format_price_rub(abs(for_pay))}</b>",
    ]
    if sale.get("warehouse_name"):
        lines.append(f"Склад: {_esc(sale['warehouse_name'])}")

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
    w = briefing.week

    lines = [
        "<b>☀️ Утренний брифинг</b>",
        "",
        "<b>📊 Вчера</b>",
        f"  Заказов: <b>{y.orders_count}</b> (отменено {y.orders_canceled})",
        f"  Выкуплено: <b>{y.sales_count} шт</b> (<b>{format_price_rub(y.revenue_net)}</b> к выплате)",
        f"  Возвратов: {y.returns_count}",
        f"  Выкупаемость: {format_percent(y.buyout_rate)}",
        "",
        "<b>📈 За 7 дней</b>",
        f"  Выкуплено: {w.sales_count} шт",
        f"  Возвратов: {w.returns_count}",
        f"  Чистая выручка: <b>{format_price_rub(w.revenue_net)}</b>",
        f"  Выкупаемость: {format_percent(w.buyout_rate)}",
        f"  Скорость: {briefing.velocity} шт/день",
        "",
        "<b>📦 Сейчас на складах</b>",
        f"  Остаток: <b>{briefing.total_stock} шт</b>",
    ]
    if briefing.in_way_to_client:
        lines.append(f"  В пути к клиенту: {briefing.in_way_to_client} шт")
    if briefing.velocity > 0 and briefing.days_left != float("inf"):
        lines.append(f"  Хватит на: <b>{briefing.days_left:.1f} дн</b>")
    else:
        lines.append("  Хватит на: неопределённо")
    synced = _format_msk_short(briefing.stock_synced_at)
    if synced:
        # Строку не разрывать тегами: на неё смотрит test_briefing_freshness.
        lines.append(f"  обновлено: {synced} МСК (= «Остаток» в кабинете на этот момент)")

    if briefing.market_min_price:
        lines.extend([
            "",
            "<b>🏪 Рынок</b>",
            f"  Минимальная цена конкурентов: <b>{format_price_rub(briefing.market_min_price)}</b>",
        ])

    if briefing.recommended_buy_count > 0:
        lines.extend([
            "",
            f"💡 Рекомендуемая закупка: <b>{briefing.recommended_buy_count} шт</b>",
            "   /reorder — детали",
        ])

    if briefing.insights:
        lines.append("")
        lines.append("<b>🎯 Инсайты</b>")
        for ins in briefing.insights:
            lines.append(f"<b>{ins.emoji} {_esc(ins.title)}</b>")
            if ins.body:
                lines.append(f"   {_esc(ins.body)}")
            if ins.action:
                lines.append(f"   → {_esc(ins.action)}")
            lines.append("")

    return "\n".join(lines).rstrip()


def build_period_metrics_message(label: str, metrics) -> str:
    lines = [
        f"<b>📊 {_esc(label)}</b>",
        "",
        f"Заказов: <b>{metrics.orders_count}</b> (отменено {metrics.orders_canceled})",
        f"Выкуплено: <b>{metrics.sales_count} шт</b>",
        f"Возвратов: {metrics.returns_count}",
        f"Выручка брутто: {format_price_rub(metrics.revenue_total)}",
        f"К выплате (с учётом возвратов): <b>{format_price_rub(metrics.revenue_net)}</b>",
        f"Выкупаемость: {format_percent(metrics.buyout_rate)}",
    ]
    if metrics.unique_articles:
        lines.append(f"Уникальных артикулов: {metrics.unique_articles}")
    return "\n".join(lines)


def build_stock_message(
    stocks: list[dict],
    *,
    last_sync_at: str | None = None,
    fbo_count: int | None = None,
    fbs_count: int | None = None,
) -> str:
    if not stocks:
        return (
            "📦 Остатков нет.\n"
            "Жду следующий скан Seller API — обновляется каждые 30 мин."
        )

    lines = ["<b>📦 Остатки по складам WB</b>", ""]
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
        lines.append(f"<b>{_esc(art)}</b> ({_esc(shorten(subject, 30))})")
        lines.append(f"  Всего на складах: <b>{qty} шт</b> ({wh} складов)")
        if in_way_to > 0:
            lines.append(f"  В пути к клиенту: {in_way_to} шт")
        lines.append("")

    lines.insert(2, f"Всего: <b>{total_all} шт</b> · В пути: {total_in_way} шт")
    lines.insert(3, "")
    text = "\n".join(lines).rstrip()

    # Футер диагностики свежести (если planner передал метаданные синхронизации).
    # Метки «🔄 Синхронизация»/«FBO»/«FBS»/«артикулов» — контракт test_stock_footer.
    if last_sync_at is not None or fbo_count is not None or fbs_count is not None:
        src: list[str] = []
        if fbo_count is not None:
            src.append(f"FBO {fbo_count}")
        if fbs_count is not None:
            src.append(f"FBS {fbs_count}")
        src.append(f"артикулов {len(stocks)}")
        text += (
            f"\n\n🔄 Синхронизация: {format_iso_datetime(last_sync_at)}"
            f"\nИсточники: {' · '.join(src)}"
        )
    return text


def build_reorder_message(data: dict) -> str:
    if "error" in data:
        return f"⚠️ {_esc(data['error'])}"

    lines = [
        "<b>💡 Рекомендация по закупке</b>",
        "",
        f"Скорость продаж: {data['velocity']} шт/день (за 14 дней)",
        f"Текущие остатки: <b>{data['total_stock']} шт</b>",
    ]
    if data["stock_days_left"] != float("inf"):
        lines.append(f"Хватит на: <b>{data['stock_days_left']:.1f} дн</b>")

    lines.extend([
        "",
        "<b>📊 Экономика закупки</b>",
        f"  Цена на рынке: {format_price_rub(data['market_min_price'])}",
        f"  С СПП (твой%) + кошельком: {format_price_rub(data['buy_price_with_spp'])}",
        f"  Цена продажи: {format_price_rub(data['sell_price'])}",
        f"  Прибыль/шт: <b>{format_price_rub(data['profit_per_unit'])}</b> ({format_percent(data['margin_percent'])})",
        "",
    ])

    if data["recommended_count"] > 0:
        lines.extend([
            f"🎯 Купи <b>{data['recommended_count']} шт</b>:",
            f"  Вложение: {format_price_rub(data['total_investment'])}",
            f"  Ожидаемая прибыль: <b>{format_price_rub(data['expected_profit'])}</b>",
            f"  ROI: {format_percent(data['roi_percent'])} за {data['days_to_sell']} дней",
        ])
    else:
        lines.append("✅ Остатков достаточно, закупка не срочна")

    if not data.get("is_profitable"):
        lines.append("")
        lines.append("⚠️ Маржа ниже порога — пересчитай или пропусти")

    return "\n".join(lines)


def build_abc_message(abc: list[dict], days: int = 30) -> str:
    if not abc:
        return "📊 Данных для ABC-анализа пока нет. Появятся продажи — появится и анализ."
    lines = [f"<b>📊 ABC-анализ</b> (последние {days} дней)", ""]
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
            f"{i}) [{marker}] <b>{_esc(art)}</b> — {format_price_rub(rev)} "
            f"({pct:.1f}%, возвратов {returns})"
        )
    return "\n".join(lines)


def build_profit_message(data: dict, period_label: str) -> str:
    """Format profit breakdown into a Telegram message."""
    if data["total_sold"] == 0:
        return (
            f"📊 Прибыль ({_esc(period_label)})\n\n"
            "Продаж за период нет. Загляни в /today или запусти /sync_finance."
        )

    gross = data.get("gross_for_pay", 0)
    gross_total = data.get("gross_total_price", 0)
    uncov = data.get("uncovered_revenue", 0)
    tax_p = data.get("tax_percent", 2.0)
    acq_p = data.get("acquiring_percent", 0.0)

    matched = data.get("matched_sold", 0)
    unmatched = data.get("unmatched_sold", 0)

    lines = [
        f"<b>💰 Чистая прибыль ({_esc(period_label)})</b>",
        "",
        f"Выкуплено: <b>{data['total_sold']} шт</b>",
    ]
    if matched and unmatched:
        lines.append(f"  закрыто финотчётом (точно): {matched} шт")
        lines.append(f"  в процессе закрытия (±2.6%): {unmatched} шт")
    elif matched and not unmatched:
        lines.append("  ✅ все закрыто финотчётом — точный расчёт")
    elif unmatched and not matched:
        lines.append("  ⚠️ нет финотчёта — оценка по /sales (±2.6%). Запусти /sync_finance")

    lines.extend([
        f"Возвратов: {data['total_returns']} шт",
        f"Цена продажи (price_with_disc): {format_price_rub(gross_total)}",
        f"После комиссии WB (ppvz_for_pay): {format_price_rub(gross)}",
        f"  − налог УСН {format_percent(tax_p)}: −{format_price_rub(data.get('total_tax', 0))}",
        f"  − логистика: −{format_price_rub(data.get('total_logistics', 0))}",
    ])
    if acq_p > 0:
        lines.append(f"  − эквайринг {format_percent(acq_p)}: −{format_price_rub(data.get('total_acquiring', 0))}")
    if uncov > 0:
        lines.append(f"  ⚠️ без закупок (исключено): {format_price_rub(uncov)}")

    if data["total_cost"] > 0:
        lines.append(f"Чистая выручка: {format_price_rub(data['total_revenue'])}")
        lines.append(f"Себестоимость закупки: {format_price_rub(data['total_cost'])}")
        lines.append("━━━━━━━━━━━━━━━")
        profit = data["total_profit"]
        if profit > 0:
            lines.append(f"✅ <b>Чистая прибыль: {format_price_rub(profit)}</b>")
        else:
            lines.append(f"🔴 <b>Убыток: {format_price_rub(profit)}</b>")
        lines.append(f"Маржа от выручки WB: <b>{format_percent(data['margin_pct'])}</b>")
        lines.append(f"ROI на вложения: {format_percent(data['roi_pct'])}")
    else:
        lines.append("")
        lines.append("⚠️ Нет данных о закупках → невозможно посчитать прибыль")
        lines.append("Записывай через /buy (пример: <code>/buy 20 9500 019</code>)")

    # Per-article breakdown
    if data["breakdown"]:
        lines.append("")
        lines.append("<b>📦 По артикулам</b>")
        for b in data["breakdown"][:10]:
            art = _esc(b.get("supplier_article") or f"nm{b.get('nm_id')}")
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
                    f"  {emoji} <b>{art}</b>: {sold} шт × ~{format_price_rub(b['avg_buy_price'])} закупка → "
                    f"прибыль <b>{profit_str}</b> ({format_percent(b['margin_pct'])})"
                )

    if data.get("missing_purchase_data"):
        missing = data["missing_purchase_data"]
        if missing:
            lines.append("")
            lines.append(f"ℹ️ Нет закупок для: {_esc(', '.join(str(m) for m in missing))}")
            lines.append("Добавь через /buy для точного расчёта")

    return "\n".join(lines)


def build_returns_message(returns: list[dict]) -> str:
    if not returns:
        return "🟢 Возвратов за период нет"
    lines = [f"<b>🔄 Возвраты ({len(returns)})</b>", ""]
    for r in returns[:15]:
        price = float(r.get("total_price", 0) or 0)
        for_pay = float(r.get("for_pay", 0) or 0)
        art = r.get("supplier_article") or r.get("nm_id")
        date = str(r.get("date", ""))[:10]
        lines.append(
            f"{date} · <b>{_esc(art)}</b> · {format_price_rub(price)} · "
            f"удержано {format_price_rub(abs(for_pay))}"
        )
    return "\n".join(lines)
