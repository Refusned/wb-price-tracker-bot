from __future__ import annotations

from aiogram import Dispatcher

from app.config import AppConfig
from app.handlers import admin, business, common, margin, top10
from app.scheduler import WbUpdateScheduler
from app.services.insight_engine import InsightEngine
from app.storage.business_repository import BusinessRepository
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
