from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.storage.business_repository import BusinessRepository
from app.storage.repositories import SubscriberRepository
from app.storage.stock_arrival_repository import StockArrivalRepository


class StockArrivalDetector:
    """Detects new FBS/FBO stock arrivals and creates pending prompts.

    Called by the scheduler after each upsert_stocks() cycle.
    """

    def __init__(
        self,
        *,
        repository: StockArrivalRepository,
        business_repository: BusinessRepository,
        subscriber_repository: SubscriberRepository,
        bot: Bot,
        delta_threshold: int = 5,
        logger: logging.Logger | None = None,
    ) -> None:
        raw_threshold = os.getenv("STOCK_ARRIVAL_DELTA_THRESHOLD", "").strip()
        if raw_threshold and delta_threshold == 5:
            delta_threshold = int(raw_threshold)
        if delta_threshold <= 0:
            raise ValueError("delta_threshold must be > 0")

        self._repository = repository
        self._business_repository = business_repository
        self._subscriber_repository = subscriber_repository
        self._bot = bot
        self._delta_threshold = int(delta_threshold)
        self._logger = logger or logging.getLogger(self.__class__.__name__)

    async def scan(self) -> int:
        """Compares current own_stocks totals against baselines."""
        current_rows = await self._load_current_totals()
        if not current_rows:
            return 0

        baselines = await self._repository.get_baselines()
        detected_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        chat_ids = await self._subscriber_repository.list_active_chat_ids()
        chat_id = chat_ids[0] if chat_ids else None

        baseline_updates: list[dict] = []
        messages: list[tuple[dict, str | None]] = []
        created = 0

        for row in current_rows:
            nm_id = int(row["nm_id"])
            supplier_article = row.get("supplier_article")
            current_total = int(row["current_total"])
            baseline = baselines.get(nm_id)

            if baseline is not None:
                baseline_total = int(baseline["last_total_full"])
                qty_delta = current_total - baseline_total

                if qty_delta >= self._delta_threshold:
                    prompt_id = await self._repository.create_prompt(
                        nm_id=nm_id,
                        supplier_article=supplier_article,
                        qty_delta=qty_delta,
                        baseline_total=baseline_total,
                        current_total=current_total,
                        detected_at=detected_at,
                        chat_id=chat_id,
                    )
                    if prompt_id is not None:
                        prompt = {
                            "id": prompt_id,
                            "nm_id": nm_id,
                            "supplier_article": supplier_article,
                            "qty_delta": qty_delta,
                            "baseline_total": baseline_total,
                            "current_total": current_total,
                        }
                        messages.append((prompt, row.get("name")))
                        created += 1

            baseline_updates.append(
                {
                    "nm_id": nm_id,
                    "supplier_article": supplier_article,
                    "last_total_full": current_total,
                    "last_seen_at": detected_at,
                }
            )

        await self._repository.upsert_baselines(baseline_updates)

        if chat_id is not None:
            for prompt, name in messages:
                text, keyboard = self._build_message(prompt, name)
                try:
                    await self._bot.send_message(chat_id, text, reply_markup=keyboard)
                except Exception:
                    self._logger.exception(
                        "Failed to send stock arrival prompt id=%s to chat_id=%s",
                        prompt["id"],
                        chat_id,
                    )

        return created

    async def _load_current_totals(self) -> list[dict]:
        rows = await self._business_repository._db.fetchall(
            """
            SELECT
                s.nm_id AS nm_id,
                COALESCE(
                    (
                        SELECT s2.supplier_article
                        FROM own_stocks s2
                        WHERE s2.nm_id = s.nm_id
                          AND s2.supplier_article IS NOT NULL
                          AND s2.supplier_article != ''
                        ORDER BY s2.updated_at DESC, s2.warehouse_name ASC
                        LIMIT 1
                    ),
                    MAX(s.supplier_article)
                ) AS supplier_article,
                SUM(
                    COALESCE(s.quantity, 0)
                    + COALESCE(s.in_way_to_client, 0)
                    + COALESCE(s.in_way_from_client, 0)
                ) AS current_total,
                COALESCE(MAX(i.name), MAX(s.subject)) AS name
            FROM own_stocks s
            LEFT JOIN items i ON i.nm_id = CAST(s.nm_id AS TEXT)
            GROUP BY s.nm_id
            ORDER BY s.nm_id ASC
            """
        )
        return [
            {
                "nm_id": int(row["nm_id"]),
                "supplier_article": (
                    str(row["supplier_article"])
                    if row["supplier_article"] is not None
                    else None
                ),
                "current_total": int(row["current_total"] or 0),
                "name": str(row["name"]) if row["name"] is not None else None,
            }
            for row in rows
        ]

    @staticmethod
    def _build_message(prompt: dict, name: str | None) -> tuple[str, InlineKeyboardMarkup]:
        supplier_article = prompt.get("supplier_article")
        article_suffix = f" ({supplier_article})" if supplier_article else ""
        lines = [
            "🆕 Новая партия!",
            f"Артикул {prompt['nm_id']}{article_suffix}",
        ]
        if name:
            lines.append(str(name))
        lines.extend(
            [
                f"Прирост остатков: +{prompt['qty_delta']} шт",
                f"Было: {prompt['baseline_total']} → стало: {prompt['current_total']}",
                "",
                "По какой цене ты закупил эту партию (₽ за единицу)?",
            ]
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💰 Указать цену",
                        callback_data=f"purprompt:price:{prompt['id']}",
                    ),
                    InlineKeyboardButton(
                        text="⏭ Пропустить",
                        callback_data=f"purprompt:skip:{prompt['id']}",
                    ),
                ]
            ]
        )
        return "\n".join(lines), keyboard
