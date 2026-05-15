from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot

from app.config import AppConfig
from app.storage.models import PriceDropEvent
from app.services.insight_engine import InsightEngine
from app.services.margin_calculator import MarginCalculator
from app.storage.business_repository import BusinessRepository
from app.storage.repositories import (
    ItemRepository,
    MetaRepository,
    PriceHistoryRepository,
    PriceStatsRepository,
    SettingsRepository,
    SubscriberRepository,
    TrackedArticleRepository,
)
from app.utils.business_formatting import (
    build_briefing_message,
    build_new_order_alert,
    build_new_sale_alert,
    build_new_return_alert,
)
from app.utils.formatting import build_price_drop_alert_message
from app.wb.client import WildberriesClient
from app.wb.seller_client import SellerClient

_HEALTH_ALERT_THRESHOLD = 3


class WbUpdateScheduler:
    def __init__(
        self,
        config: AppConfig,
        wb_client: WildberriesClient,
        bot: Bot,
        item_repository: ItemRepository,
        meta_repository: MetaRepository,
        settings_repository: SettingsRepository,
        subscriber_repository: SubscriberRepository,
        price_stats_repository: PriceStatsRepository,
        price_history_repository: PriceHistoryRepository,
        tracked_article_repository: TrackedArticleRepository,
        business_repository: BusinessRepository | None = None,
        seller_client: SellerClient | None = None,
        insight_engine: InsightEngine | None = None,
    ) -> None:
        self._config = config
        self._wb_client = wb_client
        self._bot = bot
        self._item_repository = item_repository
        self._meta_repository = meta_repository
        self._settings_repository = settings_repository
        self._subscriber_repository = subscriber_repository
        self._price_stats_repository = price_stats_repository
        self._price_history_repository = price_history_repository
        self._tracked_article_repository = tracked_article_repository
        self._business_repository = business_repository
        self._seller_client = seller_client
        self._insight_engine = insight_engine

        self._logger = logging.getLogger(self.__class__.__name__)
        self._task: asyncio.Task | None = None
        self._seller_task: asyncio.Task | None = None
        self._briefing_task: asyncio.Task | None = None
        self._manual_update_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._update_lock = asyncio.Lock()
        self._seller_lock = asyncio.Lock()
        self._consecutive_failures: int = 0
        self._health_alert_sent: bool = False
        self._scan_count: int = 0
        self._last_briefing_date: str | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="wb-updater")
        if self._seller_client and self._business_repository:
            self._seller_task = asyncio.create_task(self._run_seller_loop(), name="seller-updater")
            self._briefing_task = asyncio.create_task(self._run_briefing_loop(), name="briefing")

    async def stop(self) -> None:
        self._stop_event.set()
        for task_attr in ("_task", "_seller_task", "_briefing_task"):
            task = getattr(self, task_attr)
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            setattr(self, task_attr, None)

    def trigger_background_update(self, reason: str = "manual") -> None:
        if self._update_lock.locked():
            return
        if self._manual_update_task is not None and not self._manual_update_task.done():
            return
        self._manual_update_task = asyncio.create_task(self.update_once(reason=reason))

    async def update_once(self, reason: str = "manual") -> bool:
        async with self._update_lock:
            attempt_at = datetime.now(timezone.utc).isoformat()
            await self._meta_repository.set_value("last_update_attempt_at", attempt_at)
            self._scan_count += 1

            try:
                # Step 1: Seed tracked articles from existing items on first run
                await self._tracked_article_repository.seed_from_items(attempt_at)

                # Step 2: Batch-fetch ALL tracked articles via card API (reliable)
                tracked_nm_ids = await self._tracked_article_repository.get_active_nm_ids()
                card_items: list = []
                if tracked_nm_ids:
                    card_items = await self._wb_client.fetch_cards_batch(tracked_nm_ids)
                    # Update tracking: mark found, increment misses for missing
                    found_ids = {item.nm_id for item in card_items}
                    missing_ids = set(tracked_nm_ids) - found_ids
                    if card_items:
                        await self._tracked_article_repository.upsert_articles(card_items, attempt_at)
                    if missing_ids:
                        await self._tracked_article_repository.increment_misses(missing_ids)

                # Step 3: Search for NEW articles. Every 3rd scan (~9 min at 3-min interval).
                # Manual /rescan и startup всегда форсируют search.
                search_items: list = []
                run_search = (
                    (self._scan_count % 3 == 1)
                    or not tracked_nm_ids
                    or reason in {"startup", "manual", "manual_rescan", "find_deal"}
                )
                if run_search:
                    wb_queries = self._config.wb_queries
                    try:
                        search_items = await self._wb_client.search_across_queries(
                            queries=wb_queries,
                            max_pages=self._config.wb_max_pages,
                        )
                        # Add newly discovered articles to tracking
                        if search_items:
                            new_count = await self._tracked_article_repository.upsert_articles(
                                search_items, attempt_at
                            )
                            if new_count > 0:
                                self._logger.info("Discovered %d new articles via search", new_count)
                    except Exception as search_exc:
                        self._logger.warning("Search failed (non-fatal): %s", search_exc)
                        # Search failure is OK — we have card data

                # Step 4: Merge results (card data takes priority)
                merged: dict[str, any] = {}
                for item in search_items:
                    merged[item.nm_id] = item
                for item in card_items:  # card overwrites search (more accurate)
                    merged[item.nm_id] = item
                items = list(merged.values())

                existing_count = await self._item_repository.count_items()
                if not items and existing_count > 0:
                    raise RuntimeError("Both card and search returned empty, preserving existing cache")

                updated_at = datetime.now(timezone.utc).isoformat()
                min_price_rub = await self._settings_repository.get_min_price_rub(
                    self._config.min_price_rub
                )

                # Compute top-20 for alerts + rank_map for display in alert message
                top_sorted = sorted(
                    [i for i in items if i.in_stock and i.price_rub >= float(min_price_rub)],
                    key=lambda i: i.price_rub,
                )
                alert_nm_ids = {i.nm_id for i in top_sorted[:20]}
                rank_map = {item.nm_id: idx + 1 for idx, item in enumerate(top_sorted)}

                alert_cooldown = int(await self._settings_repository.get_float("alert_cooldown_minutes", 30.0))

                drop_events = await self._price_stats_repository.record_snapshot_and_collect_drops(
                    items,
                    alert_nm_ids=alert_nm_ids,
                    drop_threshold_percent=self._config.alert_drop_percent,
                    observed_at=updated_at,
                    rank_map=rank_map,
                    max_events=self._config.alert_max_items_per_cycle,
                    alert_cooldown_minutes=alert_cooldown,
                    stale_data_hours=1,
                )
                await self._item_repository.replace_all(items, updated_at)

                await self._price_history_repository.record_snapshot(items, updated_at)
                await self._price_history_repository.cleanup_old(
                    days=self._config.price_history_retention_days
                )

                delivered_alerts = 0
                if self._config.alerts_enabled and drop_events:
                    delivered_alerts = await self._send_price_drop_alerts(drop_events, updated_at)

                self._consecutive_failures = 0
                self._health_alert_sent = False
                tracked_count = await self._tracked_article_repository.count_active()
                await self._meta_repository.set_value("last_success_update_at", updated_at)
                await self._meta_repository.set_value("last_update_status", "success")
                await self._meta_repository.set_value("last_update_count", str(len(items)))
                await self._meta_repository.set_value("last_alerts_sent", str(delivered_alerts))
                await self._meta_repository.set_value("last_update_reason", reason)
                await self._meta_repository.set_value("last_update_error", "")
                await self._meta_repository.set_value("last_tracked_count", str(tracked_count))
                search_status = "ran" if run_search else "skipped"
                await self._meta_repository.set_value("last_search_status", search_status)

                self._logger.info(
                    "WB update completed, reason=%s, items=%s, card=%s, search=%s, tracked=%s",
                    reason,
                    len(items),
                    len(card_items),
                    len(search_items) if run_search else "skipped",
                    tracked_count,
                )
                return True
            except Exception as exc:
                failed_at = datetime.now(timezone.utc).isoformat()
                await self._meta_repository.set_value("last_update_status", "error")
                await self._meta_repository.set_value("last_update_error", str(exc))
                await self._meta_repository.set_value("last_error_at", failed_at)
                await self._meta_repository.set_value("last_update_reason", reason)

                self._consecutive_failures += 1
                self._logger.exception("WB update failed, reason=%s, consecutive=%s", reason, self._consecutive_failures)

                if self._consecutive_failures >= _HEALTH_ALERT_THRESHOLD and not self._health_alert_sent:
                    await self._send_health_alert(str(exc))
                    self._health_alert_sent = True

                return False

    async def _build_margin_calculator(self) -> tuple[MarginCalculator, float, int]:
        cfg = self._config
        sr = self._settings_repository
        sell_price = await sr.get_float("sell_price_rub", cfg.sell_price_rub)
        batch_size = int(await sr.get_float("batch_size", float(cfg.batch_size)))
        calculator = MarginCalculator(
            spp_percent=await sr.get_float("spp_percent", cfg.spp_percent),
            wb_commission_percent=await sr.get_float("wb_commission_percent", cfg.wb_commission_percent),
            logistics_cost_rub=await sr.get_float("logistics_cost_rub", cfg.logistics_cost_rub),
            storage_cost_per_day_rub=await sr.get_float("storage_cost_per_day_rub", cfg.storage_cost_per_day_rub),
            return_rate_percent=await sr.get_float("return_rate_percent", cfg.return_rate_percent),
            target_margin_percent=await sr.get_float("target_margin_percent", cfg.target_margin_percent),
        )
        return calculator, sell_price, batch_size

    async def _send_price_drop_alerts(
        self,
        events: list[PriceDropEvent],
        updated_at: str,
    ) -> int:
        chat_ids = await self._subscriber_repository.list_active_chat_ids()
        if not chat_ids:
            return 0

        calculator, sell_price, batch_size = await self._build_margin_calculator()

        deliveries = 0
        for event in events:
            margin = None
            if sell_price > 0:
                try:
                    margin = calculator.calculate(event.new_price_rub, sell_price)
                except ValueError:
                    pass

            text = build_price_drop_alert_message(
                query=self._config.wb_query,
                updated_at_iso=updated_at,
                event=event,
                margin=margin,
                batch_size=batch_size,
            )
            for chat_id in chat_ids:
                try:
                    await self._bot.send_message(chat_id=chat_id, text=text)
                    deliveries += 1
                except Exception as exc:
                    self._logger.warning(
                        "Failed to send price-drop alert to chat_id=%s, nm_id=%s: %s",
                        chat_id,
                        event.nm_id,
                        exc,
                    )
                await asyncio.sleep(0.05)

        return deliveries

    async def _send_health_alert(self, error_text: str) -> None:
        chat_ids = await self._subscriber_repository.list_active_chat_ids()
        if not chat_ids:
            return

        last_success = await self._meta_repository.get_value("last_success_update_at") or "неизвестно"
        text = (
            f"API WB не отвечает уже {self._consecutive_failures} цикла(ов) подряд\n"
            f"Последняя ошибка: {error_text[:200]}\n"
            f"Последний успешный скан: {last_success}\n"
            "Бот продолжает попытки, но цены могут быть устаревшими."
        )
        for chat_id in chat_ids:
            try:
                await self._bot.send_message(chat_id=chat_id, text=text)
            except Exception:
                pass

    async def _run(self) -> None:
        await self.update_once(reason="startup")

        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._config.wb_poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                await self.update_once(reason="polling")

    # ---------- Seller API polling ----------

    async def seller_update_once(self, *, notify: bool = True, days_back: int | None = None) -> bool:
        """One cycle: fetch orders, sales, stocks from WB Seller API. Send alerts for new events.

        days_back: how many days to look back for lastChangeDate filter.
          None → auto (90 if own_sales empty, else 3).
        """
        if not self._seller_client or not self._business_repository:
            return False
        async with self._seller_lock:
            try:
                now = datetime.now(timezone.utc)
                if days_back is None:
                    # Auto: wide on first run (empty DB), narrow on steady-state
                    existing = await self._business_repository.count_sales()
                    days_back = 90 if existing == 0 else 3
                date_from = now - timedelta(days=days_back)
                seen_at = now.isoformat()

                orders = await self._seller_client.get_orders(date_from, flag=0)
                new_order_srids = await self._business_repository.upsert_orders(orders, seen_at)

                # Sales API на statistics-api имеет частые 429. Retry если вернул 0 при больших days_back.
                sales = await self._seller_client.get_sales(date_from, flag=0)
                if not sales and days_back >= 7:
                    self._logger.warning("Sales API returned 0 at days_back=%d — retry in 10s", days_back)
                    await asyncio.sleep(10)
                    sales = await self._seller_client.get_sales(date_from, flag=0)
                new_sale_srids = await self._business_repository.upsert_sales(sales, seen_at)

                # FBO (WB-склады)
                fbo_stocks = await self._seller_client.get_stocks(date_from=date_from)
                # FBS (собственные склады селлера)
                fbs_stocks = await self._seller_client.get_all_fbs_stocks()
                all_stocks = list(fbo_stocks) + list(fbs_stocks)
                await self._business_repository.upsert_stocks(all_stocks, seen_at)
                stocks = all_stocks  # для лога ниже

                await self._meta_repository.set_value("last_seller_update_at", seen_at)
                await self._meta_repository.set_value("last_seller_orders_count", str(len(orders)))
                await self._meta_repository.set_value("last_seller_sales_count", str(len(sales)))
                await self._meta_repository.set_value("last_seller_stocks_count", str(len(stocks)))

                self._logger.info(
                    "Seller update: orders=%s (new=%s), sales=%s (new=%s), stocks=%s",
                    len(orders), len(new_order_srids),
                    len(sales), len(new_sale_srids),
                    len(stocks),
                )

                if notify:
                    await self._send_event_alerts()
                return True
            except Exception as exc:
                self._logger.exception("Seller update failed: %s", exc)
                return False

    async def _send_event_alerts(self) -> None:
        if not self._business_repository:
            return
        chat_ids = await self._subscriber_repository.list_active_chat_ids()
        if not chat_ids:
            return

        # Get calculator params for margin estimation
        sr = self._settings_repository
        cfg = self._config
        spp = await sr.get_float("spp_percent", cfg.spp_percent)
        sell_price = await sr.get_float("sell_price_rub", cfg.sell_price_rub)

        # New orders
        new_orders = await self._business_repository.get_unnotified_orders(limit=10)
        notified_order_srids: list[str] = []
        for order in new_orders:
            # Always mark as notified (even cancelled) — else they loop forever
            notified_order_srids.append(str(order["srid"]))
            if order.get("is_cancel"):
                continue  # don't alert for cancelled, but DO mark notified
            text = build_new_order_alert(order)
            for chat_id in chat_ids:
                try:
                    await self._bot.send_message(chat_id=chat_id, text=text)
                except Exception as e:
                    self._logger.warning("Failed to send order alert: %s", e)
                await asyncio.sleep(0.05)
        if notified_order_srids:
            await self._business_repository.mark_orders_notified(notified_order_srids)

        # New sales / returns
        new_sales = await self._business_repository.get_unnotified_sales(limit=10)
        notified_sale_srids: list[str] = []
        for sale in new_sales:
            is_return = bool(sale.get("is_return", 0))
            text = build_new_return_alert(sale) if is_return else build_new_sale_alert(sale)
            for chat_id in chat_ids:
                try:
                    await self._bot.send_message(chat_id=chat_id, text=text)
                except Exception as e:
                    self._logger.warning("Failed to send sale alert: %s", e)
                await asyncio.sleep(0.05)
            notified_sale_srids.append(str(sale["srid"]))
        if notified_sale_srids:
            await self._business_repository.mark_sales_notified(notified_sale_srids)

    async def _run_seller_loop(self) -> None:
        # Initial fetch — DON'T send alerts (would spam all historical events)
        await self.seller_update_once(notify=False)
        # AFTER fetch: mark everything notified so next cycle only alerts truly new events
        await self._mark_all_existing_as_notified()

        interval = getattr(self._config, "wb_seller_poll_interval_seconds", 1800)
        resync_days = getattr(self._config, "seller_full_resync_days", 7)
        cycles_per_resync = max(1, (resync_days * 86400) // interval)
        cycle_count = 0
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                cycle_count += 1
                # Every N cycles — full resync 90 days (catches missed events)
                if cycle_count % cycles_per_resync == 0:
                    self._logger.info("Running full seller resync (90 days)")
                    await self.seller_update_once(notify=False, days_back=90)
                else:
                    await self.seller_update_once(notify=True)

    async def _mark_all_existing_as_notified(self) -> None:
        """On startup, mark all existing orders/sales as notified to avoid spam on first run."""
        if not self._business_repository:
            return
        await self._business_repository.mark_all_orders_notified()
        await self._business_repository.mark_all_sales_notified()
        await self._meta_repository.set_value("seller_initial_mark", datetime.now(timezone.utc).isoformat())
        self._logger.info("Seller: marked all existing orders/sales as notified")

    # ---------- Briefing loop ----------

    async def _run_briefing_loop(self) -> None:
        """Check every 60 seconds if it's time for morning briefing."""
        briefing_hour = getattr(self._config, "briefing_hour", 9)
        briefing_minute = getattr(self._config, "briefing_minute", 0)
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=60)
            except asyncio.TimeoutError:
                now = datetime.now()  # local time
                today_str = now.strftime("%Y-%m-%d")
                if (
                    now.hour == briefing_hour
                    and now.minute >= briefing_minute
                    and self._last_briefing_date != today_str
                ):
                    await self._send_briefing()
                    self._last_briefing_date = today_str

    async def _send_briefing(self) -> None:
        if not self._insight_engine:
            return
        try:
            briefing = await self._insight_engine.build_briefing()
            text = build_briefing_message(briefing)
            chat_ids = await self._subscriber_repository.list_active_chat_ids()
            for chat_id in chat_ids:
                try:
                    await self._bot.send_message(chat_id=chat_id, text=text)
                except Exception as e:
                    self._logger.warning("Failed to send briefing: %s", e)
                await asyncio.sleep(0.05)
            self._logger.info("Morning briefing sent to %s chats", len(chat_ids))
        except Exception as exc:
            self._logger.exception("Briefing failed: %s", exc)
