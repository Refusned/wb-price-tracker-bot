from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _parse_allowed_ids(raw_value: str | None) -> set[int]:
    if not raw_value:
        return set()
    parsed: set[int] = set()
    for chunk in raw_value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if chunk.isdigit():
            parsed.add(int(chunk))
    return parsed


def _to_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _to_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _to_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _parse_query_variants(raw_value: str | None) -> list[str]:
    if not raw_value:
        return []

    variants: list[str] = []
    seen: set[str] = set()
    for chunk in raw_value.split(","):
        value = chunk.strip()
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        variants.append(value)
    return variants


def _parse_exclude_keywords(raw: str | None) -> list[str]:
    if not raw:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for chunk in raw.split(","):
        kw = chunk.strip().lower()
        if not kw or kw in seen:
            continue
        seen.add(kw)
        result.append(kw)
    return result


def _build_default_query_variants(base_query: str) -> list[str]:
    query = base_query.strip()
    if not query:
        return []
    return [query]


@dataclass(slots=True)
class AppConfig:
    bot_token: str
    allowed_user_ids: set[int]
    wb_query: str
    wb_query_variants: list[str]
    min_price_rub: int
    wb_poll_interval_seconds: int
    max_cache_age_seconds: int
    sqlite_path: str
    wb_max_pages: int
    wb_request_timeout_seconds: float
    wb_http_retries: int
    wb_http_backoff_seconds: float
    wb_rate_limit_rps: float
    alerts_enabled: bool
    alert_drop_percent: float
    alert_max_items_per_cycle: int
    log_level: str
    # Margin calculator
    spp_percent: float
    wb_commission_percent: float
    logistics_cost_rub: float
    storage_cost_per_day_rub: float
    return_rate_percent: float
    sell_price_rub: float
    target_margin_percent: float
    batch_size: int
    # Seller API (FBS)
    wb_seller_api_key: str
    wb_seller_poll_interval_seconds: int
    wb_trade_mode: str
    # Briefing cron
    briefing_hour: int
    briefing_minute: int
    # Profit calculation defaults (overridable via /settax, /setlogistics, /setacquiring)
    profit_tax_percent: float
    profit_logistics_per_unit_rub: float
    profit_acquiring_percent: float
    # Auto-sync history (full resync every N days)
    seller_full_resync_days: int
    # Price history retention (days). Critical for Day 22 counterfactual measurement.
    # Default 120 (was hardcoded 14 in scheduler.py:198 before Day 0 hotfix).
    price_history_retention_days: int
    # HMAC secret for signing Telegram callback payloads on mutating inline buttons.
    # Bot MUST refuse to start without this set if inline-button handlers are enabled.
    callback_signing_secret: str
    # Stock arrival detector: minimum positive delta in SUM(quantity+in_way_to+in_way_from)
    # per nm_id to trigger a "what did you pay?" prompt. Default 5 units.
    stock_arrival_delta_threshold: int
    # Keywords (lowercase, comma-separated in env) to EXCLUDE from /top10 and
    # all price scanning. Match by substring on item.name.lower(). Use for
    # filtering out specific colors/variants you don't trade.
    # Example: TOP10_EXCLUDE_KEYWORDS=жёлт,желт,yellow
    top10_exclude_keywords: list[str]
    # Keywords (lowercase, comma-separated in env) — if non-empty, item.name
    # MUST contain at least one to pass /top10 and price scanning. Strict
    # whitelist mode. Use when you only want specific colors/variants.
    # Example: TOP10_INCLUDE_KEYWORDS=чёрн,черн,серая,серый,серое,black,grey
    # When BOTH include and exclude are set, item must match ≥1 include AND
    # 0 excludes (typical example: include=чёрн,сер + exclude=серебр).
    top10_include_keywords: list[str]
    # If False, suppress per-order and per-sale Telegram notifications
    # (the "you got a new order!" spam). Price-drop alerts are NOT affected.
    event_alerts_enabled: bool
    # ── Day 18+: Arbitrage scanner ─────────────────────────────────
    arbitrage_enabled: bool
    arbitrage_scan_interval_seconds: int
    arbitrage_min_pprd_percent: float       # profit per ruble per day, %
    arbitrage_min_profit_rub: int
    arbitrage_min_margin_percent: float
    arbitrage_alert_cooldown_hours: int
    arbitrage_daily_alert_cap: int
    arbitrage_cohort_min_size: int
    arbitrage_default_spp_percent: float
    # WB-Кошелёк bonus (real discount applied at checkout when paying with
    # WB Wallet balance). Constant across categories. 6% for active buyers.
    arbitrage_wallet_bonus_pct: float
    # Path to file with buyer cookie (для будущего авто-fetch личной цены).
    # Пока не используется: команды /arb_set_cookie нет, PoW-solver отложен.
    # Наблюдения СПП собираются вручную (/arb_observe, /buy hook).
    wb_buyer_cookie_path: str
    # WB destination ID for card.wb.ru (Moscow default).
    wb_buyer_dest_param: int
    # Shadow mode: бот считает и показывает рекомендации, но НЕ выполняет
    # автономных мутаций (auto-observer/мутирующие inline-кнопки отключены).
    shadow_mode: bool
    # Ретеншен арбитражных таблиц (дни). Без него БД растёт бесконечно.
    arb_candidate_retention_days: int
    arb_observation_retention_days: int
    # ── LLM (Ollama Cloud) — общая основа для Фазы 1 (автоответы) и
    #    Фазы 2 (советник по кабинету) ──────────────────────────────
    # LLM-клиент создаётся, только если задан llm_api_key (как seller_client).
    # base_url по умолчанию — Ollama Cloud; для локального Ollama поставь
    # LLM_BASE_URL=http://localhost:11434.
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_timeout_seconds: float
    # ── Автоответы на отзывы/вопросы WB (Фаза 1) ───────────────────
    # ВЫКЛ по умолчанию: даже при заданном ключе ничего не постится,
    # пока флаг не включён явно. Это килл-свитч.
    feedback_auto_reply_enabled: bool
    feedback_poll_interval_seconds: int
    # Токен с scope «Вопросы и отзывы». Если пуст — берём wb_seller_api_key.
    wb_feedbacks_api_key: str
    # Необязательная подпись в конце ответа (напр. «С уважением, магазин N»).
    feedback_signature: str
    # Сколько отзывов+вопросов обрабатывать за один цикл. Троттлит «потоп»
    # на первом запуске (бэклог исторических неотвеченных отвечается порциями).
    feedback_max_per_cycle: int
    # ── Интерактивный LLM-агент по кабинету (Фаза 3) ───────────────
    # Включается при наличии LLM + Seller-данных; флаг — доп. килл-свитч.
    agent_chat_enabled: bool
    agent_think: bool          # think в tool-loop (probe подтвердил: False работает)
    agent_max_iterations: int  # лимит обращений к инструментам за один ход
    agent_history_limit: int   # сколько последних ходов держать в контексте

    def is_user_allowed(self, user_id: int | None) -> bool:
        # Deny-by-default: пустой ALLOWED_USER_IDS = доступ закрыт ВСЕМ.
        # Владелец видит свой ID в ответе бота и добавляет его в whitelist.
        # (Бывшее свойство owner_mode_enabled удалено: старая семантика
        # "owner_mode off ⇒ открыто всем" была footgun-ом.)
        if not self.allowed_user_ids:
            return False
        if user_id is None:
            return False
        return user_id in self.allowed_user_ids

    @property
    def sqlite_path_obj(self) -> Path:
        return Path(self.sqlite_path)

    @property
    def wb_queries(self) -> list[str]:
        if self.wb_query_variants:
            return self.wb_query_variants
        return _build_default_query_variants(self.wb_query)



def load_config() -> AppConfig:
    load_dotenv()

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise ValueError("BOT_TOKEN is required")

    poll_interval_seconds = _to_int("WB_POLL_INTERVAL_SECONDS", 600)

    config = AppConfig(
        bot_token=bot_token,
        allowed_user_ids=_parse_allowed_ids(os.getenv("ALLOWED_USER_IDS", "")),
        wb_query=os.getenv("WB_QUERY", "товар"),
        wb_query_variants=_parse_query_variants(os.getenv("WB_QUERY_VARIANTS", "")),
        min_price_rub=_to_int("MIN_PRICE_RUB", 9000),
        wb_poll_interval_seconds=poll_interval_seconds,
        max_cache_age_seconds=_to_int("MAX_CACHE_AGE_SECONDS", 300),
        sqlite_path=os.getenv("SQLITE_PATH", "data/app.db"),
        wb_max_pages=_to_int("WB_MAX_PAGES", 1),
        wb_request_timeout_seconds=_to_float("WB_REQUEST_TIMEOUT_SECONDS", 20.0),
        wb_http_retries=_to_int("WB_HTTP_RETRIES", 6),
        wb_http_backoff_seconds=_to_float("WB_HTTP_BACKOFF_SECONDS", 1.0),
        wb_rate_limit_rps=_to_float("WB_RATE_LIMIT_RPS", 1.0),
        alerts_enabled=_to_bool("ALERTS_ENABLED", True),
        alert_drop_percent=_to_float("ALERT_DROP_PERCENT", 5.0),
        alert_max_items_per_cycle=_to_int("ALERT_MAX_ITEMS_PER_CYCLE", 3),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        spp_percent=_to_float("SPP_PERCENT", 24.0),
        wb_commission_percent=_to_float("WB_COMMISSION_PERCENT", 15.0),
        logistics_cost_rub=_to_float("LOGISTICS_COST_RUB", 400.0),
        storage_cost_per_day_rub=_to_float("STORAGE_COST_PER_DAY_RUB", 5.0),
        return_rate_percent=_to_float("RETURN_RATE_PERCENT", 3.0),
        sell_price_rub=_to_float("SELL_PRICE_RUB", 0.0),
        target_margin_percent=_to_float("TARGET_MARGIN_PERCENT", 10.0),
        batch_size=_to_int("BATCH_SIZE", 25),
        wb_seller_api_key=os.getenv("WB_SELLER_API_KEY", "").strip(),
        wb_seller_poll_interval_seconds=_to_int("WB_SELLER_POLL_INTERVAL_SECONDS", 1800),
        wb_trade_mode=os.getenv("WB_TRADE_MODE", "FBS"),
        briefing_hour=_to_int("BRIEFING_HOUR", 9),
        briefing_minute=_to_int("BRIEFING_MINUTE", 0),
        profit_tax_percent=_to_float("PROFIT_TAX_PERCENT", 2.0),
        profit_logistics_per_unit_rub=_to_float("PROFIT_LOGISTICS_PER_UNIT_RUB", 182.0),
        profit_acquiring_percent=_to_float("PROFIT_ACQUIRING_PERCENT", 0.0),
        seller_full_resync_days=_to_int("SELLER_FULL_RESYNC_DAYS", 7),
        price_history_retention_days=_to_int("PRICE_HISTORY_RETENTION_DAYS", 120),
        callback_signing_secret=os.getenv("CALLBACK_SIGNING_SECRET", "").strip(),
        stock_arrival_delta_threshold=_to_int("STOCK_ARRIVAL_DELTA_THRESHOLD", 5),
        top10_exclude_keywords=_parse_exclude_keywords(
            # By default no exclude — strict include below covers it.
            os.getenv("TOP10_EXCLUDE_KEYWORDS", "")
        ),
        top10_include_keywords=_parse_exclude_keywords(
            # Owner trades only black/grey Yandex Station Midi. Strict mode:
            # item.name.lower() must contain at least one keyword. Items with
            # no color in name (e.g., "Умная колонка Станция Миди") are
            # EXCLUDED — owner explicitly said only black/grey.
            # Russian forms: чёрн/черн cover all declensions of "чёрный".
            # For grey we list specific forms ("серая","серый","серое") to
            # avoid matching "серебр-" (silver) which contains "сер".
            os.getenv(
                "TOP10_INCLUDE_KEYWORDS",
                "чёрн,черн,серая,серый,серое,серого,серому,серым,black,grey,gray"
            )
        ),
        event_alerts_enabled=_to_bool("EVENT_ALERTS_ENABLED", True),
        arbitrage_enabled=_to_bool("ARBITRAGE_ENABLED", False),
        arbitrage_scan_interval_seconds=_to_int("ARBITRAGE_SCAN_INTERVAL_SECONDS", 600),
        arbitrage_min_pprd_percent=_to_float("ARBITRAGE_MIN_PPRD_PERCENT", 0.25),
        arbitrage_min_profit_rub=_to_int("ARBITRAGE_MIN_PROFIT_RUB", 500),
        arbitrage_min_margin_percent=_to_float("ARBITRAGE_MIN_MARGIN_PERCENT", 8.0),
        arbitrage_alert_cooldown_hours=_to_int("ARBITRAGE_ALERT_COOLDOWN_HOURS", 6),
        arbitrage_daily_alert_cap=_to_int("ARBITRAGE_DAILY_ALERT_CAP", 30),
        arbitrage_cohort_min_size=_to_int("ARBITRAGE_COHORT_MIN_SIZE", 5),
        arbitrage_default_spp_percent=_to_float("ARBITRAGE_DEFAULT_SPP_PERCENT", 20.0),
        arbitrage_wallet_bonus_pct=_to_float("ARBITRAGE_WALLET_BONUS_PCT", 6.0),
        wb_buyer_cookie_path=os.getenv("WB_BUYER_COOKIE_PATH", "data/wb_buyer_cookie.txt"),
        wb_buyer_dest_param=_to_int("WB_BUYER_DEST_PARAM", -1257786),
        shadow_mode=_to_bool("SHADOW_MODE", True),
        arb_candidate_retention_days=_to_int("ARB_CANDIDATE_RETENTION_DAYS", 90),
        arb_observation_retention_days=_to_int("ARB_OBSERVATION_RETENTION_DAYS", 180),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://ollama.com").strip(),
        llm_api_key=os.getenv("OLLAMA_API_KEY", "").strip(),
        llm_model=os.getenv("LLM_MODEL", "deepseek-v4-pro").strip(),
        llm_timeout_seconds=_to_float("LLM_TIMEOUT_SECONDS", 60.0),
        feedback_auto_reply_enabled=_to_bool("FEEDBACK_AUTO_REPLY_ENABLED", False),
        feedback_poll_interval_seconds=_to_int("FEEDBACK_POLL_INTERVAL_SECONDS", 900),
        wb_feedbacks_api_key=(
            os.getenv("WB_FEEDBACKS_API_KEY", "").strip()
            or os.getenv("WB_SELLER_API_KEY", "").strip()
        ),
        feedback_signature=os.getenv("FEEDBACK_SIGNATURE", "").strip(),
        feedback_max_per_cycle=_to_int("FEEDBACK_MAX_PER_CYCLE", 10),
        agent_chat_enabled=_to_bool("AGENT_CHAT_ENABLED", True),
        agent_think=_to_bool("AGENT_THINK", False),
        agent_max_iterations=_to_int("AGENT_MAX_ITERATIONS", 6),
        agent_history_limit=_to_int("AGENT_HISTORY_LIMIT", 16),
    )

    # Startup-проверка безопасности: вне shadow-режима мутирующие inline-кнопки
    # подписываются HMAC, поэтому секрет обязателен. В shadow-режиме таких
    # кнопок нет — пустой секрет допустим.
    if not config.shadow_mode and not config.callback_signing_secret:
        raise ValueError(
            "CALLBACK_SIGNING_SECRET обязателен при SHADOW_MODE=false "
            "(подпись мутирующих inline-кнопок). Сгенерируй: openssl rand -hex 32"
        )

    if not config.allowed_user_ids:
        logging.getLogger("config").warning(
            "ALLOWED_USER_IDS пуст — бот закрыт для ВСЕХ (deny-by-default). "
            "Добавь свой Telegram ID в ALLOWED_USER_IDS, чтобы пользоваться ботом."
        )

    return config
