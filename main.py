from __future__ import annotations

import asyncio
import logging
import ssl

import aiohttp
import certifi
from aiogram import Bot

from app.bot import build_dispatcher
from app.config import load_config
from app.logging_setup import setup_logging
from app.scheduler import WbUpdateScheduler
from app.services.insight_engine import InsightEngine
from app.services.personal_spp_auto_collector import PersonalSppAutoCollector
from app.services.stock_arrival_detector import StockArrivalDetector
from app.storage import (
    Database,
    ItemRepository,
    MetaRepository,
    PriceHistoryRepository,
    PriceStatsRepository,
    SettingsRepository,
    SubscriberRepository,
    TrackedArticleRepository,
)
from app.storage.business_repository import BusinessRepository
from app.storage.decision_snapshot_repository import DecisionSnapshotRepository
from app.storage.missed_deal_repository import MissedDealRepository
from app.storage.personal_spp_repository import PersonalSppRepository
from app.storage.stock_arrival_repository import StockArrivalRepository
from app.wb import WildberriesClient
from app.wb.seller_client import SellerClient


async def run() -> None:
    config = load_config()
    setup_logging(config.log_level)

    logger = logging.getLogger("main")

    database = Database(config.sqlite_path)
    await database.connect()
    await database.migrate()
    applied = await database.apply_migrations()
    if applied:
        logger.info("Applied schema migrations: %s", applied)

    item_repository = ItemRepository(database)
    meta_repository = MetaRepository(database)
    settings_repository = SettingsRepository(database)
    subscriber_repository = SubscriberRepository(database)
    price_stats_repository = PriceStatsRepository(database)
    price_history_repository = PriceHistoryRepository(database)
    tracked_article_repository = TrackedArticleRepository(database)
    business_repository = BusinessRepository(database)
    personal_spp_repo = PersonalSppRepository(database)
    missed_deal_repo = MissedDealRepository(database)
    decision_snapshot_repo = DecisionSnapshotRepository(database)
    stock_arrival_repo = StockArrivalRepository(database)

    await settings_repository.ensure_defaults(config.min_price_rub)
    await settings_repository.ensure_margin_defaults(
        spp_percent=config.spp_percent,
        wb_commission_percent=config.wb_commission_percent,
        logistics_cost_rub=config.logistics_cost_rub,
        storage_cost_per_day_rub=config.storage_cost_per_day_rub,
        return_rate_percent=config.return_rate_percent,
        sell_price_rub=config.sell_price_rub,
        target_margin_percent=config.target_margin_percent,
        batch_size=config.batch_size,
    )

    ssl_context = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300, ssl=ssl_context)
    async with aiohttp.ClientSession(connector=connector) as http_session:
        wb_client = WildberriesClient(
            session=http_session,
            timeout_seconds=config.wb_request_timeout_seconds,
            retries=config.wb_http_retries,
            backoff_seconds=config.wb_http_backoff_seconds,
            rate_limit_rps=config.wb_rate_limit_rps,
            max_pages=config.wb_max_pages,
            exclude_keywords=config.top10_exclude_keywords,
            include_keywords=config.top10_include_keywords,
        )

        seller_client: SellerClient | None = None
        insight_engine: InsightEngine | None = None
        if config.wb_seller_api_key:
            seller_client = SellerClient(session=http_session, api_key=config.wb_seller_api_key)
            insight_engine = InsightEngine(
                business_repo=business_repository,
                item_repo=item_repository,
                settings_repo=settings_repository,
            )
            logger.info("Seller API enabled (FBS mode)")
        else:
            logger.info("WB_SELLER_API_KEY not set — Seller API disabled")

        bot = Bot(token=config.bot_token)

        stock_arrival_detector = StockArrivalDetector(
            repository=stock_arrival_repo,
            business_repository=business_repository,
            subscriber_repository=subscriber_repository,
            bot=bot,
            delta_threshold=config.stock_arrival_delta_threshold,
        )

        personal_spp_collector = PersonalSppAutoCollector(
            personal_spp_repo=personal_spp_repo,
            business_repository=business_repository,
            meta_repository=meta_repository,
        )

        updater = WbUpdateScheduler(
            config=config,
            wb_client=wb_client,
            bot=bot,
            item_repository=item_repository,
            meta_repository=meta_repository,
            settings_repository=settings_repository,
            subscriber_repository=subscriber_repository,
            price_stats_repository=price_stats_repository,
            price_history_repository=price_history_repository,
            tracked_article_repository=tracked_article_repository,
            decision_snapshot_repository=decision_snapshot_repo,
            stock_arrival_detector=stock_arrival_detector,
            personal_spp_auto_collector=personal_spp_collector,
            business_repository=business_repository,
            seller_client=seller_client,
            insight_engine=insight_engine,
        )

        dispatcher = build_dispatcher(
            config=config,
            item_repository=item_repository,
            meta_repository=meta_repository,
            settings_repository=settings_repository,
            subscriber_repository=subscriber_repository,
            tracked_article_repository=tracked_article_repository,
            business_repository=business_repository,
            personal_spp_repo=personal_spp_repo,
            missed_deal_repo=missed_deal_repo,
            decision_snapshot_repo=decision_snapshot_repo,
            stock_arrival_repo=stock_arrival_repo,
            personal_spp_collector=personal_spp_collector,
            insight_engine=insight_engine,
            updater=updater,
        )

        await updater.start()
        logger.info("Bot polling started")

        try:
            await dispatcher.start_polling(bot)
        finally:
            await updater.stop()
            await bot.session.close()

    await database.close()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        pass
