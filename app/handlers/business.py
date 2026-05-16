"""
Handlers for business analytics commands: /today, /yesterday, /week, /month,
/stock, /reorder, /cashflow, /funnel, /returns, /abc, /finances, /briefing,
/addpurchase, /purchases.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.config import AppConfig
from app.scheduler import WbUpdateScheduler
from app.services.insight_engine import InsightEngine
from app.storage.business_repository import BusinessRepository
from app.storage.decision_snapshot_repository import DecisionSnapshotRepository
from app.storage.repositories import SettingsRepository, SubscriberRepository
from app.utils.business_formatting import (
    build_abc_message,
    build_briefing_message,
    build_period_metrics_message,
    build_profit_message,
    build_reorder_message,
    build_returns_message,
    build_stock_message,
)
from app.utils.formatting import format_price_rub

from .common import ensure_allowed, remember_subscriber


def get_router(
    config: AppConfig,
    business_repository: BusinessRepository,
    settings_repository: SettingsRepository,
    subscriber_repository: SubscriberRepository,
    insight_engine: InsightEngine,
    updater: WbUpdateScheduler,
    decision_snapshot_repo: DecisionSnapshotRepository | None = None,
) -> Router:
    router = Router(name="business")

    @router.message(Command("today"))
    async def today_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        metrics = await business_repository.get_daily_metrics(today)
        await message.answer(build_period_metrics_message("Сегодня", metrics))

    @router.message(Command("yesterday"))
    async def yesterday_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)
        y = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        metrics = await business_repository.get_daily_metrics(y)
        await message.answer(build_period_metrics_message("Вчера", metrics))

    @router.message(Command("week"))
    async def week_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)
        metrics = await business_repository.get_period_metrics(7)
        await message.answer(build_period_metrics_message("За 7 дней", metrics))

    @router.message(Command("month"))
    async def month_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)
        metrics = await business_repository.get_period_metrics(30)
        await message.answer(build_period_metrics_message("За 30 дней", metrics))

    @router.message(Command("stock"))
    async def stock_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)
        stocks = await business_repository.get_stock_summary()
        await message.answer(build_stock_message(stocks))

    @router.message(Command("stock_fbs"))
    async def stock_fbs_handler(message: Message) -> None:
        """Принудительно запросить FBS остатки через marketplace API."""
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)
        await message.answer("🔎 Запрашиваю FBS остатки через marketplace-api...")
        fbs = await updater._seller_client.get_all_fbs_stocks()
        if not fbs:
            await message.answer(
                "❌ FBS пуст или API не ответил.\n"
                "Проверь: 1) есть ли у тебя свой склад в WB кабинете (раздел 'Маркетплейс')\n"
                "2) есть ли товары с вазначенными остатками на FBS"
            )
            return
        lines = [f"📦 FBS остатки ({len(fbs)} записей)", ""]
        total = 0
        for s in fbs[:30]:
            total += s.quantity
            lines.append(f"  {s.supplier_article or s.nm_id} ({s.warehouse_name}): {s.quantity} шт")
        lines.append("")
        lines.append(f"Итого: {sum(s.quantity for s in fbs)} шт на {len({s.warehouse_name for s in fbs})} FBS складах")
        from datetime import datetime, timezone
        await business_repository.upsert_stocks(fbs, datetime.now(timezone.utc).isoformat())
        lines.append("\n✅ Записано в БД. /stock покажет вместе с FBO.")
        await message.answer("\n".join(lines))

    @router.message(Command("reorder"))
    async def reorder_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)
        data = await insight_engine.get_reorder_recommendation()
        await message.answer(build_reorder_message(data))

    @router.message(Command("briefing"))
    async def briefing_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)
        briefing = await insight_engine.build_briefing()
        await message.answer(build_briefing_message(briefing))

    @router.message(Command("returns"))
    async def returns_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)
        returns = await business_repository.get_returns(days=30, limit=20)
        await message.answer(build_returns_message(returns))

    @router.message(Command("abc"))
    async def abc_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)
        abc = await business_repository.get_abc_analysis(days=30)
        await message.answer(build_abc_message(abc, days=30))

    @router.message(Command("cashflow"))
    async def cashflow_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)
        # Approximation: WB pays weekly (Fri). Calculate pending payout.
        week_metrics = await business_repository.get_period_metrics(7)
        invested_7d = await business_repository.total_invested_last(7)
        today = datetime.now(timezone.utc)
        days_to_friday = (4 - today.weekday()) % 7
        if days_to_friday == 0:
            days_to_friday = 7

        lines = [
            "💰 Кэшфлоу",
            "",
            f"К выплате (за последние 7 дн): {format_price_rub(week_metrics.revenue_net)}",
            f"Ожидается выплата через: {days_to_friday} дн (~пятница)",
            "",
            f"Инвестировано в закупки (7д): {format_price_rub(invested_7d)}",
        ]
        net_flow = week_metrics.revenue_net - invested_7d
        lines.append(f"Чистый поток (7д): {format_price_rub(net_flow)}")
        if net_flow > 0:
            lines.append("✅ Бизнес приносит деньги")
        else:
            lines.append("⚠️ Расходы превышают доходы — проверь закупки")
        await message.answer("\n".join(lines))

    @router.message(Command("rescan_seller"))
    async def rescan_seller_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)
        await message.answer("Запуск ручного обновления Seller API...")
        ok = await updater.seller_update_once(notify=True)
        if ok:
            await message.answer("✅ Готово. Проверь /today или /stock")
        else:
            await message.answer("⚠️ Ошибка обновления. Смотри логи.")

    @router.message(Command("sync_finance"))
    async def sync_finance_handler(message: Message, command: CommandObject) -> None:
        """Pull /api/v5/supplier/reportDetailByPeriod — real logistics, returns, storage."""
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)
        args = (command.args or "").strip()
        days = int(args) if args.isdigit() else 30
        days = min(days, 180)
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        await message.answer(f"💼 Тяну финотчёт WB за {days} дн...")
        rows = await updater._seller_client.get_financial_report(now - timedelta(days=days), now)
        n = await business_repository.upsert_finance_journal(rows, now.isoformat())
        await message.answer(f"✅ Записал {n} строк в finance_journal. Проверь /finance")

    @router.message(Command("finance"))
    async def finance_handler(message: Message, command: CommandObject) -> None:
        if not await ensure_allowed(message, config):
            return
        args = (command.args or "").strip()
        days = int(args) if args.isdigit() else 30
        s = await business_repository.get_finance_summary(days)
        if not s.get("rows_count"):
            await message.answer(
                f"📊 Финотчёт пустой за {days} дн.\nЗапусти /sync_finance {days}"
            )
            return
        sales_net = float(s.get("sales_for_pay") or 0) + float(s.get("returns_for_pay") or 0)
        lines = [
            f"💼 Финотчёт WB ({days} дн)",
            "",
            f"Продаж: {int(s.get('sales_count') or 0)} | Возвратов: {int(s.get('returns_count') or 0)}",
            f"Выручка к выплате: {format_price_rub(sales_net)}",
            f"  продажи: {format_price_rub(s.get('sales_for_pay') or 0)}",
            f"  возвраты: {format_price_rub(s.get('returns_for_pay') or 0)}",
            "",
            "📦 Удержания WB:",
            f"  Логистика: {format_price_rub(s.get('total_logistics') or 0)}",
            f"  Хранение: {format_price_rub(s.get('total_storage') or 0)}",
            f"  Штрафы: {format_price_rub(s.get('total_penalty') or 0)}",
            f"  Приёмка: {format_price_rub(s.get('total_acceptance') or 0)}",
            f"  Удержания: {format_price_rub(s.get('total_deduction') or 0)}",
            f"  Доп.платежи: {format_price_rub(s.get('total_additional') or 0)}",
            "",
            f"Всего строк: {int(s.get('rows_count') or 0)}",
        ]
        # Real logistics per unit
        if s.get("sales_count"):
            real_log = float(s["total_logistics"] or 0) / int(s["sales_count"])
            lines.append(f"\n💡 Реальная логистика: {real_log:.1f} ₽/продажу")
            lines.append(f"   /setlogistics {real_log:.0f} — обновить")
        await message.answer("\n".join(lines))

    @router.message(Command("resync_history"))
    async def resync_history_handler(message: Message, command: CommandObject) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)
        args = (command.args or "").strip()
        days = 90
        if args.isdigit():
            days = min(int(args), 365)
        await message.answer(
            f"🔄 Синхронизация истории за {days} дн... "
            f"Уведомлений не будет (история). Займёт 30-60 сек."
        )
        ok = await updater.seller_update_once(notify=False, days_back=days)
        if ok:
            sales = await business_repository.count_sales()
            orders = await business_repository.count_orders()
            await message.answer(
                f"✅ Готово. В базе: {orders} заказов, {sales} продаж.\n"
                f"Проверь /week /month /profit"
            )
        else:
            await message.answer("⚠️ Ошибка. Смотри логи.")

    async def _record_purchase(message: Message, command: CommandObject) -> None:
        """Shared logic for /buy and /addpurchase."""
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)
        args = (command.args or "").strip().split()
        if len(args) < 2:
            await message.answer(
                "Использование: /buy <кол-во> <цена/шт> <артикул>\n"
                "Пример: /buy 20 9500 019\n"
                "Артикул (supplier_article) — как в твоём кабинете WB (019, 020, 22).\n"
                "Можно также передать nm_id (длинное число)."
            )
            return
        try:
            qty = int(args[0])
            price = float(args[1])
        except ValueError:
            await message.answer("Количество и цена должны быть числами. Пример: /buy 20 9500 019")
            return
        if qty <= 0 or price <= 0:
            await message.answer("Количество и цена должны быть > 0")
            return

        article = args[2] if len(args) > 2 else None
        nm_id = None
        # Если длинное число — это nm_id (обычно 9+ цифр)
        if article and article.isdigit() and len(article) >= 7:
            nm_id = int(article)
            article = None

        spp = await settings_repository.get_float("spp_percent", 24.0)
        pid = await business_repository.add_purchase(
            nm_id=nm_id,
            supplier_article=article,
            quantity=qty,
            buy_price_per_unit=price,
            spp_at_purchase=spp,
            notes=None,
        )

        # Day 16: link to recent decision_snapshot if available.
        # Only when nm_id known directly. For supplier_article-only purchases
        # the user can re-run with explicit nm_id later if linking matters.
        linked_snapshot_id: int | None = None
        if decision_snapshot_repo is not None and nm_id is not None:
            try:
                snap = await decision_snapshot_repo.find_most_recent_unlinked(
                    nm_id=int(nm_id), within_seconds=86400,
                )
                if snap is not None:
                    await decision_snapshot_repo.link_to_purchase(
                        snapshot_id=int(snap["id"]),
                        purchase_id=int(pid),
                        action="bought",
                    )
                    linked_snapshot_id = int(snap["id"])
            except Exception:
                pass  # linking is best-effort, don't block purchase

        total = qty * price
        ref = article or (f"nm{nm_id}" if nm_id else "—")
        link_note = f"\n🔗 Связан с decision #{linked_snapshot_id}" if linked_snapshot_id else ""
        await message.answer(
            f"✅ Закупка #{pid} записана\n\n"
            f"Артикул: {ref}\n"
            f"Количество: {qty} шт\n"
            f"Цена/шт: {format_price_rub(price)}\n"
            f"Всего: {format_price_rub(total)}\n"
            f"СПП на момент: {spp}%"
            f"{link_note}\n\n"
            f"💡 Прибыль по артикулу: /profit"
        )

    @router.message(Command("buy"))
    async def buy_handler(message: Message, command: CommandObject) -> None:
        await _record_purchase(message, command)

    @router.message(Command("addpurchase"))
    async def addpurchase_handler(message: Message, command: CommandObject) -> None:
        # Alias for backward compatibility
        await _record_purchase(message, command)

    @router.message(Command("profit"))
    async def profit_handler(message: Message, command: CommandObject) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)

        args = (command.args or "").strip().lower()
        if args == "today":
            period_days = 1
            label = "сегодня"
        elif args == "yesterday":
            period_days = 2  # today и вчера — отфильтруем
            label = "вчера"
        elif args == "week" or args == "":
            period_days = 7
            label = "7 дней"
        elif args == "month":
            period_days = 30
            label = "30 дней"
        elif args == "all":
            period_days = None
            label = "всё время"
        elif args.isdigit():
            period_days = int(args)
            label = f"{period_days} дн"
        else:
            await message.answer(
                "Использование:\n"
                "/profit [today|yesterday|week|month|all|число_дней]\n\n"
                "Примеры:\n"
                "  /profit — за 7 дней (по умолчанию)\n"
                "  /profit today — за сегодня\n"
                "  /profit month — за 30 дней\n"
                "  /profit all — за всё время\n"
                "  /profit 3 — за 3 дня"
            )
            return

        tax_p = await settings_repository.get_float("profit_tax_percent", config.profit_tax_percent)
        log_u = await settings_repository.get_float("profit_logistics_per_unit_rub", config.profit_logistics_per_unit_rub)
        acq_p = await settings_repository.get_float("profit_acquiring_percent", config.profit_acquiring_percent)
        data = await business_repository.get_total_profit(
            days=period_days,
            tax_percent=tax_p,
            logistics_per_unit_rub=log_u,
            acquiring_percent=acq_p,
        )
        await message.answer(build_profit_message(data, label))

    @router.message(Command("settax"))
    async def settax_handler(message: Message, command: CommandObject) -> None:
        if not await ensure_allowed(message, config):
            return
        args = (command.args or "").strip()
        try:
            val = float(args.replace(",", "."))
            if val < 0 or val > 50:
                raise ValueError
        except ValueError:
            await message.answer("Использование: /settax <процент>\nПример: /settax 2 (УСН 2%)")
            return
        await settings_repository.set_float("profit_tax_percent", val)
        await message.answer(f"✅ Налог УСН установлен: {val}%")

    @router.message(Command("setlogistics"))
    async def setlogistics_handler(message: Message, command: CommandObject) -> None:
        if not await ensure_allowed(message, config):
            return
        args = (command.args or "").strip()
        try:
            val = float(args.replace(",", "."))
            if val < 0 or val > 1000:
                raise ValueError
        except ValueError:
            await message.answer("Использование: /setlogistics <₽/шт>\nПример: /setlogistics 60")
            return
        await settings_repository.set_float("profit_logistics_per_unit_rub", val)
        await message.answer(f"✅ Логистика установлена: {val} ₽/шт")

    @router.message(Command("setacquiring"))
    async def setacquiring_handler(message: Message, command: CommandObject) -> None:
        if not await ensure_allowed(message, config):
            return
        args = (command.args or "").strip()
        try:
            val = float(args.replace(",", "."))
            if val < 0 or val > 10:
                raise ValueError
        except ValueError:
            await message.answer("Использование: /setacquiring <процент>\nПример: /setacquiring 0 (выкл)")
            return
        await settings_repository.set_float("profit_acquiring_percent", val)
        await message.answer(f"✅ Эквайринг установлен: {val}%")

    @router.message(Command("profitcosts"))
    async def profitcosts_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        tax_p = await settings_repository.get_float("profit_tax_percent", config.profit_tax_percent)
        log_u = await settings_repository.get_float("profit_logistics_per_unit_rub", config.profit_logistics_per_unit_rub)
        acq_p = await settings_repository.get_float("profit_acquiring_percent", config.profit_acquiring_percent)
        lines = [
            "📊 Параметры расчёта прибыли",
            "",
            f"Налог УСН: {tax_p}%",
            f"Логистика FBS: {log_u:.0f} ₽/шт",
            f"Эквайринг: {acq_p}%",
            "",
            "Команды:",
            "/settax <%>",
            "/setlogistics <₽/шт>",
            "/setacquiring <%>",
        ]
        await message.answer("\n".join(lines))

    @router.message(Command("purchases"))
    async def purchases_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)
        purchases = await business_repository.list_purchases(days=30)
        if not purchases:
            await message.answer(
                "Нет записей о закупках за 30 дней.\n"
                "Добавь через /addpurchase <кол-во> <цена>"
            )
            return
        total_qty = sum(p.get("quantity", 0) for p in purchases)
        total_cost = sum(float(p.get("total_cost", 0) or 0) for p in purchases)
        lines = [f"📝 Закупки за 30 дней: {total_qty} шт на {format_price_rub(total_cost)}", ""]
        for p in purchases[:15]:
            date = str(p.get("date", ""))[:10]
            qty = p.get("quantity", 0)
            price = float(p.get("buy_price_per_unit", 0) or 0)
            art = p.get("supplier_article") or p.get("nm_id") or "—"
            lines.append(f"{date} | {qty} шт × {format_price_rub(price)} | {art}")
        await message.answer("\n".join(lines))

    return router
