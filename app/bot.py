from __future__ import annotations

from aiogram import Dispatcher

from app.config import AppConfig
from app.handlers import admin, business, common, decisions, margin, missed_deals, purchase_prompts, spp_log, top10
from app.scheduler import WbUpdateScheduler
from app.services.insight_engine import InsightEngine
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
    insight_engine: InsightEngine | None,
    updater: WbUpdateScheduler,
) -> Dispatcher:
    dp = Dispatcher()

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
            )
        )

    return dp
