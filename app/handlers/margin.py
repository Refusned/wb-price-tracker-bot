from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.config import AppConfig
from app.scheduler import WbUpdateScheduler
from app.services.margin_calculator import MarginCalculator
from datetime import datetime, timezone

from app.storage.repositories import (
    ItemRepository,
    MetaRepository,
    SettingsRepository,
    SubscriberRepository,
    TrackedArticleRepository,
)
from app.utils.formatting import format_margin_result, format_price_rub

from .common import ensure_allowed, is_cache_stale, remember_subscriber


async def _build_calculator(settings: SettingsRepository, config: AppConfig) -> MarginCalculator:
    return MarginCalculator(
        spp_percent=await settings.get_float("spp_percent", config.spp_percent),
        wb_commission_percent=await settings.get_float("wb_commission_percent", config.wb_commission_percent),
        logistics_cost_rub=await settings.get_float("logistics_cost_rub", config.logistics_cost_rub),
        storage_cost_per_day_rub=await settings.get_float("storage_cost_per_day_rub", config.storage_cost_per_day_rub),
        return_rate_percent=await settings.get_float("return_rate_percent", config.return_rate_percent),
        target_margin_percent=await settings.get_float("target_margin_percent", config.target_margin_percent),
    )


async def _get_sell_price(settings: SettingsRepository, config: AppConfig) -> float:
    return await settings.get_float("sell_price_rub", config.sell_price_rub)


def get_router(
    config: AppConfig,
    item_repository: ItemRepository,
    meta_repository: MetaRepository,
    settings_repository: SettingsRepository,
    subscriber_repository: SubscriberRepository,
    tracked_article_repository: TrackedArticleRepository,
    updater: WbUpdateScheduler,
) -> Router:
    router = Router(name="margin")

    @router.message(Command("deals"))
    async def deals_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)

        sell_price = await _get_sell_price(settings_repository, config)
        if sell_price <= 0:
            await message.answer(
                "Цена продажи не установлена.\n"
                "Установите: /setsellprice <цена>\n"
                "Пример: /setsellprice 12000"
            )
            return

        calculator = await _build_calculator(settings_repository, config)
        min_price = await settings_repository.get_min_price_rub(config.min_price_rub)

        last_success = await meta_repository.get_value("last_success_update_at")
        if is_cache_stale(last_success, config.max_cache_age_seconds):
            updater.trigger_background_update(reason="stale_cache_on_deals")

        items = await item_repository.get_top_items(min_price_rub=min_price, limit=30)
        if not items:
            await message.answer("Нет товаров в кэше. Подождите обновления.")
            return

        deals = []
        for item in items:
            result = calculator.calculate(item.price_rub, sell_price)
            if result.is_profitable:
                deals.append((item, result))

        if not deals:
            await message.answer(
                f"Нет выгодных сделок при цене продажи {format_price_rub(sell_price)}.\n"
                f"Текущий порог маржи: {calculator.target_margin_percent}%"
            )
            return

        batch_size = int(await settings_repository.get_float("batch_size", float(config.batch_size)))
        lines = [f"Выгодные сделки (продажа по {format_price_rub(sell_price)}):", ""]
        for idx, (item, result) in enumerate(deals[:10], start=1):
            lines.append(f"{idx}) {item.name}")
            lines.append(f"Цена: {format_price_rub(item.price_rub)}")
            lines.append(f"С СПП {calculator.spp_percent}%: {format_price_rub(result.buy_price_with_spp)}")
            lines.append(f"Прибыль: {format_price_rub(result.profit_per_unit)}/шт ({result.margin_percent}%)")
            lines.append(f"На {batch_size} шт: {format_price_rub(result.profit_per_unit * batch_size)}")
            if item.stock_qty is not None:
                lines.append(f"В наличии: {item.stock_qty} шт.")
            lines.append(f"Артикул: {item.nm_id}")
            lines.append(f"Ссылка: {item.url}")
            lines.append("")

        await message.answer("\n".join(lines).strip())

    @router.message(Command("calc"))
    async def calc_handler(message: Message, command: CommandObject) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)

        args = (command.args or "").strip()
        if not args:
            await message.answer("Использование: /calc <цена закупки>\nПример: /calc 9500")
            return

        try:
            buy_price = float(args)
        except ValueError:
            await message.answer("Введите число. Пример: /calc 9500")
            return

        if buy_price <= 0:
            await message.answer("Цена должна быть больше 0")
            return

        sell_price = await _get_sell_price(settings_repository, config)
        if sell_price <= 0:
            await message.answer(
                "Цена продажи не установлена.\n"
                "Установите: /setsellprice <цена>"
            )
            return

        calculator = await _build_calculator(settings_repository, config)
        result = calculator.calculate(buy_price, sell_price)
        batch_size = int(await settings_repository.get_float("batch_size", float(config.batch_size)))

        text = format_margin_result(result, batch_size=batch_size)
        await message.answer(text)

    @router.message(Command("spp"))
    async def spp_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)

        spp = await settings_repository.get_float("spp_percent", config.spp_percent)
        sell_price = await _get_sell_price(settings_repository, config)

        lines = [f"Текущий СПП: {spp}%"]
        if sell_price > 0:
            calculator = await _build_calculator(settings_repository, config)
            example_buy = sell_price * 0.9
            result = calculator.calculate(example_buy, sell_price)
            lines.append(f"Пример: закупка {format_price_rub(example_buy)} → прибыль {format_price_rub(result.profit_per_unit)}/шт")
        else:
            lines.append("Установите цену продажи: /setsellprice <цена>")

        lines.append(f"\nИзменить: /setspp <число>")
        await message.answer("\n".join(lines))

    @router.message(Command("setspp"))
    async def set_spp_handler(message: Message, command: CommandObject) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)

        args = (command.args or "").strip()
        if not args:
            await message.answer("Использование: /setspp 24")
            return

        try:
            new_spp = float(args)
        except ValueError:
            await message.answer("Введите число. Пример: /setspp 24")
            return

        if new_spp < 0 or new_spp > 50:
            await message.answer("СПП должен быть от 0 до 50%")
            return

        await settings_repository.set_value("spp_percent", str(new_spp))
        await message.answer(f"СПП обновлён: {new_spp}%")

    @router.message(Command("setsellprice"))
    async def set_sell_price_handler(message: Message, command: CommandObject) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)

        args = (command.args or "").strip()
        if not args:
            await message.answer("Использование: /setsellprice 12000")
            return

        try:
            new_price = float(args)
        except ValueError:
            await message.answer("Введите число. Пример: /setsellprice 12000")
            return

        if new_price <= 0:
            await message.answer("Цена должна быть больше 0")
            return

        await settings_repository.set_value("sell_price_rub", str(new_price))
        await message.answer(f"Цена продажи: {format_price_rub(new_price)}\nТеперь /deals и /calc работают.")

    @router.message(Command("costs"))
    async def costs_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)

        spp = await settings_repository.get_float("spp_percent", config.spp_percent)
        commission = await settings_repository.get_float("wb_commission_percent", config.wb_commission_percent)
        logistics = await settings_repository.get_float("logistics_cost_rub", config.logistics_cost_rub)
        storage = await settings_repository.get_float("storage_cost_per_day_rub", config.storage_cost_per_day_rub)
        return_rate = await settings_repository.get_float("return_rate_percent", config.return_rate_percent)
        sell_price = await settings_repository.get_float("sell_price_rub", config.sell_price_rub)
        target = await settings_repository.get_float("target_margin_percent", config.target_margin_percent)
        batch_size = await settings_repository.get_float("batch_size", float(config.batch_size))

        lines = [
            "Параметры расчёта маржи:",
            f"СПП: {spp}% (/setspp)",
            f"Цена продажи: {format_price_rub(sell_price) if sell_price > 0 else 'не установлена'} (/setsellprice)",
            f"Комиссия WB: {commission}%",
            f"Логистика: {format_price_rub(logistics)}",
            f"Хранение: {format_price_rub(storage)}/день (×14 дней = {format_price_rub(storage * 14)})",
            f"Возвраты: {return_rate}%",
            f"Порог маржи: {target}%",
            f"Размер партии: {int(batch_size)} шт.",
        ]
        await message.answer("\n".join(lines))

    @router.message(Command("track"))
    async def track_handler(message: Message, command: CommandObject) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)

        args = (command.args or "").strip()
        if not args or not args.isdigit():
            await message.answer("Использование: /track <артикул>\nПример: /track 12345678")
            return

        now = datetime.now(timezone.utc).isoformat()
        is_new = await tracked_article_repository.add_by_nm_id(args, f"Manual #{args}", now)
        if is_new:
            await message.answer(f"Артикул {args} добавлен в отслеживание.\nЦена появится после следующего скана.")
        else:
            await message.answer(f"Артикул {args} уже отслеживается (реактивирован).")
        updater.trigger_background_update(reason="track_added")

    @router.message(Command("untrack"))
    async def untrack_handler(message: Message, command: CommandObject) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)

        args = (command.args or "").strip()
        if not args or not args.isdigit():
            await message.answer("Использование: /untrack <артикул>")
            return

        removed = await tracked_article_repository.remove_by_nm_id(args)
        if removed:
            await message.answer(f"Артикул {args} убран из отслеживания.")
        else:
            await message.answer(f"Артикул {args} не найден в отслеживании.")

    @router.message(Command("tracked"))
    async def tracked_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)

        articles = await tracked_article_repository.get_active_list(limit=50)
        if not articles:
            await message.answer("Нет отслеживаемых артикулов. Дождитесь первого скана или добавьте: /track <артикул>")
            return

        lines = [f"Отслеживаемые артикулы ({len(articles)}):",""]
        for nm_id, name, last_seen in articles:
            lines.append(f"{nm_id} — {name[:40]}")
        lines.append(f"\nДобавить: /track <артикул>")
        lines.append(f"Убрать: /untrack <артикул>")
        await message.answer("\n".join(lines))

    return router
