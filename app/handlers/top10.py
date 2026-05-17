from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.config import AppConfig
from app.scheduler import WbUpdateScheduler
from app.storage.repositories import (
    ItemRepository,
    MetaRepository,
    SettingsRepository,
    SubscriberRepository,
)
from app.utils.formatting import build_top10_message, format_price_rub

from .common import ensure_allowed, is_cache_stale, remember_subscriber


def get_router(
    config: AppConfig,
    item_repository: ItemRepository,
    meta_repository: MetaRepository,
    settings_repository: SettingsRepository,
    subscriber_repository: SubscriberRepository,
    updater: WbUpdateScheduler,
) -> Router:
    router = Router(name="top10")

    @router.message(Command("top10"))
    async def top10_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)

        min_price = await settings_repository.get_min_price_rub(config.min_price_rub)
        last_success = await meta_repository.get_value("last_success_update_at")

        stale = is_cache_stale(last_success, config.max_cache_age_seconds)
        if stale:
            updater.trigger_background_update(reason="stale_cache_on_top10")

        items = await item_repository.get_top_items(
            min_price_rub=min_price,
            limit=10,
            exclude_keywords=config.top10_exclude_keywords,
            include_keywords=config.top10_include_keywords,
        )

        if not items:
            text = f"В наличии от {format_price_rub(min_price)} не найдено"
            if stale:
                text += "\nКэш обновляется в фоне, повторите /top10 через минуту."
            await message.answer(text)
            return

        text = build_top10_message(
            query=config.wb_query,
            min_price_rub=min_price,
            updated_at_iso=last_success,
            items=items,
        )
        if stale:
            text += "\n\nКэш устарел, запущено фоновое обновление."

        await message.answer(text)

    return router
