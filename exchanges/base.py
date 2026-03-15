"""
Base exchange connector — abstract interface all exchanges must implement.
Handles retry logic, caching, and rate limiting at the base level.
"""
from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Union

import aiohttp

import config
from database.models import NormalisedMarket, OrderBook

logger = logging.getLogger(__name__)


class ExchangeError(Exception):
    """Raised when an exchange API call fails after all retries."""
    pass


class RateLimitError(ExchangeError):
    """Raised when the API returns a rate limit response."""
    pass


class BaseExchange(ABC):
    """
    Abstract base class for all exchange connectors.

    Subclasses must implement:
      - fetch_markets() -> list[NormalisedMarket]
      - get_orderbook(market_id) -> OrderBook
      - _platform_name property
    """

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: dict[str, tuple[float, any]] = {}   # key -> (timestamp, data)
        self._last_fetch_time: Optional[float] = None
        self._markets_count: int = 0
        self._error_count: int = 0
        self._api_call_count: int = 0
        self._cache_hit_count: int = 0

    # ─── Abstract interface ───────────────────────────────────────────────────

    @property
    @abstractmethod
    def _platform_name(self) -> str:
        """Short platform identifier, e.g. 'polymarket'."""
        ...

    @abstractmethod
    async def fetch_markets(self) -> list[NormalisedMarket]:
        """Fetch all active markets from the exchange. Must be implemented."""
        ...

    @abstractmethod
    async def get_orderbook(self, market_id: str) -> Optional[OrderBook]:
        """Fetch order book for a specific market."""
        ...

    # ─── Session management ───────────────────────────────────────────────────

    async def open(self) -> None:
        """Open the HTTP session. Call before using the connector."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=config.API_TIMEOUT_SECONDS)
            connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                headers=self._default_headers(),
            )
        logger.debug(f"[{self._platform_name}] HTTP session opened")

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
        logger.debug(f"[{self._platform_name}] HTTP session closed")

    def _default_headers(self) -> dict:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"PolyKalshiArbEngine/{config.VERSION}",
        }

    # ─── HTTP with retry ─────────────────────────────────────────────────────

    async def _get(
        self,
        url: str,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        cache_key: Optional[str] = None,
    ) -> Any:
        """
        GET request with:
          - Cache lookup (TTL = CACHE_TTL_SECONDS)
          - Exponential backoff retry (up to MAX_RETRIES)
          - Rate limit handling (429 → wait and retry once)
        """
        if cache_key:
            cached = self._cache_get(cache_key)
            if cached is not None:
                self._cache_hit_count += 1
                logger.debug(f"[{self._platform_name}] Cache hit: {cache_key}")
                return cached

        if self._session is None:
            await self.open()

        last_exc: Optional[Exception] = None
        for attempt in range(config.MAX_RETRIES):
            try:
                self._api_call_count += 1
                async with self._session.get(
                    url, params=params, headers=headers
                ) as resp:
                    if resp.status == 401:
                        # Auth failure — don't retry, fail fast with clear message
                        raise ExchangeError(
                            f"[{self._platform_name}] 401 Unauthorized — check KALSHI_API_KEY "
                            f"in .env (must be the UUID from kalshi.com/account/api-keys)"
                        )
                    if resp.status == 429:
                        # Respect Retry-After header if present, else exponential backoff
                        retry_after = resp.headers.get("Retry-After")
                        if retry_after:
                            wait = float(retry_after)
                        else:
                            wait = 30.0 * (2 ** attempt)  # 30s, 60s, 120s
                        logger.warning(
                            f"[{self._platform_name}] Rate limited. Waiting {wait:.0f}s"
                        )
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
                    if cache_key:
                        self._cache_set(cache_key, data)
                    return data
            except aiohttp.ClientResponseError as e:
                logger.warning(
                    f"[{self._platform_name}] HTTP {e.status} on attempt {attempt+1}: {url}"
                )
                last_exc = e
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning(
                    f"[{self._platform_name}] Request error attempt {attempt+1}: {e}"
                )
                last_exc = e

            if attempt < config.MAX_RETRIES - 1:
                backoff = config.RETRY_BACKOFF_BASE * (2 ** attempt)
                logger.debug(f"[{self._platform_name}] Retrying in {backoff}s…")
                await asyncio.sleep(backoff)

        self._error_count += 1
        raise ExchangeError(
            f"[{self._platform_name}] Failed after {config.MAX_RETRIES} attempts: {last_exc}"
        )

    # ─── Cache ────────────────────────────────────────────────────────────────

    def _cache_get(self, key: str) -> Optional[any]:
        entry = self._cache.get(key)
        if entry is None:
            return None
        ts, data = entry
        if time.monotonic() - ts > config.CACHE_TTL_SECONDS:
            del self._cache[key]
            return None
        return data

    def _cache_set(self, key: str, data: any) -> None:
        self._cache[key] = (time.monotonic(), data)

    def _cache_invalidate(self, key: str) -> None:
        self._cache.pop(key, None)

    # ─── Health check ─────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        """Return True if the API is reachable and responding correctly."""
        try:
            await self._health_probe()
            return True
        except Exception as e:
            logger.warning(f"[{self._platform_name}] Health check failed: {e}")
            return False

    @abstractmethod
    async def _health_probe(self) -> None:
        """Implement a lightweight API call to verify connectivity."""
        ...

    # ─── Properties ──────────────────────────────────────────────────────────

    @property
    def last_fetch_time(self) -> Optional[float]:
        return self._last_fetch_time

    @property
    def markets_count(self) -> int:
        return self._markets_count

    @property
    def error_count(self) -> int:
        return self._error_count

    @property
    def api_call_count(self) -> int:
        return self._api_call_count

    @property
    def cache_hit_count(self) -> int:
        return self._cache_hit_count

    @property
    def cache_hit_rate(self) -> float:
        total = self._api_call_count + self._cache_hit_count
        if total == 0:
            return 0.0
        return self._cache_hit_count / total
