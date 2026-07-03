from __future__ import annotations

import asyncio
import logging
import ssl

import aiohttp
import certifi
from aiogram import Bot

from app.arbitrage.auto_observer import AutoObserver
from app.arbitrage.repository import ArbitrageRepository
from app.arbitrage.scanner import ArbitrageScanner
from app.arbitrage.spp_resolver import PersonalSppResolver
from app.arbitrage.tariffs_cache import TariffsCache
from app.arbitrage.tariffs_repository import TariffsRepository
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
from app.wb.feedbacks_client import WBFeedbacksClient
from app.llm import LLMClient
from app.services.feedback_responder import FeedbackResponder
from app.services.cabinet_advisor import CabinetAdvisor
from app.services.agent_tools import AgentToolset
from app.services.cabinet_agent import CabinetAgent
from app.storage.feedback_reply_repository import FeedbackReplyRepository
from app.storage.dialog_repository import DialogRepository


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
    arb_repo = ArbitrageRepository(database)
    arb_tariffs_repo = TariffsRepository(database)
    feedback_reply_repo = FeedbackReplyRepository(database)

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
            callback_signing_secret=config.callback_signing_secret,
        )

        personal_spp_collector = PersonalSppAutoCollector(
            personal_spp_repo=personal_spp_repo,
            business_repository=business_repository,
            meta_repository=meta_repository,
        )

        # ── Day 18+: arbitrage scanner wiring ──────────────────────
        arb_scanner: ArbitrageScanner | None = None
        arb_tariffs_cache: TariffsCache | None = None
        arb_auto_observer: AutoObserver | None = None
        if config.arbitrage_enabled and config.wb_seller_api_key:
            arb_tariffs_cache = TariffsCache(
                session=http_session,
                seller_api_key=config.wb_seller_api_key,
                tariffs_repo=arb_tariffs_repo,
            )
            arb_spp_resolver = PersonalSppResolver(arb_repo)
            arb_auto_observer = AutoObserver(
                wb_client=wb_client, arb_repo=arb_repo,
                business_repo=business_repository,
            )
            arb_scanner = ArbitrageScanner(
                config=config,
                wb_client=wb_client,
                bot=bot,
                arb_repo=arb_repo,
                tariffs_repo=arb_tariffs_repo,
                spp_resolver=arb_spp_resolver,
                business_repo=business_repository,
                subscriber_repo=subscriber_repository,
            )
            logger.info("Arbitrage scanner enabled (Day 18+)")
        elif config.arbitrage_enabled:
            logger.warning("ARBITRAGE_ENABLED=true but WB_SELLER_API_KEY is empty — scanner disabled")

        # ── Фаза 1: LLM-автоответы на отзывы/вопросы WB ───────────────
        # LLM-клиент поднимается при наличии OLLAMA_API_KEY. Автоответы
        # включаются ТОЛЬКО если FEEDBACK_AUTO_REPLY_ENABLED=true И есть ключ
        # с scope «Вопросы и отзывы». По умолчанию всё выключено (килл-свитч):
        # сам факт деплоя ничего не публикует.
        # Клиент отзывов/вопросов поднимаем при наличии ключа со scope «Вопросы и
        # отзывы» (НЕЗАВИСИМО от авто-ответов) — он нужен и автопостеру (Фаза 1),
        # и агенту для propose_feedback_reply (Фаза 3, по кнопке подтверждения).
        feedbacks_client: WBFeedbacksClient | None = None
        if config.wb_feedbacks_api_key:
            feedbacks_client = WBFeedbacksClient(
                session=http_session, api_key=config.wb_feedbacks_api_key,
            )

        feedback_responder: FeedbackResponder | None = None
        cabinet_advisor: CabinetAdvisor | None = None
        cabinet_agent: CabinetAgent | None = None
        llm_client: LLMClient | None = None
        if config.llm_api_key:
            llm_client = LLMClient(
                session=http_session,
                api_key=config.llm_api_key,
                base_url=config.llm_base_url,
                model=config.llm_model,
                timeout_seconds=config.llm_timeout_seconds,
            )
            logger.info("LLM enabled: model=%s base=%s", config.llm_model, config.llm_base_url)
            if config.feedback_auto_reply_enabled and feedbacks_client is not None:
                feedback_responder = FeedbackResponder(
                    feedbacks_client=feedbacks_client,
                    llm_client=llm_client,
                    reply_repo=feedback_reply_repo,
                    subscriber_repository=subscriber_repository,
                    bot=bot,
                    config=config,
                )
                logger.warning(
                    "Feedback AUTO-REPLY ВКЛЮЧЁН — бот будет САМ публиковать "
                    "ответы покупателям без подтверждения (модель %s)", config.llm_model,
                )
        if config.feedback_auto_reply_enabled and feedback_responder is None:
            logger.warning(
                "FEEDBACK_AUTO_REPLY_ENABLED=true, но нет OLLAMA_API_KEY или ключа "
                "с scope «Вопросы и отзывы» (WB_FEEDBACKS_API_KEY/WB_SELLER_API_KEY) "
                "— автоответы ВЫКЛЮЧЕНЫ",
            )

        # Фаза 2: советник по кабинету (/advice). Нужны LLM + Seller-данные.
        if insight_engine is not None and llm_client is not None:
            cabinet_advisor = CabinetAdvisor(
                insight_engine=insight_engine, llm_client=llm_client,
            )
            logger.info("Cabinet advisor enabled (/advice)")

        # Фаза 3: интерактивный диалог-агент (🤖 Ассистент / /chat). Нужны LLM +
        # Seller-данные. Инструменты read-only; предложенные мутации — по кнопке.
        if insight_engine is not None and llm_client is not None and config.agent_chat_enabled:
            agent_toolset = AgentToolset(
                business_repository=business_repository,
                settings_repository=settings_repository,
                seller_client=seller_client,
                feedbacks_client=feedbacks_client,
                default_tax_percent=config.profit_tax_percent,
                default_logistics_per_unit_rub=config.profit_logistics_per_unit_rub,
                default_acquiring_percent=config.profit_acquiring_percent,
            )
            cabinet_agent = CabinetAgent(
                llm_client=llm_client,
                toolset=agent_toolset,
                dialog_repo=DialogRepository(database),
                think=config.agent_think,
                max_iterations=config.agent_max_iterations,
                history_limit=config.agent_history_limit,
            )
            logger.info("Cabinet agent enabled (🤖 Ассистент / /chat)")

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
            arbitrage_scanner=arb_scanner,
            tariffs_cache=arb_tariffs_cache,
            feedback_responder=feedback_responder,
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
            arb_repo=arb_repo,
            arb_scanner=arb_scanner,
            auto_observer=arb_auto_observer,
            cabinet_advisor=cabinet_advisor,
            cabinet_agent=cabinet_agent,
            feedbacks_client=feedbacks_client,
            feedback_reply_repo=feedback_reply_repo,
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
