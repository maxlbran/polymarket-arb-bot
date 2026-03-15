"""
Kalshi exchange connector.
API: https://api.elections.kalshi.com/trade-api/v2

Auth: Authorization: Token <KALSHI_API_KEY>

Strategy: Full market pagination — no series whitelist.
Fetches ALL active Kalshi markets by paginating the /markets endpoint
without a series_ticker filter. Filters out MVE parlays (mve_collection_ticker)
in Python. Stops early once the close_time horizon exceeds our scan window.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from dateutil import parser as dateutil_parser

import config
from database.models import NormalisedMarket, OrderBook
from exchanges.base import BaseExchange, ExchangeError

logger = logging.getLogger(__name__)

KALSHI_TOKEN = (
    os.getenv("KALSHI_API_KEY", "").strip()
    or os.getenv("KALSHI_API_KEY_ID", "").strip()
)

# Maximum pages to scan (100 markets/page). Safety cap to avoid infinite loops.
_MAX_PAGES = 500

# Stop early if this many consecutive pages have NO markets within our horizon.
_EARLY_STOP_PAGES = 8


def _map_category(text: str) -> str:
    if not text:
        return "Other"
    lower = text.lower()
    for keyword, standard in config.KALSHI_CATEGORY_MAP.items():
        if keyword in lower:
            return standard
    for keyword, standard in config.POLYMARKET_CATEGORY_MAP.items():
        if keyword in lower:
            return standard
    return "Other"


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _get_price(raw: dict, *keys, default: float = 0.0) -> float:
    """Extract price from multiple candidate keys; handle string values."""
    for key in keys:
        v = raw.get(key)
        if v is not None:
            f = _safe_float(v, -1.0)
            if f >= 0:
                return f / 100.0 if f > 1.0 else f
    return default


class KalshiConnector(BaseExchange):
    """
    Fetches ALL Kalshi markets via full pagination of /markets endpoint.
    No series whitelist — covers crypto, economics, politics, sports, everything.
    """

    BASE_URL = config.KALSHI_BASE_URL

    def __init__(self) -> None:
        super().__init__()
        self._auth_available: bool = bool(KALSHI_TOKEN)
        if not self._auth_available:
            logger.warning("[kalshi] KALSHI_API_KEY not set in .env")

    @property
    def _platform_name(self) -> str:
        return "kalshi"

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Token {KALSHI_TOKEN}"} if KALSHI_TOKEN else {}

    # ─── Health check ────────────────────────────────────────────────────────

    async def _health_probe(self) -> None:
        if not self._auth_available:
            raise ExchangeError("Kalshi: KALSHI_API_KEY not configured")
        await self._get(
            f"{self.BASE_URL}/markets",
            params={"limit": 1, "status": "open"},
            headers=self._auth_headers(),
        )

    # ─── Main fetch ──────────────────────────────────────────────────────────

    async def fetch_markets(self) -> list[NormalisedMarket]:
        """
        Paginate ALL active Kalshi markets within our time horizon.

        Uses the Kalshi `max_close_ts` API filter to constrain results to
        markets closing within our horizon window. This avoids paginating through
        hundreds of thousands of MVE parlays (which dominate the unfiltered feed)
        and goes straight to the near-term real markets we care about.

        MVE parlays (mve_collection_ticker set) are still filtered in Python
        since they may appear within the time window too.
        """
        if not self._auth_available:
            logger.warning("[kalshi] No credentials — skipping Kalshi fetch")
            return []

        t0 = time.monotonic()
        markets: list[NormalisedMarket] = []
        seen_ids: set[str] = set()

        # Scan horizon: 3× MAX_DAYS_TO_RESOLUTION, minimum 90 days.
        # The `max_close_ts` API filter is our primary boundary;
        # the in-Python horizon_dt check below is a second layer.
        horizon_days = max(config.MAX_DAYS_TO_RESOLUTION * 3, 90)
        now_utc = datetime.now(tz=timezone.utc)
        horizon_dt = now_utc + timedelta(days=horizon_days)
        max_close_ts = int(horizon_dt.timestamp())

        cursor: Optional[str] = None
        page_num = 0
        consecutive_far_pages = 0
        total_raw = 0
        total_mve = 0

        logger.info(
            f"[kalshi] Starting market fetch (max_close_ts={horizon_days}d / {horizon_dt.date()})…"
        )

        while page_num < _MAX_PAGES:
            params: dict = {
                "limit": 100,
                "status": "open",
                "max_close_ts": max_close_ts,
            }
            if cursor:
                params["cursor"] = cursor

            cache_key = f"kalshi_full_p{page_num}"
            try:
                data = await self._get(
                    f"{self.BASE_URL}/markets",
                    params=params,
                    headers=self._auth_headers(),
                    cache_key=cache_key,
                )
            except ExchangeError as e:
                logger.warning(f"[kalshi] Page {page_num} fetch failed: {e}")
                break

            if not isinstance(data, dict):
                break

            page = data.get("markets", [])
            if not page:
                break

            total_raw += len(page)
            page_num += 1

            # Track whether this page had any near-horizon real (non-MVE) markets.
            # Pages containing only MVE parlays don't count toward the early-stop
            # counter — they provide no signal about how far-future real markets are.
            page_has_near = False
            page_real_count = 0  # non-MVE markets on this page

            for raw in page:
                # Skip MVE parlays (Multi-Variate Event collections)
                if raw.get("mve_collection_ticker"):
                    total_mve += 1
                    continue
                if raw.get("is_provisional", False):
                    continue

                page_real_count += 1

                # Check close_time — skip far-future markets but keep counting
                close_raw = raw.get("close_time", "") or raw.get("expiration_time", "")
                if close_raw:
                    try:
                        close_dt = dateutil_parser.parse(close_raw[:19]).replace(tzinfo=timezone.utc)
                        if close_dt <= horizon_dt:
                            page_has_near = True
                    except Exception:
                        page_has_near = True  # unknown date, keep it
                else:
                    page_has_near = True

                try:
                    market = self._normalise(raw)
                    if market and market.external_id not in seen_ids:
                        seen_ids.add(market.external_id)
                        markets.append(market)
                except Exception as e:
                    logger.debug(f"[kalshi] Normalise error {raw.get('ticker','?')}: {e}")

            # Early stop: if too many consecutive pages with real markets have
            # no near-term markets. MVE-only pages do NOT advance the counter.
            if page_real_count > 0:
                if not page_has_near:
                    consecutive_far_pages += 1
                    if consecutive_far_pages >= _EARLY_STOP_PAGES:
                        logger.info(
                            f"[kalshi] Early stop after {page_num} pages "
                            f"({_EARLY_STOP_PAGES} consecutive far-future pages)"
                        )
                        break
                else:
                    consecutive_far_pages = 0

            if page_num % 20 == 0:
                logger.info(
                    f"[kalshi] Page {page_num} | Raw: {total_raw} | "
                    f"MVE filtered: {total_mve} | Markets: {len(markets)}"
                )

            cursor = data.get("cursor")
            if not cursor or len(page) < 100:
                break

            await asyncio.sleep(1.0)

        elapsed = time.monotonic() - t0
        self._last_fetch_time = time.monotonic()
        self._markets_count = len(markets)
        logger.info(
            f"[kalshi] Fetched {len(markets)} markets from {page_num} pages "
            f"(raw={total_raw}, mve_filtered={total_mve}) in {elapsed:.1f}s"
        )
        return markets

    # ─── Normalisation ───────────────────────────────────────────────────────

    def _normalise(self, raw: dict) -> Optional[NormalisedMarket]:
        ticker = raw.get("ticker", "")
        if not ticker:
            return None

        title = raw.get("title", "").strip()
        if not title:
            return None

        subtitle = raw.get("subtitle", "")
        description = f"{title}. {subtitle}".strip(" .")
        if raw.get("rules_primary"):
            description = raw["rules_primary"]

        yes_bid = _get_price(raw, "yes_bid_dollars", "yes_bid", default=0.0)
        yes_ask = _get_price(raw, "yes_ask_dollars", "yes_ask", default=0.0)

        if yes_bid == 0.0 and yes_ask == 0.0:
            last = _get_price(raw, "last_price_dollars", default=0.0)
            yes_bid = yes_ask = last if last > 0 else 0.5

        bid_ask_spread = max(0.0, yes_ask - yes_bid) if yes_ask > yes_bid else 0.0
        yes_mid = max(0.001, min(0.999, (yes_bid + yes_ask) / 2.0))
        no_mid = max(0.001, min(0.999, 1.0 - yes_mid))

        volume_raw = _safe_float(raw.get("volume_fp") or raw.get("volume") or 0)
        open_interest = _safe_float(raw.get("open_interest_fp") or raw.get("open_interest") or 0)
        volume_24h = _safe_float(raw.get("volume_24h_fp") or raw.get("volume_24h") or 0)
        liquidity = _safe_float(raw.get("liquidity_dollars") or 0) or (open_interest + volume_24h * 0.1)

        resolution_date = None
        for field in ("close_time", "expiration_time", "expected_expiration_time"):
            raw_date = raw.get(field, "")
            if raw_date:
                try:
                    resolution_date = dateutil_parser.parse(raw_date)
                    if resolution_date.tzinfo is None:
                        resolution_date = resolution_date.replace(tzinfo=timezone.utc)
                    break
                except Exception:
                    continue

        event_ticker = raw.get("event_ticker", "")
        series = raw.get("series_ticker", "")
        prefix = series.split("-")[0] if series else event_ticker.split("-")[0]
        category_text = f"{prefix} {title} {description}".strip()
        category = _map_category(category_text)

        status_raw = raw.get("status", "open").lower()
        status = "active" if status_raw in ("open", "active") else "closed"
        url = f"https://kalshi.com/markets/{event_ticker}"

        return NormalisedMarket(
            platform="kalshi",
            external_id=ticker,
            title=title,
            description=description,
            category=category,
            yes_price=yes_mid,
            no_price=no_mid,
            implied_probability=yes_mid,
            volume_24h=volume_24h,
            total_volume=volume_raw,
            liquidity_depth=liquidity,
            resolution_date=resolution_date,
            status=status,
            url=url,
            raw_json=json.dumps(raw),
            bid_ask_spread=bid_ask_spread,
        )

    # ─── Order book ──────────────────────────────────────────────────────────

    async def get_orderbook(self, market_id: str) -> Optional[OrderBook]:
        try:
            data = await self._get(
                f"{self.BASE_URL}/markets/{market_id}/orderbook",
                headers=self._auth_headers(),
                cache_key=f"kalshi_ob_{market_id}",
            )
        except ExchangeError:
            return None

        ob = OrderBook(market_id=market_id, platform="kalshi")
        book = data.get("orderbook", {})

        for level in book.get("yes", []):
            if isinstance(level, list) and len(level) >= 2:
                p = _safe_float(level[0])
                p = p / 100.0 if p > 1.0 else p
                ob.yes_bids.append((p, _safe_float(level[1])))

        for level in book.get("no", []):
            if isinstance(level, list) and len(level) >= 2:
                p = _safe_float(level[0])
                p = p / 100.0 if p > 1.0 else p
                ob.no_bids.append((p, _safe_float(level[1])))

        if ob.yes_bids and not ob.yes_asks:
            ob.yes_asks = [(1.0 - p, s) for p, s in ob.no_bids]
        if ob.no_bids and not ob.no_asks:
            ob.no_asks = [(1.0 - p, s) for p, s in ob.yes_bids]

        return ob
