"""Глобальный access-middleware: deny-by-default авторизация.

Регистрируется как outer-middleware на message и callback_query, поэтому
срабатывает ДО фильтров и хендлеров. Это defense-in-depth поверх ручных
``ensure_allowed`` в хендлерах: даже если новый хендлер забудет проверку,
неразрешённый пользователь до него не дойдёт.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from app.config import AppConfig

logger = logging.getLogger(__name__)


class AccessMiddleware(BaseMiddleware):
    def __init__(self, config: AppConfig) -> None:
        self._config = config

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        user_id = user.id if user is not None else None

        if self._config.is_user_allowed(user_id):
            return await handler(event, data)

        logger.warning("Access denied for user_id=%s", user_id)
        if isinstance(event, CallbackQuery):
            await event.answer("Доступ запрещён.", show_alert=True)
        elif isinstance(event, Message):
            await event.answer(
                "Доступ запрещён.\n"
                f"Ваш Telegram ID: {user_id}\n"
                "Владелец должен добавить его в ALLOWED_USER_IDS."
            )
        return None
