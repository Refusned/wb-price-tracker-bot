from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.config import AppConfig
from app.scheduler import WbUpdateScheduler
from app.storage.repositories import (
    SettingsRepository,
    SubscriberRepository,
    TrackedArticleRepository,
)
from app.utils.formatting import format_price_rub

from .common import ensure_allowed, remember_subscriber


def get_router(
    config: AppConfig,
    settings_repository: SettingsRepository,
    subscriber_repository: SubscriberRepository,
    tracked_article_repository: TrackedArticleRepository,
    updater: WbUpdateScheduler,
) -> Router:
    router = Router(name="admin")

    @router.message(Command("setminprice"))
    async def set_min_price_handler(message: Message, command: CommandObject) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)

        args = (command.args or "").strip()
        if not args:
            await message.answer("Использование: /setminprice 9500")
            return

        try:
            new_value = int(args)
        except ValueError:
            await message.answer("Цена должна быть целым числом, пример: /setminprice 9500")
            return

        if new_value <= 0:
            await message.answer("Цена должна быть больше 0")
            return

        await settings_repository.set_value("min_price_rub", str(new_value))
        updater.trigger_background_update(reason="setminprice")

        await message.answer(
            f"Минимальная цена обновлена: {format_price_rub(new_value)}\n"
            "Изменение сохранено в SQLite."
        )

    @router.message(Command("rescan"))
    async def rescan_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)
        updater.trigger_background_update(reason="manual_rescan")
        await message.answer("Запущен принудительный скан + search новых карточек. Результаты через 15-30 сек.")

    @router.message(Command("find_deal"))
    async def find_deal_handler(message: Message) -> None:
        """Принудительный скан с поиском новых карточек + мгновенный показ топ-10."""
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)
        await message.answer("🔎 Ищу свежие карточки через search.wb.ru...")
        ok = await updater.update_once(reason="find_deal")
        if not ok:
            await message.answer("⚠️ Скан не удался, смотри /status")
            return
        from app.utils.formatting import build_top10_message
        min_price = await settings_repository.get_min_price_rub(config.min_price_rub)
        items = await updater._item_repository.get_top_items(min_price_rub=min_price, limit=10)
        last = await updater._meta_repository.get_value("last_success_update_at")
        await message.answer(build_top10_message(
            query=config.wb_query,
            min_price_rub=min_price,
            updated_at_iso=last,
            items=items,
        ))

    @router.message(Command("untrack_all"))
    async def untrack_all_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)
        count = await tracked_article_repository.deactivate_all()
        await message.answer(
            f"Деактивировано {count} артикулов. Используй /track <артикул> чтобы добавить новые."
        )

    @router.message(Command("setalertcooldown"))
    async def set_cooldown_handler(message: Message, command: CommandObject) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)
        args = (command.args or "").strip()
        if not args:
            current = int(await settings_repository.get_float("alert_cooldown_minutes", 30.0))
            await message.answer(f"Текущий cooldown: {current} мин. Использование: /setalertcooldown 30")
            return
        try:
            value = int(args)
        except ValueError:
            await message.answer("Введите число минут. Пример: /setalertcooldown 30")
            return
        if value < 0 or value > 1440:
            await message.answer("Значение от 0 до 1440 минут")
            return
        await settings_repository.set_value("alert_cooldown_minutes", str(value))
        await message.answer(f"Cooldown алертов обновлён: {value} мин")

    return router
