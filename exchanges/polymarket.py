"""
Polymarket exchange connector.
Uses the Gamma API: https://gamma-api.polymarket.com

Key notes:
- Markets are paginated; we fetch ALL pages
- outcomePrices are [yes_price, no_price] as strings
- liquidity is provided in USD
- endDate is ISO-8601 string or null
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

from dateutil import parser as dateutil_parser

import config
from database.models import NormalisedMarket, OrderBook
from exchanges.base import BaseExchange, ExchangeError

logger = logging.getLogger(__name__)


def _map_category(text: str) -> str:
    """
    Infer a standard category from raw text (title, category field, or tags).
    Polymarket category/tags fields are often null — we infer from title.
    """
    if not text:
        return "Other"
    lower = text.lower()
    for keyword, standard in config.POLYMARKET_CATEGORY_MAP.items():
        if keyword in lower:
            return standard
    return "Other"


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


class PolymarketConnector(BaseExchange):
    """
    Fetches all active Polymarket CLOB markets.
    Normalises them into the standard NormalisedMarket schema.
    """

    BASE_URL = config.POLYMARKET_BASE_URL

    @property
    def _platform_name(self) -> str:
        return "polymarket"

    # ─── Health check ────────────────────────────────────────────────────────

    async def _health_probe(self) -> None:
        url = f"{self.BASE_URL}/markets"
        await self._get(url, params={"limit": 1, "active": "true"})

    # ─── Market fetching ─────────────────────────────────────────────────────

    async def fetch_markets(self) -> list[NormalisedMarket]:
        """
        Paginate through ALL active Polymarket markets and return normalised list.
        """
        t0 = time.monotonic()
        markets: list[NormalisedMarket] = []
        offset = 0
        page_num = 0
        page_size = config.POLYMARKET_PAGE_SIZE
        seen_ids: set[str] = set()

        logger.info("[polymarket] Starting full market fetch…")

        while True:
            params = {
                "limit": page_size,
                "offset": offset,
                "active": "true",
                "closed": "false",
                "archived": "false",
            }
            cache_key = f"poly_markets_offset_{offset}"
            try:
                data = await self._get(
                    f"{self.BASE_URL}/markets",
                    params=params,
                    cache_key=cache_key,
                )
            except ExchangeError as e:
                logger.error(f"[polymarket] Failed to fetch page at offset {offset}: {e}")
                break

            # Gamma API returns a list directly
            if isinstance(data, dict):
                page = data.get("data", data.get("markets", []))
            elif isinstance(data, list):
                page = data
            else:
                logger.warning(f"[polymarket] Unexpected response type at offset {offset}")
                break

            if not page:
                break

            for raw in page:
                try:
                    market = self._normalise(raw)
                    if (
                        market
                        and market.external_id not in seen_ids
                        and market.status == "active"
                    ):
                        seen_ids.add(market.external_id)
                        markets.append(market)
                except Exception as e:
                    logger.warning(f"[polymarket] Failed to normalise market: {e} — {raw.get('id','?')}")

            page_num += 1
            if page_num % 10 == 0:
                logger.info(
                    f"[polymarket] Page {page_num} (offset={offset}): "
                    f"{len(markets)} markets fetched so far…"
                )
            else:
                logger.debug(f"[polymarket] Page {page_num} offset={offset}: {len(page)} markets (total: {len(markets)})")

            if len(page) < page_size:
                break
            offset += page_size

        elapsed = time.monotonic() - t0
        self._last_fetch_time = time.monotonic()
        self._markets_count = len(markets)
        logger.info(
            f"[polymarket] Fetched {len(markets)} active markets "
            f"across {page_num} pages in {elapsed:.1f}s"
        )
        return markets

    # ─── Normalisation ───────────────────────────────────────────────────────

    def _normalise(self, raw: dict) -> Optional[NormalisedMarket]:
        """Convert raw Gamma API dict to NormalisedMarket."""
        market_id = str(raw.get("id", ""))
        if not market_id:
            return None

        # Title / description
        question = raw.get("question", raw.get("title", "")).strip()
        description = raw.get("description", "").strip()

        if not question:
            return None

        # Outcome prices — Gamma returns as JSON-encoded string '["0.63","0.37"]'
        # or sometimes already a list. Handle both.
        raw_outcome_prices = raw.get("outcomePrices", [])
        raw_outcomes = raw.get("outcomes", ["Yes", "No"])

        if isinstance(raw_outcome_prices, str):
            try:
                outcome_prices = json.loads(raw_outcome_prices)
            except (json.JSONDecodeError, ValueError):
                outcome_prices = []
        else:
            outcome_prices = raw_outcome_prices or []

        if isinstance(raw_outcomes, str):
            try:
                outcomes = json.loads(raw_outcomes)
            except (json.JSONDecodeError, ValueError):
                outcomes = ["Yes", "No"]
        else:
            outcomes = raw_outcomes or ["Yes", "No"]

        yes_price = 0.5
        no_price = 0.5

        if outcome_prices and len(outcome_prices) >= 2:
            # Find YES index
            yes_idx = 0
            for i, o in enumerate(outcomes):
                if str(o).lower() in ("yes", "true", "1"):
                    yes_idx = i
                    break
            try:
                yes_price = _safe_float(outcome_prices[yes_idx])
                no_price = _safe_float(outcome_prices[1 - yes_idx])
            except IndexError:
                yes_price = _safe_float(outcome_prices[0])
                no_price = 1.0 - yes_price

        # Clamp prices to valid range
        yes_price = max(0.001, min(0.999, yes_price))
        no_price = max(0.001, min(0.999, no_price))

        # Volume and liquidity
        volume_24h = _safe_float(raw.get("volume24hr", raw.get("volumeNum", 0)))
        total_volume = _safe_float(raw.get("volume", raw.get("volumeNum", 0)))
        liquidity = _safe_float(raw.get("liquidity", raw.get("liquidityNum", 0)))

        # Resolution date
        resolution_date = None
        end_date_str = raw.get("endDate", raw.get("end_date_iso", ""))
        if end_date_str:
            try:
                resolution_date = dateutil_parser.parse(end_date_str)
                if resolution_date.tzinfo is None:
                    resolution_date = resolution_date.replace(tzinfo=timezone.utc)
            except Exception:
                pass

        # Category — infer from all available text since API often returns null
        tags = raw.get("tags") or []
        tag_str = " ".join(str(t) for t in tags) if tags else ""
        category_raw = raw.get("category") or ""
        # Combine all available text for category inference
        category_text = f"{category_raw} {tag_str} {question}".strip()
        category = _map_category(category_text)

        # Status
        active = raw.get("active", True)
        closed = raw.get("closed", False)
        archived = raw.get("archived", False)
        status = "active" if (active and not closed and not archived) else "closed"

        # URL
        slug = raw.get("slug", market_id)
        url = f"https://polymarket.com/event/{slug}"

        return NormalisedMarket(
            platform="polymarket",
            external_id=market_id,
            title=question,
            description=description,
            category=category,
            yes_price=yes_price,
            no_price=no_price,
            implied_probability=yes_price,
            volume_24h=volume_24h,
            total_volume=total_volume,
            liquidity_depth=liquidity,
            resolution_date=resolution_date,
            status=status,
            url=url,
            raw_json=json.dumps(raw),
        )

    # ─── Order book ──────────────────────────────────────────────────────────

    async def get_orderbook(self, market_id: str) -> Optional[OrderBook]:
        """
        Attempt to fetch CLOB order book data.
        Gamma API provides limited order book data; we use best-effort approach.
        Returns a synthetic OrderBook from market prices if deep book unavailable.
        """
        try:
            data = await self._get(
                f"{self.BASE_URL}/markets/{market_id}",
                cache_key=f"poly_ob_{market_id}",
            )
        except ExchangeError:
            return None

        ob = OrderBook(market_id=market_id, platform="polymarket")

        # Build synthetic order book from best price
        outcome_prices = data.get("outcomePrices", [])
        liquidity = _safe_float(data.get("liquidityNum", data.get("liquidity", 0)))

        if outcome_prices and len(outcome_prices) >= 2:
            yes_p = _safe_float(outcome_prices[0])
            no_p = _safe_float(outcome_prices[1])
            # Synthetic: single level at current price with available liquidity
            ob.yes_asks = [(yes_p, liquidity * yes_p)]
            ob.no_asks = [(no_p, liquidity * no_p)]

        return ob
