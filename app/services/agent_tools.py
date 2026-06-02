"""
Read-only инструменты + propose-инструменты для интерактивного LLM-агента по
WB-кабинету (Фаза 3).

🔴 MONEY-SAFETY (MS-1): toolset READ-ONLY BY CONSTRUCTION. В конструкторе только
read-источники; ни один инструмент не вызывает мутирующих методов
(add_purchase/set_value/answer_*). propose_*-инструменты НИЧЕГО не пишут и не
публикуют — лишь валидируют и нормализуют предложение; реальное действие
выполняет callback подписанной кнопки (см. agent_chat.py). Реестр — whitelist;
call() никогда не бросает (ошибка → строкой в role:'tool').
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.feedback_posting import content_gate
from app.storage.business_repository import BusinessRepository
from app.storage.repositories import SettingsRepository
from app.wb.feedbacks_client import ANSWER_MIN_LEN, WBFeedbacksClient
from app.wb.seller_client import SellerClient

ToolHandler = Callable[[dict[str, Any]], Awaitable[str]]

# Настроечные ключи прибыли + допустимые диапазоны (как в /settax,/setlogistics,/setacquiring).
_PROFIT_PARAMS = {
    "tax": ("profit_tax_percent", 0.0, 50.0, "%", "Налог УСН"),
    "logistics": ("profit_logistics_per_unit_rub", 0.0, 1000.0, "₽/шт", "Логистика"),
    "acquiring": ("profit_acquiring_percent", 0.0, 10.0, "%", "Эквайринг"),
}


@dataclass(slots=True)
class Tool:
    schema: dict[str, Any]   # {"type":"function","function":{...}} для Ollama
    handler: ToolHandler     # async (args) -> str (готовый JSON/текст в role:'tool')


def _fn(name: str, description: str, properties: dict[str, Any],
        required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required or []},
    }}


class AgentToolset:
    MAX_ROWS = 20
    MAX_OUTPUT_CHARS = 4000

    def __init__(
        self,
        *,
        business_repository: BusinessRepository,
        settings_repository: SettingsRepository,
        seller_client: SellerClient | None = None,
        feedbacks_client: WBFeedbacksClient | None = None,
        default_tax_percent: float = 2.0,
        default_logistics_per_unit_rub: float = 182.0,
        default_acquiring_percent: float = 0.0,
        index_cap: int = 40,
    ) -> None:
        self._biz = business_repository
        self._settings = settings_repository
        self._seller = seller_client
        self._fb = feedbacks_client
        self._def_tax = default_tax_percent
        self._def_logi = default_logistics_per_unit_rub
        self._def_acq = default_acquiring_percent
        self._index_cap = index_cap
        self._logger = logging.getLogger(self.__class__.__name__)
        self._index_cache: list[dict[str, Any]] | None = None
        self._funnel_cache: dict[tuple, str] = {}
        self._tools: dict[str, Tool] = {}
        self._register_all()

    # ---------- публичный API для оркестратора ----------

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema for t in self._tools.values()]

    def tool_names(self) -> list[str]:
        return list(self._tools)

    def new_turn(self) -> None:
        """Сбросить покороткоживущий кэш живых WB-вызовов (один реальный вызов
        воронки на ход диалога, не на каждый повтор tool-call)."""
        self._funnel_cache.clear()

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        """Исполнить инструмент. НИКОГДА не бросает — ошибка возвращается строкой."""
        tool = self._tools.get(name)
        if tool is None:
            return self._err(f"неизвестный инструмент '{name}'")
        try:
            out = await tool.handler(arguments or {})
        except Exception as exc:  # noqa: BLE001 — изоляция тула от падения цикла
            self._logger.warning("tool %s failed: %s", name, exc, exc_info=True)
            return self._err(f"инструмент '{name}' упал: {exc}")
        return out if len(out) <= self.MAX_OUTPUT_CHARS else out[: self.MAX_OUTPUT_CHARS] + ' …"усечено"'

    async def article_index(self) -> list[dict[str, Any]]:
        """Компактный индекс артикулов кабинета (nm_id/supplier_article/subject)
        для системного промпта и валидации propose_purchase. Лениво, кэш на
        процесс (новые артикулы появятся после рестарта — ок для персонального бота)."""
        if self._index_cache is not None:
            return self._index_cache
        seen: dict[str, dict[str, Any]] = {}
        try:
            for r in await self._biz.get_stock_summary():
                nm = r.get("nm_id")
                if nm is None:
                    continue
                seen[str(nm)] = {"nm_id": nm, "supplier_article": r.get("supplier_article"),
                                 "subject": r.get("subject")}
            for r in await self._biz.get_abc_analysis(30):
                nm = r.get("nm_id")
                if nm is None or str(nm) in seen:
                    continue
                seen[str(nm)] = {"nm_id": nm, "supplier_article": r.get("supplier_article"),
                                 "subject": r.get("subject")}
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("article_index build failed: %s", exc)
        self._index_cache = list(seen.values())[: self._index_cap]
        return self._index_cache

    # ---------- helpers ----------

    @staticmethod
    def _err(msg: str) -> str:
        return json.dumps({"error": msg}, ensure_ascii=False)

    @staticmethod
    def _dump(payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, default=str)

    @staticmethod
    def _clamp_int(args: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
        try:
            v = int(args.get(key, default))
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))

    async def _profit_costs(self) -> tuple[float, float, float]:
        """Текущие параметры прибыли (из настроек, как у /profit)."""
        tax = await self._settings.get_float("profit_tax_percent", self._def_tax)
        logi = await self._settings.get_float("profit_logistics_per_unit_rub", self._def_logi)
        acq = await self._settings.get_float("profit_acquiring_percent", self._def_acq)
        return tax, logi, acq

    # ---------- регистрация ----------

    def _register_all(self) -> None:
        r = self._register
        r(_fn("get_period_summary",
              "Сводка продаж за последние N дней: заказы, продажи, возвраты, выручка, "
              "выручка к выплате, выкупаемость.",
              {"days": {"type": "integer", "minimum": 1, "maximum": 90,
                        "description": "Период в днях, по умолчанию 7"}}),
          self._t_period_summary)
        r(_fn("get_daily_metrics",
              "Метрики за один день (для сравнения 'сегодня vs вчера').",
              {"date": {"type": "string",
                        "description": "Дата YYYY-MM-DD, либо 'today'/'yesterday' (по умолчанию today)"}}),
          self._t_daily_metrics)
        r(_fn("get_top_articles",
              "Рейтинг артикулов по чистой выручке за N дней (ABC): продажи, выручка, возвраты.",
              {"days": {"type": "integer", "minimum": 1, "maximum": 90, "description": "По умолчанию 30"},
               "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "По умолчанию 10"}}),
          self._t_top_articles)
        r(_fn("get_profit_breakdown",
              "Прибыль по артикулам за N дней: revenue, profit, маржа %, ROI %, средняя закупка, "
              "есть ли данные о закупке. ВНИМАНИЕ: при has_purchase_data=false прибыль ЗАНИЖЕНА.",
              {"days": {"type": "integer", "minimum": 1, "maximum": 90, "description": "По умолчанию 30"},
               "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "По умолчанию 15"}}),
          self._t_profit_breakdown)
        r(_fn("get_total_profit",
              "Итоговый P&L за N дней: выручка, налог, логистика, себестоимость, прибыль, маржа %, "
              "ROI %, список артикулов без данных о закупке.",
              {"days": {"type": "integer", "minimum": 1, "maximum": 90, "description": "По умолчанию 30"}}),
          self._t_total_profit)
        r(_fn("get_stock_summary",
              "Остатки на складах WB по артикулам: всего шт, в пути к клиенту, число складов.", {}),
          self._t_stock_summary)
        r(_fn("get_returns",
              "Последние возвраты за N дней: дата, артикул, сумма.",
              {"days": {"type": "integer", "minimum": 1, "maximum": 90, "description": "По умолчанию 30"},
               "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "По умолчанию 15"}}),
          self._t_returns)

        if self._seller is not None:
            r(_fn("get_funnel",
                  "Свежая воронка WB по артикулам: показы, добавления в корзину, заказы, % выкупа. "
                  "Помогает понять: мало показов (SEO/реклама), низкая корзина (карточка/цена) или "
                  "низкий выкуп. Если nm_ids не заданы — берёт артикулы кабинета.",
                  {"nm_ids": {"type": "array", "items": {"type": "integer"},
                              "description": "Список nm_id; пусто = все артикулы кабинета"},
                   "days": {"type": "integer", "minimum": 1, "maximum": 30, "description": "По умолчанию 7"}}),
              self._t_funnel)

        if self._fb is not None:
            r(_fn("get_unanswered_feedbacks",
                  "Неотвеченные отзывы покупателей: id, оценка, товар, текст. id нужен для "
                  "propose_feedback_reply.", {}),
              self._t_unanswered_feedbacks)
            r(_fn("get_unanswered_questions",
                  "Неотвеченные вопросы покупателей: id, товар, текст.", {}),
              self._t_unanswered_questions)

        # --- propose-инструменты (НЕ мутируют; только валидируют+нормализуют) ---
        r(_fn("propose_purchase",
              "ПРЕДЛОЖИТЬ владельцу записать закупку (он подтвердит кнопкой). Не выполняет запись. "
              "Артикул должен существовать в кабинете.",
              {"nm_id": {"type": "integer", "description": "nm_id артикула"},
               "supplier_article": {"type": "string", "description": "Артикул продавца (если нет nm_id)"},
               "quantity": {"type": "integer", "minimum": 1, "description": "Количество, шт"},
               "buy_price_per_unit": {"type": "number", "minimum": 0, "description": "Цена закупки за шт, ₽"},
               "notes": {"type": "string", "description": "Комментарий (опц.)"}},
              required=["quantity", "buy_price_per_unit"]),
          self._t_propose_purchase)
        r(_fn("propose_profit_setting",
              "ПРЕДЛОЖИТЬ изменить параметр расчёта прибыли (владелец подтвердит кнопкой). Не применяет.",
              {"param": {"type": "string", "enum": ["tax", "logistics", "acquiring"],
                         "description": "tax=налог %, logistics=₽/шт, acquiring=%"},
               "value": {"type": "number", "minimum": 0, "description": "Новое значение"}},
              required=["param", "value"]),
          self._t_propose_profit_setting)
        if self._fb is not None:
            r(_fn("propose_feedback_reply",
                  "ПРЕДЛОЖИТЬ ответ покупателю на отзыв/вопрос (владелец подтвердит кнопкой; публикация "
                  "необратима). Не публикует. Сначала получи id через get_unanswered_*. Без ссылок/"
                  "телефонов/почты.",
                  {"target_id": {"type": "string", "description": "id отзыва/вопроса WB"},
                   "kind": {"type": "string", "enum": ["feedback", "question"],
                            "description": "feedback=отзыв, question=вопрос"},
                   "text": {"type": "string", "description": "Текст ответа покупателю"}},
                  required=["target_id", "kind", "text"]),
              self._t_propose_feedback_reply)

    def _register(self, schema: dict[str, Any], handler: ToolHandler) -> None:
        self._tools[schema["function"]["name"]] = Tool(schema=schema, handler=handler)

    # ---------- READ-инструменты ----------

    async def _t_period_summary(self, args: dict[str, Any]) -> str:
        days = self._clamp_int(args, "days", 7, 1, 90)
        m = await self._biz.get_period_metrics(days)
        return self._dump(asdict(m))

    async def _t_daily_metrics(self, args: dict[str, Any]) -> str:
        raw = str(args.get("date", "today")).strip().lower()
        today = datetime.now(timezone.utc).date()
        if raw in ("", "today", "сегодня"):
            date = today.isoformat()
        elif raw in ("yesterday", "вчера"):
            date = (today - timedelta(days=1)).isoformat()
        else:
            try:
                date = datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
            except ValueError:
                return self._err("date должна быть YYYY-MM-DD, 'today' или 'yesterday'")
        m = await self._biz.get_daily_metrics(date)
        return self._dump(asdict(m))

    async def _t_top_articles(self, args: dict[str, Any]) -> str:
        days = self._clamp_int(args, "days", 30, 1, 90)
        limit = self._clamp_int(args, "limit", 10, 1, self.MAX_ROWS)
        rows = await self._biz.get_abc_analysis(days)
        out = [{
            "nm_id": r.get("nm_id"), "supplier_article": r.get("supplier_article"),
            "subject": r.get("subject"), "sale_count": r.get("sale_count"),
            "net_revenue": round(float(r.get("net_revenue") or 0), 2),
            "returns": r.get("returns"),
        } for r in rows[:limit]]
        return self._dump({"days": days, "articles": out})

    async def _t_profit_breakdown(self, args: dict[str, Any]) -> str:
        days = self._clamp_int(args, "days", 30, 1, 90)
        limit = self._clamp_int(args, "limit", 15, 1, self.MAX_ROWS)
        tax, logi, acq = await self._profit_costs()
        rows = await self._biz.get_profit_breakdown(
            days=days, tax_percent=tax, logistics_per_unit_rub=logi, acquiring_percent=acq)
        keep = ("supplier_article", "nm_id", "subject", "sold_qty", "returns_qty",
                "revenue", "profit", "margin_pct", "roi_pct", "avg_buy_price", "has_purchase_data")
        out = [{k: r.get(k) for k in keep} for r in rows[:limit]]
        return self._dump({"days": days, "articles": out})

    async def _t_total_profit(self, args: dict[str, Any]) -> str:
        days = self._clamp_int(args, "days", 30, 1, 90)
        tax, logi, acq = await self._profit_costs()
        data = await self._biz.get_total_profit(
            days=days, tax_percent=tax, logistics_per_unit_rub=logi, acquiring_percent=acq)
        data.pop("breakdown", None)  # тяжёлый дубль get_profit_breakdown
        return self._dump(data)

    async def _t_stock_summary(self, args: dict[str, Any]) -> str:
        rows = await self._biz.get_stock_summary()
        out = [{
            "nm_id": r.get("nm_id"), "supplier_article": r.get("supplier_article"),
            "subject": r.get("subject"), "total_qty": r.get("total_qty"),
            "total_in_way_to": r.get("total_in_way_to"), "warehouse_count": r.get("warehouse_count"),
        } for r in rows[: self.MAX_ROWS]]
        return self._dump({"articles": out})

    async def _t_returns(self, args: dict[str, Any]) -> str:
        days = self._clamp_int(args, "days", 30, 1, 90)
        limit = self._clamp_int(args, "limit", 15, 1, self.MAX_ROWS)
        rows = await self._biz.get_returns(days=days, limit=limit)
        return self._dump({"days": days, "returns": [dict(r) for r in rows]})

    async def _t_funnel(self, args: dict[str, Any]) -> str:
        if self._seller is None:
            return self._err("воронка недоступна: нет seller-клиента")
        days = self._clamp_int(args, "days", 7, 1, 30)
        nm_ids = args.get("nm_ids") or []
        if not isinstance(nm_ids, list):
            nm_ids = []
        clean_ids: list[int] = []
        for x in nm_ids[: self.MAX_ROWS]:
            try:
                clean_ids.append(int(x))
            except (TypeError, ValueError):
                continue
        if not clean_ids:
            clean_ids = [int(a["nm_id"]) for a in await self.article_index()
                         if a.get("nm_id") is not None][: self.MAX_ROWS]
        if not clean_ids:
            return self._err("нет артикулов для воронки")

        cache_key = (tuple(sorted(clean_ids)), days)
        if cache_key in self._funnel_cache:
            return self._funnel_cache[cache_key]

        date_to = datetime.now(timezone.utc)
        date_from = date_to - timedelta(days=days)
        cards = await self._seller.get_nm_report_detail(clean_ids, date_from, date_to)
        funnel: list[dict[str, Any]] = []
        for c in cards or []:
            if not isinstance(c, dict):
                continue
            sp = ((c.get("statistics") or {}).get("selectedPeriod")) or {}
            funnel.append({
                "nm_id": c.get("nmID"), "vendor_code": c.get("vendorCode"),
                "views": sp.get("openCardCount"), "cart_adds": sp.get("addToCartCount"),
                "orders": sp.get("ordersCount"), "buyouts": sp.get("buyoutsCount"),
                "buyout_percent": sp.get("buyoutPercent"),
                "add_to_cart_percent": sp.get("addToCartPercent"),
                "cart_to_order_percent": sp.get("cartToOrderPercent"),
            })
        result = self._dump({"days": days, "funnel": funnel}) if funnel else self._dump(
            {"days": days, "funnel": [], "note": "WB не отдал данные (429 или пусто)"})
        self._funnel_cache[cache_key] = result
        return result

    async def _t_unanswered_feedbacks(self, args: dict[str, Any]) -> str:
        if self._fb is None:
            return self._err("отзывы недоступны: нет ключа со scope «Вопросы и отзывы»")
        fbs = await self._fb.get_unanswered_feedbacks()
        out = [{"id": f.id, "rating": f.rating, "product_name": f.product_name,
                "text": (f.text or "")[:200]} for f in fbs[: self.MAX_ROWS]]
        return self._dump({"feedbacks": out})

    async def _t_unanswered_questions(self, args: dict[str, Any]) -> str:
        if self._fb is None:
            return self._err("вопросы недоступны: нет ключа со scope «Вопросы и отзывы»")
        qs = await self._fb.get_unanswered_questions()
        out = [{"id": q.id, "product_name": q.product_name, "text": (q.text or "")[:200]}
               for q in qs[: self.MAX_ROWS]]
        return self._dump({"questions": out})

    # ---------- PROPOSE-инструменты (валидация+нормализация, НЕ мутируют) ----------

    async def _t_propose_purchase(self, args: dict[str, Any]) -> str:
        try:
            quantity = int(args.get("quantity"))
            price = float(args.get("buy_price_per_unit"))
        except (TypeError, ValueError):
            return self._dump({"ok": False, "error": "quantity (int) и buy_price_per_unit (number) обязательны"})
        if quantity <= 0:
            return self._dump({"ok": False, "error": "quantity должно быть > 0"})
        if price <= 0:
            return self._dump({"ok": False, "error": "buy_price_per_unit должно быть > 0"})

        nm_id = args.get("nm_id")
        supplier_article = args.get("supplier_article")
        if nm_id is None and not supplier_article:
            return self._dump({"ok": False, "error": "нужен nm_id или supplier_article"})

        index = await self.article_index()
        nm_set = {str(a["nm_id"]) for a in index if a.get("nm_id") is not None}
        art_set = {str(a["supplier_article"]) for a in index if a.get("supplier_article")}
        resolved = None
        if nm_id is not None and str(nm_id) in nm_set:
            resolved = next(a for a in index if str(a["nm_id"]) == str(nm_id))
        elif supplier_article and str(supplier_article) in art_set:
            resolved = next(a for a in index if str(a.get("supplier_article")) == str(supplier_article))
        if resolved is None:
            ident = nm_id if nm_id is not None else supplier_article
            return self._dump({"ok": False, "error": f"артикул '{ident}' не найден в кабинете"})

        label = resolved.get("supplier_article") or resolved.get("nm_id")
        total = round(quantity * price, 2)
        return self._dump({
            "ok": True, "kind": "purchase",
            "params": {"nm_id": resolved.get("nm_id"),
                       "supplier_article": resolved.get("supplier_article"),
                       "quantity": quantity, "buy_price_per_unit": price,
                       "notes": (str(args["notes"])[:200] if args.get("notes") else None)},
            "summary": f"Записать закупку: {quantity} шт × {price:g} ₽ — {label} (итого {total:g} ₽)",
        })

    async def _t_propose_profit_setting(self, args: dict[str, Any]) -> str:
        param = str(args.get("param", "")).strip().lower()
        spec = _PROFIT_PARAMS.get(param)
        if spec is None:
            return self._dump({"ok": False, "error": "param ∈ {tax, logistics, acquiring}"})
        key, lo, hi, unit, label = spec
        try:
            value = float(args.get("value"))
        except (TypeError, ValueError):
            return self._dump({"ok": False, "error": "value должно быть числом"})
        if not (lo <= value <= hi):
            return self._dump({"ok": False, "error": f"{label}: значение вне диапазона [{lo:g};{hi:g}]"})
        return self._dump({
            "ok": True, "kind": "profit_setting",
            "params": {"param": param, "settings_key": key, "value": value},
            "summary": f"Установить {label} = {value:g}{unit}",
        })

    async def _t_propose_feedback_reply(self, args: dict[str, Any]) -> str:
        if self._fb is None:
            return self._dump({"ok": False, "error": "ответы недоступны: нет ключа «Вопросы и отзывы»"})
        target_id = str(args.get("target_id", "")).strip()
        kind = str(args.get("kind", "")).strip().lower()
        text = str(args.get("text", "")).strip()
        if not target_id:
            return self._dump({"ok": False, "error": "нужен target_id (из get_unanswered_*)"})
        if kind not in ("feedback", "question"):
            return self._dump({"ok": False, "error": "kind ∈ {feedback, question}"})
        if len(text) < ANSWER_MIN_LEN:
            return self._dump({"ok": False, "error": f"текст слишком короткий (мин. {ANSWER_MIN_LEN})"})
        if not content_gate(text):
            return self._dump({"ok": False, "error": "в ответе нельзя ссылки/телефоны/почту"})
        return self._dump({
            "ok": True, "kind": "feedback_reply",
            "params": {"target_id": target_id, "target_kind": kind, "text": text},
            # Превью почти полного текста (а не 80 симв): владелец подтверждает
            # необратимый публичный пост — он должен видеть, что реально уйдёт.
            "summary": f"Ответить на {'отзыв' if kind == 'feedback' else 'вопрос'} {target_id}:\n"
                       f"«{text[:600]}{'…' if len(text) > 600 else ''}»",
        })
