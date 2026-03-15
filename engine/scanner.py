"""
Main Scanning Loop — orchestrates the entire arb detection pipeline.

Cycle:
  1. Health check both APIs
  2. Fetch all markets concurrently
  3. Persist raw markets to DB
  4. Run matching engine
  5. Calculate arb for all matches
  6. Risk score and filter
  7. Update dashboard
  8. Fire alerts for TIER 1
  9. Log scan metadata
  10. Sleep until next scan (adaptive timing)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    import psutil as _psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False

import config
from database.db import DatabaseManager
from database.models import ArbOpportunity, ScanResult
from exchanges.polymarket import PolymarketConnector
from exchanges.kalshi import KalshiConnector
from engine.matcher import MatchingEngine
from engine.calculator import ArbCalculator
from engine.risk import RiskEngine
from alerts.notifier import Notifier
from display.dashboard import Dashboard

logger = logging.getLogger(__name__)


def _is_quiet_hours() -> bool:
    """Return True if current UTC time is in the quiet period (2am–6am)."""
    hour = datetime.now(tz=timezone.utc).hour
    return config.QUIET_HOURS_START <= hour < config.QUIET_HOURS_END


def _scan_interval(last_tier1_at: Optional[datetime], live_opps: list | None = None) -> int:
    """
    Return the number of seconds to sleep before next scan.
    Adaptive based on live opportunity urgency, TIER1 activity, and time of day.

    Priority order:
      1. TIER1 boost (2min) — recent tier1 found
      2. Hot window (5min)  — any opp resolves in < 7 days
      3. Warm window (15min) — any opp resolves in < 30 days
      4. Quiet hours (30min)
      5. Normal (config default, 10min)
    """
    now = datetime.now(tz=timezone.utc)

    if last_tier1_at:
        elapsed = (now - last_tier1_at).total_seconds() / 60.0
        if elapsed < config.TIER1_BOOST_DURATION_MINUTES:
            return config.TIER1_BOOST_INTERVAL_MINUTES * 60

    if live_opps:
        min_days = min((o.days_to_resolution for o in live_opps), default=999)
        if min_days < 7:
            return 5 * 60   # 5 min — imminent resolution
        if min_days < 30:
            return 15 * 60  # 15 min — short-term opportunity live

    if _is_quiet_hours():
        return config.QUIET_INTERVAL_MINUTES * 60

    return config.SCAN_INTERVAL_MINUTES * 60


class Scanner:
    """
    Runs the main scan loop indefinitely.
    Manages all sub-components and coordinates the full pipeline.
    """

    def __init__(self, db: DatabaseManager, dashboard: Dashboard, notifier: Notifier) -> None:
        self.db = db
        self.dashboard = dashboard
        self.notifier = notifier
        self.poly = PolymarketConnector()
        self.kalshi = KalshiConnector()
        self.matcher = MatchingEngine()
        self.calculator = ArbCalculator()
        self._last_tier1_at: Optional[datetime] = None
        self._running = False
        self._live_opportunities: list[ArbOpportunity] = []
        self._scan_number = 0
        self._total_api_calls = 0
        self._total_cache_hits = 0
        self._total_errors = 0
        # Tracks when each opportunity pair was first observed (persists across scans)
        self._first_seen: dict[str, datetime] = {}

    async def start(self) -> None:
        """Open connections and start the scan loop."""
        await self.poly.open()
        await self.kalshi.open()
        logger.info("[scanner] Connectors opened")
        self._running = True

        try:
            await self._run_loop()
        finally:
            await self._shutdown()

    async def stop(self) -> None:
        self._running = False

    async def _shutdown(self) -> None:
        await self.poly.close()
        await self.kalshi.close()
        logger.info("[scanner] Connectors closed")

    # ─── Main loop ────────────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        logger.info("[scanner] Scan loop starting")

        while self._running:
            self._scan_number += 1
            logger.info(f"\n[scanner] ═══ Scan #{self._scan_number} starting ═══")

            try:
                scan_result = await self._run_scan()
                interval = _scan_interval(self._last_tier1_at, self._live_opportunities)

                next_scan_at = datetime.now(tz=timezone.utc) + timedelta(seconds=interval)

                # Update dashboard with completed scan
                opp_today = await self.db.get_opportunity_count_today()
                best_spread = await self.db.get_best_spread_today()

                self.dashboard.update(
                    opportunities=self._live_opportunities,
                    scan_result=scan_result,
                    poly_healthy=self.poly_healthy,
                    kalshi_healthy=self.kalshi_healthy,
                    next_scan_at=next_scan_at,
                    api_calls=self._total_api_calls,
                    cache_hits=self._total_cache_hits,
                    errors=self._total_errors,
                    best_spread_today=best_spread,
                    opp_count_today=opp_today,
                )

                logger.info(
                    f"[scanner] Scan #{self._scan_number} complete. "
                    f"Next in {interval//60}m {interval%60}s"
                )

                # Sleep, but wake up to update the dashboard countdown every 30s
                slept = 0
                while slept < interval and self._running:
                    await asyncio.sleep(min(30, interval - slept))
                    slept += 30
                    # Keep dashboard countdown live
                    self.dashboard.update(
                        opportunities=self._live_opportunities,
                        poly_healthy=self.poly_healthy,
                        kalshi_healthy=self.kalshi_healthy,
                        next_scan_at=next_scan_at,
                        api_calls=self._total_api_calls,
                        cache_hits=self._total_cache_hits,
                        errors=self._total_errors,
                    )

            except KeyboardInterrupt:
                logger.info("[scanner] KeyboardInterrupt — stopping")
                break
            except Exception as e:
                self._total_errors += 1
                logger.error(f"[scanner] Unhandled error in scan loop: {e}", exc_info=True)
                await asyncio.sleep(60)  # Brief pause before retry

    # ─── Single scan cycle ────────────────────────────────────────────────────

    async def _run_scan(self) -> ScanResult:
        t_start = time.monotonic()
        started_at = datetime.now(tz=timezone.utc)
        scan_id = await self.db.start_scan(started_at)
        errors = 0

        # Step 1: Health check
        self.poly_healthy, self.kalshi_healthy = await asyncio.gather(
            self.poly.health_check(),
            self.kalshi.health_check(),
        )
        logger.info(
            f"[scanner] Health: Polymarket={'OK' if self.poly_healthy else 'DOWN'} | "
            f"Kalshi={'OK' if self.kalshi_healthy else 'DOWN'}"
        )

        # Step 2: Fetch markets concurrently
        poly_markets, kalshi_markets = [], []
        try:
            poly_markets, kalshi_markets = await asyncio.gather(
                self.poly.fetch_markets(),
                self.kalshi.fetch_markets(),
                return_exceptions=False,
            )
        except Exception as e:
            logger.error(f"[scanner] Market fetch failed: {e}")
            errors += 1

        # Step 3: Persist markets
        if poly_markets:
            await self.db.upsert_markets_bulk(poly_markets)
        if kalshi_markets:
            await self.db.upsert_markets_bulk(kalshi_markets)

        # Step 4: Match markets
        matches = []
        if poly_markets and kalshi_markets:
            matches = self.matcher.find_matches(poly_markets, kalshi_markets)
        logger.info(f"[scanner] Matches found: {len(matches)}")

        # Step 5: Calculate arbitrage
        opportunities = self.calculator.calculate_all(matches)

        # Step 6: Risk scoring and filtering
        risk_engine = RiskEngine(
            poly_healthy=self.poly_healthy,
            kalshi_healthy=self.kalshi_healthy,
        )
        scored = risk_engine.score_all(opportunities)
        safe_opps = risk_engine.filter_safe(scored)

        # Step 6b: Exclude markets resolving beyond MAX_DAYS_TO_RESOLUTION
        safe_opps = [
            o for o in safe_opps
            if o.days_to_resolution <= config.MAX_DAYS_TO_RESOLUTION
        ]

        # Step 7: Assign scan_id, stamp first_seen, and persist
        tier_counts = {1: 0, 2: 0, 3: 0}
        now_utc = datetime.now(tz=timezone.utc)
        for opp in safe_opps:
            opp.scan_id = scan_id
            # Track when this pair was first observed
            pair_key = (
                f"{opp.match.market_poly.external_id}:"
                f"{opp.match.market_kalshi.external_id}"
            )
            if pair_key not in self._first_seen:
                self._first_seen[pair_key] = now_utc
            opp.first_seen_at = self._first_seen[pair_key]
            try:
                poly_db_id = await self._get_market_db_id(opp.match.market_poly)
                kalshi_db_id = await self._get_market_db_id(opp.match.market_kalshi)
                opp.db_id = await self.db.save_opportunity(opp, poly_db_id, kalshi_db_id)
            except Exception as e:
                logger.error(f"[scanner] Failed to save opportunity: {e}")
            tier_counts[opp.tier] = tier_counts.get(opp.tier, 0) + 1

            if opp.tier == 1:
                self._last_tier1_at = datetime.now(tz=timezone.utc)

            # Persist to opportunity_tracking table (Part 10)
            try:
                await self.db.upsert_opportunity_tracking(
                    poly_external_id=opp.match.market_poly.external_id,
                    kalshi_external_id=opp.match.market_kalshi.external_id,
                    spread=opp.net_spread,
                    first_detected_at=opp.first_seen_at or opp.detected_at,
                )
            except Exception as e:
                logger.debug(f"[scanner] Tracking upsert failed: {e}")

        # Update live opportunities list
        self._live_opportunities = safe_opps

        # Step 8: Expire stale opportunities and prune old markets
        await self.db.expire_old_opportunities()
        await self.db.prune_old_markets(days=7)

        # Step 9: Fire alerts
        self.notifier.notify_all(safe_opps)

        # Step 10: Record scan metadata
        t_end = time.monotonic()
        completed_at = datetime.now(tz=timezone.utc)
        duration_ms = int((t_end - t_start) * 1000)

        # Update API call counters
        self._total_api_calls = self.poly.api_call_count + self.kalshi.api_call_count
        self._total_cache_hits = self.poly.cache_hit_count + self.kalshi.cache_hit_count

        result = ScanResult(
            scan_id=scan_id,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
            markets_fetched_poly=len(poly_markets),
            markets_fetched_kalshi=len(kalshi_markets),
            matches_found=len(matches),
            tier1_count=tier_counts.get(1, 0),
            tier2_count=tier_counts.get(2, 0),
            tier3_count=tier_counts.get(3, 0),
            errors_count=errors,
            opportunities=safe_opps,
        )

        await self.db.complete_scan(result)

        # Resource monitor
        if _PSUTIL_AVAILABLE:
            try:
                proc = _psutil.Process(os.getpid())
                cpu_pct = proc.cpu_percent(interval=0.1)
                rss_mb = proc.memory_info().rss / 1_048_576
                logger.info(
                    f"[scanner] Resources — CPU: {cpu_pct:.1f}% | RAM: {rss_mb:.0f} MB"
                )
            except Exception:
                pass

        logger.info(
            f"[scanner] Scan done in {duration_ms}ms | "
            f"Poly:{len(poly_markets)} Kalshi:{len(kalshi_markets)} | "
            f"Matches:{len(matches)} | Safe opps:{len(safe_opps)} | "
            f"T1:{tier_counts.get(1,0)} T2:{tier_counts.get(2,0)} T3:{tier_counts.get(3,0)}"
        )
        return result

    async def _get_market_db_id(self, market) -> int:
        """Look up the DB id for a market by platform + external_id."""
        async with self.db._db.execute(
            "SELECT id FROM markets WHERE platform=? AND external_id=?",
            (market.platform, market.external_id),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0
