from __future__ import annotations

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
    # If False, suppress per-order and per-sale Telegram notifications
    # (the "you got a new order!" spam). Price-drop alerts are NOT affected.
    event_alerts_enabled: bool

    @property
    def owner_mode_enabled(self) -> bool:
        return bool(self.allowed_user_ids)

    def is_user_allowed(self, user_id: int | None) -> bool:
        if not self.owner_mode_enabled:
            return True
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

    return AppConfig(
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
            os.getenv("TOP10_EXCLUDE_KEYWORDS", "жёлт,желт,yellow")
        ),
        event_alerts_enabled=_to_bool("EVENT_ALERTS_ENABLED", True),
    )
