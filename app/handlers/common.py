from __future__ import annotations

from datetime import datetime, timezone

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from app.config import AppConfig
from app.scheduler import WbUpdateScheduler
from app.storage.repositories import (
    ItemRepository,
    MetaRepository,
    SettingsRepository,
    SubscriberRepository,
)
from app.utils.formatting import format_iso_datetime, format_price_rub


def is_cache_stale(last_success_iso: str | None, max_age_seconds: int) -> bool:
    if not last_success_iso:
        return True
    try:
        dt = datetime.fromisoformat(last_success_iso)
    except ValueError:
        return True

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    age_seconds = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()
    return age_seconds > max_age_seconds


async def ensure_allowed(message: Message, config: AppConfig) -> bool:
    user_id = message.from_user.id if message.from_user else None
    if config.is_user_allowed(user_id):
        return True

    await message.answer("Доступ запрещён.")
    return False


async def remember_subscriber(message: Message, subscriber_repository: SubscriberRepository) -> None:
    chat = message.chat
    if chat is None:
        return
    if chat.type != "private":
        return

    user_id = message.from_user.id if message.from_user else None
    await subscriber_repository.upsert_active(chat.id, user_id)


def get_router(
    config: AppConfig,
    item_repository: ItemRepository,
    meta_repository: MetaRepository,
    settings_repository: SettingsRepository,
    subscriber_repository: SubscriberRepository,
    updater: WbUpdateScheduler,
) -> Router:
    router = Router(name="common")

    @router.message(CommandStart())
    async def start_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)

        text = (
            "Привет. Я мониторю Wildberries по запросу \"{query}\".\n\n"
            "Ценовой сканер + калькулятор маржи.\n"
            "Команды: /help"
        ).format(query=config.wb_query)

        if config.owner_mode_enabled:
            text += "\nРежим доступа: только разрешённые user_id."

        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="/top10"), KeyboardButton(text="/deals")],
                [KeyboardButton(text="/spp"), KeyboardButton(text="/status")],
            ],
            resize_keyboard=True,
        )
        await message.answer(text, reply_markup=keyboard)

    @router.message(Command("help"))
    async def help_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)

        help_lines = [
            "Доступные команды:",
            "",
            "Цены:",
            "/top10 - ТОП-10 самых дешёвых в наличии",
            "/deals - выгодные сделки (с маржой выше порога)",
            "/calc <цена> - расчёт маржи при закупке по цене",
            "",
            "Настройки маржи:",
            "/spp - текущий СПП",
            "/setspp <число> - изменить СПП",
            "/setsellprice <цена> - установить цену продажи",
            "/costs - все параметры расчёта",
            "",
            "Отслеживание:",
            "/track <артикул> - добавить артикул",
            "/untrack <артикул> - убрать артикул",
            "/tracked - список отслеживаемых",
            "/untrack_all - очистить все",
            "",
            "📊 Бизнес (Seller API):",
            "/briefing - утренний брифинг вручную",
            "/today /yesterday /week /month - метрики за период",
            "/stock - остатки на складах WB (FBO + FBS)",
            "/stock_fbs - принудительный запрос FBS остатков",
            "/reorder - рекомендация по закупке",
            "/cashflow - кэшфлоу и ожидаемая выплата",
            "/abc - ABC-анализ артикулов",
            "/returns - возвраты за 30 дней",
            "/buy <кол-во> <цена> <артикул> - записать закупку",
            "/profit [today|week|month|all] - чистая прибыль",
            "/purchases - список закупок",
            "/rescan_seller - принудительно обновить Seller API",
            "/resync_history [дней] - синхронизировать историю (по умолчанию 90)",
            "/sync_finance [дней] - подтянуть финотчёт (лог-ка/возвраты/штрафы)",
            "/finance [дней] - показать удержания из финотчёта",
            "/profitcosts - налог/логистика/эквайринг расчёта прибыли",
            "/settax <%> - налог УСН (по умолч. 2%)",
            "/setlogistics <₽/шт> - логистика FBS (по умолч. 60)",
            "/setacquiring <%> - эквайринг (по умолч. 0)",
            "",
            "Общее:",
            "/status - состояние бота",
            "/rescan - принудительный скан цен + search новых карточек",
            "/find_deal - найти свежие дешёвые карточки немедленно",
            "/alerts_on - включить уведомления",
            "/alerts_off - выключить уведомления",
            "/setminprice <число> - мин. цена фильтра",
            "/setalertcooldown <мин> - cooldown между алертами",
        ]
        await message.answer("\n".join(help_lines))

    @router.message(Command("status"))
    async def status_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)

        items_count = await item_repository.count_items()
        subscribers_count = await subscriber_repository.count_active()
        min_price = await settings_repository.get_min_price_rub(config.min_price_rub)

        last_success = await meta_repository.get_value("last_success_update_at")
        last_attempt = await meta_repository.get_value("last_update_attempt_at")
        last_status = await meta_repository.get_value("last_update_status")
        last_error = await meta_repository.get_value("last_update_error")
        last_alerts_sent = await meta_repository.get_value("last_alerts_sent")
        last_tracked_count = await meta_repository.get_value("last_tracked_count")
        last_search_status = await meta_repository.get_value("last_search_status")
        alert_cooldown = int(await settings_repository.get_float("alert_cooldown_minutes", 30.0))

        stale = is_cache_stale(last_success, config.max_cache_age_seconds)

        lines = [
            f"📊 Статус бота",
            "",
            f"Запрос: {config.wb_query}",
            f"Товаров в кэше: {items_count}",
            f"Отслеживаемых артикулов: {last_tracked_count or '—'}",
            f"Фильтр мин. цены: {format_price_rub(min_price)}",
            "",
            f"Последний скан: {format_iso_datetime(last_success)}",
            f"Статус: {last_status or 'нет данных'}",
            f"Search запускался: {last_search_status or 'нет данных'}",
            f"Интервал: {config.wb_poll_interval_seconds} сек",
            f"Кэш протух: {'⚠️ да' if stale else 'нет'}",
            "",
            f"Уведомления: {'✅ вкл' if config.alerts_enabled else '❌ выкл'}",
            f"Порог падения: -{config.alert_drop_percent:.1f}%",
            f"Cooldown между алертами: {alert_cooldown} мин",
            f"Подписчиков: {subscribers_count}",
            f"Отправлено в посл. скан: {last_alerts_sent or 0}",
        ]

        if last_status == "error" and last_error:
            lines.append("")
            lines.append(f"⚠️ Ошибка: {last_error[:300]}")

        await message.answer("\n".join(lines))

    @router.message(Command("alerts_on"))
    async def alerts_on_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return

        await remember_subscriber(message, subscriber_repository)
        if message.chat:
            await subscriber_repository.set_active(message.chat.id, True)
        await message.answer("Push-уведомления о резком снижении цены включены.")

    @router.message(Command("alerts_off"))
    async def alerts_off_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return

        if message.chat:
            await subscriber_repository.set_active(message.chat.id, False)
        await message.answer("Push-уведомления выключены для этого чата.")

    return router
