"""ArbitrageScanner: main orchestrator for the autonomous WB→WB scanner.

Round 4 paradigm: category-first. Each enabled query brings a cohort of SKUs
from the same WB subject; scanner ranks them by margin and pushes alerts for
deals passing the canonical threshold.

Flow per scan_once():
    1. Load enabled arb_queries.
    2. For each query: search_for_arbitrage_raw() → ~100 raw products.
    3. Build cohort metrics (median, P25, min) from cohort prices.
    4. Per item:
       a. Resolve buyer-side СПП (PersonalSppResolver).
       b. If СПП unresolvable → skip (no default alert).
       c. Lookup tariffs (commission for subject, logistics by volume).
       d. Compute margin via compute_arbitrage_margin.
       e. Threshold check + alert dedup + daily cap.
    5. Send Telegram alerts via bot, mark_alerted.

Exclude rules (Round 4):
    - SKU which appears in own_sales / purchases last 90 days (self-listing).
    - cohort_size < ARBITRAGE_COHORT_MIN_SIZE.
    - confidence='low' on СПП.
"""
from __future__ import annotations

import asyncio
import logging
import statistics
from datetime import datetime, timezone

from aiogram import Bot

from app.arbitrage.formatting import build_alert_message
from app.arbitrage.margin import (
    MarginBreakdown,
    compute_arbitrage_margin,
    decompose_composite_spp,
    estimate_hold_days_from_feedbacks,
    passes_threshold,
)
from app.arbitrage.repository import ArbitrageRepository
from app.arbitrage.spp_resolver import PersonalSppResolver
from app.arbitrage.tariffs_repository import TariffsRepository
from app.config import AppConfig
from app.storage.business_repository import BusinessRepository
from app.storage.repositories import SubscriberRepository
from app.wb.client import WildberriesClient

logger = logging.getLogger(__name__)


class ArbitrageScanner:
    def __init__(
        self,
        *,
        config: AppConfig,
        wb_client: WildberriesClient,
        bot: Bot,
        arb_repo: ArbitrageRepository,
        tariffs_repo: TariffsRepository,
        spp_resolver: PersonalSppResolver,
        business_repo: BusinessRepository,
        subscriber_repo: SubscriberRepository,
    ) -> None:
        self._config = config
        self._wb = wb_client
        self._bot = bot
        self._repo = arb_repo
        self._tariffs = tariffs_repo
        self._spp = spp_resolver
        self._business = business_repo
        self._subs = subscriber_repo
        self._own_nm_cache: set[int] = set()
        self._own_nm_cache_at: datetime | None = None

    async def scan_once(self) -> dict[str, int]:
        """Run a single scan over all enabled queries. Returns summary counts."""
        queries = await self._repo.list_queries(only_enabled=True)
        if not queries:
            logger.debug("ARBITRAGE: no enabled queries")
            return {"queries": 0, "candidates": 0, "alerted": 0}

        # Refresh own listings cache hourly
        await self._refresh_own_nm_cache()

        total_candidates = 0
        new_alerts_this_scan = 0
        daily_cap = self._config.arbitrage_daily_alert_cap

        # /review fix: daily cap должен быть TRULY daily (24h rolling), не per-scan.
        # Без этого scanner раз в 10 мин может отправить cap × 144 alerts/day.
        already_alerted_today = await self._repo.alerts_today_count()
        if already_alerted_today >= daily_cap:
            logger.info(
                "ARBITRAGE: daily cap %d already reached (%d alerts in last 24h), skip scan",
                daily_cap, already_alerted_today,
            )
            return {"queries": len(queries), "candidates": 0, "alerted": 0}

        for q in queries:
            try:
                effective_alerted = already_alerted_today + new_alerts_this_scan
                c, a = await self._scan_query(
                    q, daily_cap=daily_cap, alerted_today=effective_alerted,
                )
                total_candidates += c
                new_alerts_this_scan += a
                if already_alerted_today + new_alerts_this_scan >= daily_cap:
                    logger.info(
                        "ARBITRAGE: daily alert cap %d reached (24h total)", daily_cap,
                    )
                    break
            except Exception:
                logger.exception("ARBITRAGE: scan_query failed for %r", q.get("query"))

        logger.info(
            "ARBITRAGE: scan_once done queries=%d candidates=%d alerted=%d (24h total=%d)",
            len(queries), total_candidates, new_alerts_this_scan,
            already_alerted_today + new_alerts_this_scan,
        )
        return {
            "queries": len(queries),
            "candidates": total_candidates,
            "alerted": new_alerts_this_scan,
        }

    async def _scan_query(
        self, q: dict, *, daily_cap: int, alerted_today: int,
    ) -> tuple[int, int]:
        """Per-query scan with subject-grouped cohort metrics.

        Codex /review fix #1: WB search returns mixed categories (e.g.
        "Робот пылесос" → пылесосы + фильтры + аксессуары). Cohort metrics
        computed across all results give garbage P25 anchor. We now group
        products by subjectId and only scan within the DOMINANT subject
        (or the subject explicitly tied to the query).
        """
        query_text = q["query"]
        query_id = q["id"]
        target_subject_id = q.get("subject_id")
        try:
            products = await self._wb.search_for_arbitrage_raw(query_text, max_pages=2)
        except Exception:
            logger.exception("ARBITRAGE: WB search failed for %r", query_text)
            await self._repo.mark_scanned(query_id, 0)
            return (0, 0)

        if len(products) < self._config.arbitrage_cohort_min_size:
            logger.info(
                "ARBITRAGE: %r cohort too small (%d < %d), skip",
                query_text, len(products), self._config.arbitrage_cohort_min_size,
            )
            await self._repo.mark_scanned(query_id, len(products))
            return (0, 0)

        # Group products by subjectId to find dominant subject for cohort.
        # Without this filter, P25/median across mixed subjects = garbage.
        by_subject: dict[int, list[dict]] = {}
        for p in products:
            sid = p.get("subjectId")
            if isinstance(sid, int):
                by_subject.setdefault(sid, []).append(p)

        if not by_subject:
            logger.warning("ARBITRAGE: %r no products with subjectId, skip", query_text)
            await self._repo.mark_scanned(query_id, 0)
            return (0, 0)

        if target_subject_id and target_subject_id in by_subject:
            dominant_subject = target_subject_id
        else:
            dominant_subject = max(by_subject, key=lambda sid: len(by_subject[sid]))

        focused = by_subject[dominant_subject]

        # Этап 1: per-query deterministic color/variant filter (NO LLM).
        # Applied BEFORE cohort metrics so P25/median/min anchor on the
        # correct product set. Keywords are per-query (empty → no filtering),
        # so a Станция query filters by colour while a robot-vacuum query is
        # left untouched.
        focused_before = len(focused)
        focused = _filter_cohort_by_keywords(
            focused,
            include=q.get("include_keywords"),
            exclude=q.get("exclude_keywords"),
        )
        if len(focused) != focused_before:
            logger.info(
                "ARBITRAGE: %r cohort %d → %d after color/variant filter",
                query_text, focused_before, len(focused),
            )

        if len(focused) < self._config.arbitrage_cohort_min_size:
            logger.info(
                "ARBITRAGE: %r dominant subject %d cohort too small (%d), skip",
                query_text, dominant_subject, len(focused),
            )
            await self._repo.mark_scanned(query_id, len(focused))
            return (0, 0)

        # Cohort metrics from public prices WITHIN dominant subject only.
        prices_rub = [_parse_price_rub(p) for p in focused]
        prices_rub = [p for p in prices_rub if p > 0]
        if len(prices_rub) < self._config.arbitrage_cohort_min_size:
            await self._repo.mark_scanned(query_id, len(prices_rub))
            return (0, 0)

        prices_rub.sort()
        market_median = int(statistics.median(prices_rub))
        market_p25 = int(prices_rub[len(prices_rub) // 4])
        market_min = int(prices_rub[0])
        cohort_size = len(prices_rub)
        # Persist dominant subject + sample subject_name so /arb_list can
        # show the owner WHICH WB subject this query maps to (helps them
        # add observations via /arb_observe for the right category).
        sample_subj_name = next(
            ((p.get("subjectName") or "").strip() for p in focused
             if (p.get("subjectName") or "").strip()),
            None,
        )
        await self._repo.update_query_subject(
            query_id, subject_id=dominant_subject, subject_name=sample_subj_name,
        )
        logger.info(
            "ARBITRAGE: %r → subject %d (%s), cohort=%d, P25=%d₽, median=%d₽",
            query_text, dominant_subject, sample_subj_name or "?",
            cohort_size, market_p25, market_median,
        )

        candidates_count = 0
        alerted_count = 0

        # Only iterate products in dominant subject — not the noisy mix.
        for prod in focused:
            if alerted_today + alerted_count >= daily_cap:
                break
            try:
                hit = await self._evaluate_product(
                    prod,
                    query_text=query_text,
                    market_median=market_median,
                    market_p25=market_p25,
                    market_min=market_min,
                    cohort_size=cohort_size,
                )
            except Exception:
                logger.exception("ARBITRAGE: evaluate failed for nm=%s", prod.get("id"))
                continue
            if hit is None:
                continue
            candidates_count += 1
            if hit:
                alerted_count += 1

        # Record COHORT size (not candidates_count) so user sees "found N
        # products in category X" — even when 0 passed SPP resolution.
        # /arb_list shows hint if found > 0 but no observations exist.
        await self._repo.mark_scanned(query_id, cohort_size)
        return (candidates_count, alerted_count)

    async def _evaluate_product(
        self,
        prod: dict,
        *,
        query_text: str,
        market_median: int,
        market_p25: int,
        market_min: int,
        cohort_size: int,
    ) -> bool | None:
        """Returns True if alerted, False if candidate-not-alerted, None if skipped."""
        nm_id = prod.get("id")
        if not isinstance(nm_id, int) or nm_id <= 0:
            return None

        # Exclude self-listings (Round 4 D34)
        if nm_id in self._own_nm_cache:
            return None

        market_price_rub = _parse_price_rub(prod)
        if market_price_rub <= 0:
            return None

        # Outlier guard: WB sometimes mislabels accessories with the same
        # subjectId as the main product (e.g. подставки for Станции). Their
        # cheap price + cohort P25 anchor creates fake 1000%+ margins.
        # Skip items priced <50% of P25 (the realistic floor for the cohort).
        # ``market_p25`` and ``market_min`` are passed in from _scan_query.
        if market_p25 > 0 and market_price_rub < market_p25 // 2:
            return None

        subject_id = prod.get("subjectId") if isinstance(prod.get("subjectId"), int) else None
        # /review fix: extract subjectName from raw payload for /arb_my_spp display.
        # Falls back to None if WB omits it (then /arb_my_spp shows '?').
        subject_name = (prod.get("subjectName") or "").strip() or None
        name = (prod.get("name") or "").strip() or None
        brand = (prod.get("brand") or "").strip() or None
        feedbacks = int(prod.get("feedbacks") or 0)
        volume_l = float(prod.get("volume") or 1)

        # Resolve buyer-side СПП.
        # 2026-05-18 ground-truth update: WB-Скидка (бывш. СПП) is category-WIDE
        # (one value applied to every buyer in the subject), funded by WB at
        # its own expense per offer 5.4. So category_avg observations are
        # JUST AS VALID as per-nm — we removed the old gating that blocked it.
        spp = await self._spp.resolve(nm_id=nm_id, subject_id=subject_id)
        if spp is None:
            return None  # skip — no observation data at all

        # Tariffs lookup
        commission_pct = None
        if subject_id is not None:
            commission_pct = await self._tariffs.get_commission_fbs(subject_id)
        logistics_rub = await self._tariffs.estimate_logistics_for_volume(volume_l)

        # Hold time proxy from feedbacks
        expected_hold_days = estimate_hold_days_from_feedbacks(feedbacks)

        # Decompose observed composite SPP (= category_СПП + wallet_bonus
        # baked together) into category_only. Wallet is applied separately
        # to buy_price in compute_arbitrage_margin.
        wallet_pct = self._config.arbitrage_wallet_bonus_pct
        category_spp_pct = decompose_composite_spp(spp.spp_percent, wallet_pct=wallet_pct)

        margin = compute_arbitrage_margin(
            market_price_rub=market_price_rub,
            category_spp_pct=category_spp_pct,
            wallet_bonus_pct=wallet_pct,
            spp_source=spp.source,
            spp_confidence=spp.confidence,
            # By default match competitor's implied listed (reverse from
            # their market price). Owner can override strategy later.
            listed_price_rub=None,
            commission_pct=commission_pct,
            logistics_rub=logistics_rub,
            acquiring_percent=self._config.profit_acquiring_percent,
            tax_percent=self._config.profit_tax_percent,
            return_rate_percent=self._config.return_rate_percent,
            storage_cost_per_day_rub=self._config.storage_cost_per_day_rub,
            expected_hold_days=expected_hold_days,
        )

        threshold_ok = passes_threshold(
            margin,
            min_pprd_percent=self._config.arbitrage_min_pprd_percent,
            min_profit_rub=self._config.arbitrage_min_profit_rub,
            min_margin_percent=self._config.arbitrage_min_margin_percent,
        )

        url = f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx"

        # Composite buyer discount (category + wallet) for legacy `spp_percent_used`
        # display field. Real components stored on candidate via new schema.
        composite_pct = (1.0 - (1.0 - category_spp_pct / 100.0) * (1.0 - wallet_pct / 100.0)) * 100.0

        cand_id = await self._repo.record_candidate(
            nm_id=nm_id,
            query=query_text,
            subject_id=subject_id,
            name=name,
            brand=brand,
            market_price_rub=market_price_rub,
            market_median_rub=market_median,
            market_p25_rub=market_p25,
            market_min_rub=market_min,
            buyer_price_rub=margin.buy_price_rub,
            spp_percent_used=composite_pct,
            spp_source=margin.spp_source,
            spp_confidence=margin.spp_confidence,
            listed_price_rub=margin.listed_implied_rub,
            commission_pct=margin.commission_pct,
            commission_rub=margin.commission_rub,
            logistics_rub=margin.logistics_rub,
            acquiring_rub=margin.acquiring_rub,
            return_reserve_rub=margin.return_reserve_rub,
            tax_rub=margin.tax_rub,
            holding_rub=margin.holding_rub,
            revenue_after_wb_rub=margin.revenue_after_wb_rub,
            margin_rub=margin.margin_rub,
            margin_percent=margin.margin_percent,
            profit_per_ruble_day_pct=margin.profit_per_ruble_day_pct,
            expected_hold_days=expected_hold_days,
            cohort_size=cohort_size,
            url=url,
        )

        if not threshold_ok:
            return False

        # 2026-05-18 model update: category_avg IS valid for alert.
        # WB-Скидка is category-wide, financed by WB (offer 5.4) — not a
        # per-buyer personal estimate. Removed old per-nm-only gate.
        # Still skip when both tariffs missing (silent 16%/500₽ unsafe for
        # categories we've never seen). Confidence='low' already filtered
        # by passes_threshold.
        if commission_pct is None and logistics_rub is None:
            logger.info(
                "ARBITRAGE: nm=%s threshold ok but tariffs unverified — skip alert",
                nm_id,
            )
            return False

        # Cooldown: don't re-alert same nm_id within N hours
        if await self._repo.recently_alerted(nm_id, hours=self._config.arbitrage_alert_cooldown_hours):
            return False

        # Send Telegram alert
        text = build_alert_message(
            nm_id=nm_id, name=name, brand=brand, query=query_text,
            margin=margin, cohort_size=cohort_size,
            market_median=market_median, market_p25=market_p25, market_min=market_min,
            url=url,
        )
        sent = await self._broadcast(text)
        if sent > 0:
            await self._repo.mark_alerted(cand_id)
            logger.info("ARBITRAGE: alert sent nm=%s margin=%d₽ pprd=%.3f%% to %d subs",
                        nm_id, margin.margin_rub, margin.profit_per_ruble_day_pct, sent)
            return True
        return False

    async def _broadcast(self, text: str) -> int:
        chat_ids = await self._subs.list_active_chat_ids()
        sent = 0
        for chat_id in chat_ids:
            try:
                await self._bot.send_message(chat_id, text, disable_web_page_preview=True)
                sent += 1
            except Exception:
                logger.exception("ARBITRAGE: send_message failed for chat=%s", chat_id)
        return sent

    async def _refresh_own_nm_cache(self) -> None:
        now = datetime.now(timezone.utc)
        if self._own_nm_cache_at is not None:
            age = (now - self._own_nm_cache_at).total_seconds()
            if age < 3600:
                return
        try:
            nm_ids = await self._business.list_recent_own_nm_ids(days=90)
            self._own_nm_cache = set(nm_ids)
            self._own_nm_cache_at = now
            logger.info("ARBITRAGE: own_nm_cache refreshed: %d nm_ids", len(self._own_nm_cache))
        except Exception:
            logger.exception("ARBITRAGE: failed to refresh own_nm_cache (using stale)")


def _parse_keyword_csv(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [kw.strip().lower() for kw in str(raw).split(",") if kw.strip()]


def _filter_cohort_by_keywords(
    products: list[dict], *, include: str | None, exclude: str | None,
) -> list[dict]:
    """Деterministic per-query color/variant filter on product ``name``.

    Mirrors ``WildberriesClient._filter_relevant_items`` semantics: exclude
    pass first (drop any name containing a blocked keyword), then include
    whitelist (keep only names containing ≥1 include keyword). No keywords
    set → list returned unchanged.
    """
    inc = _parse_keyword_csv(include)
    exc = _parse_keyword_csv(exclude)
    if not inc and not exc:
        return products
    result: list[dict] = []
    for p in products:
        name = (p.get("name") or "").lower()
        if exc and any(kw in name for kw in exc):
            continue
        if inc and not any(kw in name for kw in inc):
            continue
        result.append(p)
    return result


def _parse_price_rub(prod: dict) -> int:
    """Extract sale_price in rubles from raw WB product dict.

    WB u-search/v18 (2026-05) moved prices into ``sizes[0].price.product``
    (after seller discount, kopecks). ``sizes[0].price.basic`` is regular.
    Legacy v9-v14: top-level ``salePriceU`` / ``priceU``.

    Returns 0 on missing/unparseable. Logs warning to detect drift.
    """
    # v18 shape — preferred
    sizes = prod.get("sizes")
    if isinstance(sizes, list) and sizes:
        first = sizes[0] if isinstance(sizes[0], dict) else None
        if first:
            price_obj = first.get("price")
            if isinstance(price_obj, dict):
                product_price = price_obj.get("product")
                if product_price:
                    try:
                        return int(product_price) // 100
                    except (TypeError, ValueError):
                        pass
                basic = price_obj.get("basic")
                if basic:
                    try:
                        return int(basic) // 100
                    except (TypeError, ValueError):
                        pass

    # Legacy fallback
    raw = prod.get("salePriceU") or prod.get("priceU")
    if raw is None or raw == 0:
        logger.debug("ARBITRAGE: no price for nm=%s (no sizes.price, no priceU)", prod.get("id"))
        return 0
    try:
        return int(raw) // 100
    except (TypeError, ValueError):
        logger.warning(
            "ARBITRAGE: unparseable price for nm=%s sizes[0].price=%r salePriceU=%r",
            prod.get("id"),
            sizes[0].get("price") if isinstance(sizes, list) and sizes else None,
            prod.get("salePriceU"),
        )
        return 0
