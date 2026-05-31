from __future__ import annotations

from aiogram import Dispatcher

from app.arbitrage import handlers as arbitrage_handlers
from app.arbitrage.auto_observer import AutoObserver
from app.arbitrage.repository import ArbitrageRepository
from app.arbitrage.scanner import ArbitrageScanner
from app.config import AppConfig
from app.middlewares import AccessMiddleware
from app.handlers import admin, business, common, decisions, main_menu, margin, missed_deals, purchase_prompts, spp_log, top10
from app.scheduler import WbUpdateScheduler
from app.services.insight_engine import InsightEngine
from app.services.personal_spp_auto_collector import PersonalSppAutoCollector
from app.storage.business_repository import BusinessRepository
from app.storage.decision_snapshot_repository import DecisionSnapshotRepository
from app.storage.missed_deal_repository import MissedDealRepository
from app.storage.personal_spp_repository import PersonalSppRepository
from app.storage.stock_arrival_repository import StockArrivalRepository
from app.storage.repositories import (
    ItemRepository,
    MetaRepository,
    SettingsRepository,
    SubscriberRepository,
    TrackedArticleRepository,
)


def build_dispatcher(
    config: AppConfig,
    item_repository: ItemRepository,
    meta_repository: MetaRepository,
    settings_repository: SettingsRepository,
    subscriber_repository: SubscriberRepository,
    tracked_article_repository: TrackedArticleRepository,
    business_repository: BusinessRepository,
    personal_spp_repo: PersonalSppRepository,
    missed_deal_repo: MissedDealRepository,
    decision_snapshot_repo: DecisionSnapshotRepository,
    stock_arrival_repo: StockArrivalRepository,
    personal_spp_collector: PersonalSppAutoCollector | None,
    insight_engine: InsightEngine | None,
    updater: WbUpdateScheduler,
    arb_repo: ArbitrageRepository | None = None,
    arb_scanner: ArbitrageScanner | None = None,
    auto_observer: AutoObserver | None = None,
) -> Dispatcher:
    dp = Dispatcher()

    # Deny-by-default access gate. outer-middleware на update покрывает message,
    # callback_query и все прочие типы апдейтов ДО фильтров и хендлеров.
    # Callback-хендлеры (md:*/purprompt:*) полагаются на это: их собственная
    # проверка is_user_allowed снята в пользу этого middleware.
    dp.update.outer_middleware(AccessMiddleware(config))

    # main_menu MUST be registered FIRST — it handles /start and main
    # reply-keyboard buttons (🎯 Арбитраж / 💰 Финансы / etc.).
    dp.include_router(
        main_menu.get_router(
            config=config,
            subscriber_repo=subscriber_repository,
        )
    )

    dp.include_router(
        admin.get_router(
            config=config,
            settings_repository=settings_repository,
            subscriber_repository=subscriber_repository,
            tracked_article_repository=tracked_article_repository,
            updater=updater,
        )
    )
    dp.include_router(
        margin.get_router(
            config=config,
            item_repository=item_repository,
            meta_repository=meta_repository,
            settings_repository=settings_repository,
            subscriber_repository=subscriber_repository,
            tracked_article_repository=tracked_article_repository,
            updater=updater,
        )
    )
    dp.include_router(
        top10.get_router(
            config=config,
            item_repository=item_repository,
            meta_repository=meta_repository,
            settings_repository=settings_repository,
            subscriber_repository=subscriber_repository,
            updater=updater,
        )
    )
    dp.include_router(
        common.get_router(
            config=config,
            item_repository=item_repository,
            meta_repository=meta_repository,
            settings_repository=settings_repository,
            subscriber_repository=subscriber_repository,
            updater=updater,
        )
    )
    dp.include_router(
        spp_log.get_router(
            config=config,
            personal_spp_repo=personal_spp_repo,
            personal_spp_collector=personal_spp_collector,
        )
    )
    dp.include_router(
        decisions.get_router(
            config=config,
            decision_snapshot_repo=decision_snapshot_repo,
            subscriber_repository=subscriber_repository,
        )
    )
    dp.include_router(
        missed_deals.get_router(
            config=config,
            missed_deal_repo=missed_deal_repo,
            subscriber_repository=subscriber_repository,
        )
    )
    dp.include_router(
        purchase_prompts.get_router(
            config=config,
            stock_arrival_repo=stock_arrival_repo,
            business_repository=business_repository,
            subscriber_repository=subscriber_repository,
            decision_snapshot_repo=decision_snapshot_repo,
            auto_observer=auto_observer,
        )
    )

    if insight_engine is not None:
        dp.include_router(
            business.get_router(
                config=config,
                business_repository=business_repository,
                settings_repository=settings_repository,
                subscriber_repository=subscriber_repository,
                insight_engine=insight_engine,
                updater=updater,
                decision_snapshot_repo=decision_snapshot_repo,
                auto_observer=auto_observer,
            )
        )

    if arb_repo is not None and arb_scanner is not None and config.arbitrage_enabled:
        # auto_observer required for arb_quickadd / arb_bulk. Fallback empty stub
        # if for some reason it's None (shouldn't happen in main.py wiring).
        if auto_observer is None:
            # This branch shouldn't trigger; defensive
            raise RuntimeError("auto_observer must be provided when arbitrage_enabled")
        dp.include_router(
            arbitrage_handlers.get_router(
                config=config,
                arb_repo=arb_repo,
                scanner=arb_scanner,
                subscriber_repo=subscriber_repository,
                auto_observer=auto_observer,
            )
        )

    return dp
