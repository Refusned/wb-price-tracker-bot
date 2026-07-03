"""
Фаза 2: LLM-советник по кабинету.

Тонкий слой поверх InsightEngine: берёт уже посчитанный брифинг (продажи,
возвраты, выкупаемость, остатки, скорость, сигналы) → сериализует в компактную
сводку → просит LLM дать разбор и конкретные советы по продажам.

Read-only: ничего не публикует и не мутирует. Результат уходит только владельцу
(команда /advice). Reuse: LLMClient (Фаза 1) + InsightEngine (существующий).
"""
from __future__ import annotations

import logging
import math

from app.llm.client import LLMClient
from app.services.insight_engine import BriefingData, InsightEngine


_SYSTEM_PROMPT = (
    "Ты — опытный аналитик-консультант по продажам на Wildberries. На основе "
    "данных кабинета продавца дай краткий разбор и КОНКРЕТНЫЕ советы по продажам.\n"
    "Правила:\n"
    "- На русском, по делу, без воды. Структура: 2–4 главных вывода + что сделать.\n"
    "- Опирайся ТОЛЬКО на приведённые цифры; не выдумывай данных, которых нет.\n"
    "- Приоритизируй денежно-важное: out-of-stock, возвраты, низкая выкупаемость.\n"
    "- Советы конкретные и выполнимые (цена, закупка, карточка, реклама), без "
    "банальностей.\n"
    "- Пиши обычным текстом без Markdown/HTML-разметки (никаких **, ##, `). "
    "Списки маркируй «—», акценты — эмодзи в начале строки.\n"
    "- Если данных мало — честно скажи, чего не хватает для анализа."
)


class CabinetAdvisor:
    def __init__(self, *, insight_engine: InsightEngine, llm_client: LLMClient) -> None:
        self._insights = insight_engine
        self._llm = llm_client
        self._logger = logging.getLogger(self.__class__.__name__)

    async def build_advice(self) -> str:
        """Собрать разбор кабинета с советами. Может бросить LLMError."""
        briefing = await self._insights.build_briefing()
        summary = self._serialize(briefing)
        # think=False обязателен: deepseek-v4-pro — thinking-модель, иначе на
        # длинном промпте весь бюджет уходит в «размышление» и content пуст.
        return await self._llm.generate(
            system=_SYSTEM_PROMPT, user=summary, temperature=0.4,
            num_predict=1500, think=False,
        )

    @staticmethod
    def _serialize(b: BriefingData) -> str:
        t, y, w = b.today, b.yesterday, b.week
        days_left = "∞" if math.isinf(b.days_left) else f"{b.days_left:.1f}"
        market = f"{b.market_min_price:.0f}₽" if b.market_min_price else "н/д"
        lines = [
            "Данные кабинета Wildberries:",
            f"Сегодня: заказов {t.orders_count} (отмен {t.orders_canceled}), "
            f"продаж {t.sales_count}, возвратов {t.returns_count}, "
            f"выручка {t.revenue_total:.0f}₽ (к выплате {t.revenue_net:.0f}₽).",
            f"Вчера: продаж {y.sales_count}, возвратов {y.returns_count}.",
            f"Неделя: продаж {w.sales_count}, возвратов {w.returns_count}, "
            f"выкупаемость {w.buyout_rate}%.",
            f"Скорость продаж: {b.velocity} шт/день. Остаток на складах: "
            f"{b.total_stock} шт (в пути к клиенту {b.in_way_to_client}). "
            f"Хватит на {days_left} дн.",
            f"Мин. цена конкурентов: {market}. "
            f"Рекомендованная закупка (бот): {b.recommended_buy_count} шт.",
        ]
        if b.insights:
            lines.append("Сигналы бота:")
            for ins in b.insights:
                action = f" → {ins.action}" if ins.action else ""
                lines.append(f"- [{ins.level}] {ins.title}: {ins.body}{action}")
        return "\n".join(lines)
