"""
Database manager — async SQLite via aiosqlite.
Handles all persistence: markets, opportunities, scans, executions.
"""
from __future__ import annotations

import json
import logging
import aiosqlite
from datetime import datetime, timedelta
from typing import Optional

import config
from database.models import NormalisedMarket, ArbOpportunity, ScanResult

logger = logging.getLogger(__name__)


CREATE_MARKETS = """
CREATE TABLE IF NOT EXISTS markets (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    platform          TEXT NOT NULL,
    external_id       TEXT NOT NULL,
    title             TEXT NOT NULL,
    description       TEXT,
    category          TEXT,
    yes_price         REAL,
    no_price          REAL,
    implied_probability REAL,
    volume_24h        REAL,
    total_volume      REAL,
    liquidity_depth   REAL,
    resolution_date   TEXT,
    status            TEXT DEFAULT 'active',
    url               TEXT,
    raw_json          TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE(platform, external_id)
);
"""

CREATE_MARKETS_IDX = """
CREATE INDEX IF NOT EXISTS idx_markets_platform ON markets(platform);
CREATE INDEX IF NOT EXISTS idx_markets_category ON markets(category);
CREATE INDEX IF NOT EXISTS idx_markets_status ON markets(status);
"""

CREATE_SCANS = """
CREATE TABLE IF NOT EXISTS scans (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at              TEXT NOT NULL,
    completed_at            TEXT,
    duration_ms             INTEGER,
    markets_fetched_poly    INTEGER DEFAULT 0,
    markets_fetched_kalshi  INTEGER DEFAULT 0,
    matches_found           INTEGER DEFAULT 0,
    tier1_count             INTEGER DEFAULT 0,
    tier2_count             INTEGER DEFAULT 0,
    tier3_count             INTEGER DEFAULT 0,
    errors_count            INTEGER DEFAULT 0
);
"""

CREATE_OPPORTUNITIES = """
CREATE TABLE IF NOT EXISTS opportunities (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id             INTEGER REFERENCES scans(id),
    market_a_id         INTEGER REFERENCES markets(id),
    market_b_id         INTEGER REFERENCES markets(id),
    match_confidence    REAL,
    gross_spread        REAL,
    net_spread_after_fees REAL,
    tier                INTEGER,
    risk_score          REAL,
    profit_50           REAL,
    profit_100          REAL,
    profit_200          REAL,
    profit_500          REAL,
    annualised_return   REAL,
    liquidity_limited   INTEGER DEFAULT 0,
    polarity_flipped    INTEGER DEFAULT 0,
    detected_at         TEXT NOT NULL,
    expires_at          TEXT,
    status              TEXT DEFAULT 'live'
);
"""

CREATE_OPPORTUNITIES_IDX = """
CREATE INDEX IF NOT EXISTS idx_opps_scan ON opportunities(scan_id);
CREATE INDEX IF NOT EXISTS idx_opps_tier ON opportunities(tier);
CREATE INDEX IF NOT EXISTS idx_opps_status ON opportunities(status);
CREATE INDEX IF NOT EXISTS idx_opps_detected ON opportunities(detected_at);
"""

CREATE_EXECUTIONS = """
CREATE TABLE IF NOT EXISTS executions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id  INTEGER REFERENCES opportunities(id),
    side_a_order    TEXT,
    side_b_order    TEXT,
    deployed_capital REAL,
    actual_profit   REAL,
    status          TEXT DEFAULT 'pending',
    executed_at     TEXT,
    settled_at      TEXT
);
"""

CREATE_OPPORTUNITY_TRACKING = """
CREATE TABLE IF NOT EXISTS opportunity_tracking (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    poly_external_id        TEXT NOT NULL,
    kalshi_external_id      TEXT NOT NULL,
    first_detected_at       TEXT NOT NULL,
    spread_at_detection     REAL NOT NULL,
    last_seen_at            TEXT,
    still_live_1h           INTEGER DEFAULT -1,
    still_live_at_close     INTEGER DEFAULT -1,
    resolved_correctly      INTEGER DEFAULT -1,
    UNIQUE(poly_external_id, kalshi_external_id)
);
"""

CREATE_OPPORTUNITY_TRACKING_IDX = """
CREATE INDEX IF NOT EXISTS idx_opp_tracking_poly ON opportunity_tracking(poly_external_id);
CREATE INDEX IF NOT EXISTS idx_opp_tracking_first ON opportunity_tracking(first_detected_at);
"""


class DatabaseManager:
    def __init__(self, db_path: str = config.DB_PATH):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA cache_size=-32000")  # 32MB cache
        await self._initialise_schema()
        logger.info(f"Database connected: {self.db_path}")

    async def disconnect(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def _initialise_schema(self) -> None:
        await self._db.execute(CREATE_MARKETS)
        for stmt in CREATE_MARKETS_IDX.strip().split("\n"):
            await self._db.execute(stmt)
        await self._db.execute(CREATE_SCANS)
        await self._db.execute(CREATE_OPPORTUNITIES)
        for stmt in CREATE_OPPORTUNITIES_IDX.strip().split("\n"):
            await self._db.execute(stmt)
        await self._db.execute(CREATE_EXECUTIONS)
        await self._db.execute(CREATE_OPPORTUNITY_TRACKING)
        for stmt in CREATE_OPPORTUNITY_TRACKING_IDX.strip().split("\n"):
            await self._db.execute(stmt)
        await self._db.commit()

    # ─── Markets ─────────────────────────────────────────────────────────────

    async def upsert_market(self, market: NormalisedMarket) -> int:
        """Insert or update a market, return its DB id."""
        now = datetime.utcnow().isoformat()
        res_date = market.resolution_date.isoformat() if market.resolution_date else None
        async with self._db.execute(
            """
            INSERT INTO markets
                (platform, external_id, title, description, category,
                 yes_price, no_price, implied_probability,
                 volume_24h, total_volume, liquidity_depth,
                 resolution_date, status, url, raw_json, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(platform, external_id) DO UPDATE SET
                title=excluded.title,
                category=excluded.category,
                yes_price=excluded.yes_price,
                no_price=excluded.no_price,
                implied_probability=excluded.implied_probability,
                volume_24h=excluded.volume_24h,
                total_volume=excluded.total_volume,
                liquidity_depth=excluded.liquidity_depth,
                resolution_date=excluded.resolution_date,
                status=excluded.status,
                raw_json=excluded.raw_json,
                updated_at=excluded.updated_at
            """,
            (
                market.platform, market.external_id, market.title,
                market.description, market.category,
                market.yes_price, market.no_price, market.implied_probability,
                market.volume_24h, market.total_volume, market.liquidity_depth,
                res_date, market.status, market.url, market.raw_json,
                now, now,
            ),
        ) as cur:
            await self._db.commit()
            if cur.lastrowid:
                return cur.lastrowid
            # Fetch id after conflict update
            async with self._db.execute(
                "SELECT id FROM markets WHERE platform=? AND external_id=?",
                (market.platform, market.external_id),
            ) as row_cur:
                row = await row_cur.fetchone()
                return row["id"] if row else 0

    async def upsert_markets_bulk(self, markets: list[NormalisedMarket]) -> None:
        """Bulk upsert for efficiency."""
        now = datetime.utcnow().isoformat()
        rows = []
        for m in markets:
            res_date = m.resolution_date.isoformat() if m.resolution_date else None
            rows.append((
                m.platform, m.external_id, m.title, m.description, m.category,
                m.yes_price, m.no_price, m.implied_probability,
                m.volume_24h, m.total_volume, m.liquidity_depth,
                res_date, m.status, m.url, m.raw_json, now, now,
            ))
        await self._db.executemany(
            """
            INSERT INTO markets
                (platform, external_id, title, description, category,
                 yes_price, no_price, implied_probability,
                 volume_24h, total_volume, liquidity_depth,
                 resolution_date, status, url, raw_json, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(platform, external_id) DO UPDATE SET
                title=excluded.title,
                category=excluded.category,
                yes_price=excluded.yes_price,
                no_price=excluded.no_price,
                implied_probability=excluded.implied_probability,
                volume_24h=excluded.volume_24h,
                total_volume=excluded.total_volume,
                liquidity_depth=excluded.liquidity_depth,
                resolution_date=excluded.resolution_date,
                status=excluded.status,
                raw_json=excluded.raw_json,
                updated_at=excluded.updated_at
            """,
            rows,
        )
        await self._db.commit()
        logger.debug(f"Upserted {len(rows)} markets")

    # ─── Scans ───────────────────────────────────────────────────────────────

    async def start_scan(self, started_at: datetime) -> int:
        async with self._db.execute(
            "INSERT INTO scans (started_at) VALUES (?)",
            (started_at.isoformat(),),
        ) as cur:
            await self._db.commit()
            return cur.lastrowid

    async def complete_scan(self, result: ScanResult) -> None:
        await self._db.execute(
            """
            UPDATE scans SET
                completed_at=?, duration_ms=?,
                markets_fetched_poly=?, markets_fetched_kalshi=?,
                matches_found=?, tier1_count=?, tier2_count=?,
                tier3_count=?, errors_count=?
            WHERE id=?
            """,
            (
                result.completed_at.isoformat(),
                result.duration_ms,
                result.markets_fetched_poly,
                result.markets_fetched_kalshi,
                result.matches_found,
                result.tier1_count,
                result.tier2_count,
                result.tier3_count,
                result.errors_count,
                result.scan_id,
            ),
        )
        await self._db.commit()

    async def get_scan_stats_today(self) -> dict:
        today = datetime.utcnow().date().isoformat()
        async with self._db.execute(
            """
            SELECT
                COUNT(*) as scan_count,
                SUM(tier1_count) as tier1_total,
                SUM(tier2_count) as tier2_total,
                SUM(errors_count) as errors_total,
                AVG(duration_ms) as avg_duration_ms
            FROM scans
            WHERE started_at >= ?
            """,
            (today,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else {}

    # ─── Opportunities ───────────────────────────────────────────────────────

    async def save_opportunity(
        self,
        opp: ArbOpportunity,
        market_a_db_id: int,
        market_b_db_id: int,
    ) -> int:
        p50 = opp.profit_scenarios.get(50)
        p100 = opp.profit_scenarios.get(100)
        p200 = opp.profit_scenarios.get(200)
        p500 = opp.profit_scenarios.get(500)
        expires = opp.expires_at.isoformat() if opp.expires_at else None

        async with self._db.execute(
            """
            INSERT INTO opportunities
                (scan_id, market_a_id, market_b_id, match_confidence,
                 gross_spread, net_spread_after_fees, tier, risk_score,
                 profit_50, profit_100, profit_200, profit_500,
                 annualised_return, liquidity_limited, polarity_flipped,
                 detected_at, expires_at, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                opp.scan_id,
                market_a_db_id,
                market_b_db_id,
                opp.match.confidence,
                opp.gross_spread,
                opp.net_spread,
                opp.tier,
                opp.risk_score,
                p50.net_profit_usd if p50 else None,
                p100.net_profit_usd if p100 else None,
                p200.net_profit_usd if p200 else None,
                p500.net_profit_usd if p500 else None,
                opp.annualised_return,
                1 if (p100 and p100.liquidity_limited) else 0,
                1 if opp.match.polarity_flipped else 0,
                opp.detected_at.isoformat(),
                expires,
                opp.status,
            ),
        ) as cur:
            await self._db.commit()
            return cur.lastrowid

    async def get_live_opportunities(self) -> list[dict]:
        async with self._db.execute(
            """
            SELECT o.*,
                   ma.title as poly_title, ma.url as poly_url,
                   ma.yes_price as poly_yes_price,
                   mb.title as kalshi_title, mb.url as kalshi_url,
                   mb.yes_price as kalshi_yes_price
            FROM opportunities o
            JOIN markets ma ON o.market_a_id = ma.id
            JOIN markets mb ON o.market_b_id = mb.id
            WHERE o.status = 'live'
            ORDER BY o.net_spread_after_fees DESC
            """
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def expire_old_opportunities(self) -> None:
        """Mark opportunities as expired if their resolution date has passed."""
        now = datetime.utcnow().isoformat()
        await self._db.execute(
            "UPDATE opportunities SET status='expired' WHERE expires_at < ? AND status='live'",
            (now,),
        )
        await self._db.commit()

    async def prune_old_markets(self, days: int = 7) -> int:
        """
        Delete market rows last updated more than `days` ago that are not
        referenced by any live opportunity. Returns the count deleted.
        Keeps the DB small so repeated scans don't accumulate stale data.
        """
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        async with self._db.execute(
            """
            DELETE FROM markets
            WHERE updated_at < ?
              AND id NOT IN (
                SELECT market_a_id FROM opportunities WHERE status = 'live'
                UNION
                SELECT market_b_id FROM opportunities WHERE status = 'live'
              )
            """,
            (cutoff,),
        ) as cur:
            deleted = cur.rowcount
        await self._db.commit()
        if deleted:
            logger.info(f"[db] Pruned {deleted} stale markets (> {days} days old)")
        return deleted

    # ─── Opportunity tracking ─────────────────────────────────────────────────

    async def upsert_opportunity_tracking(
        self,
        poly_external_id: str,
        kalshi_external_id: str,
        spread: float,
        first_detected_at: datetime,
    ) -> None:
        """Record first detection of an opportunity pair; update last_seen on revisit."""
        now = datetime.utcnow().isoformat()
        await self._db.execute(
            """
            INSERT INTO opportunity_tracking
                (poly_external_id, kalshi_external_id, first_detected_at,
                 spread_at_detection, last_seen_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(poly_external_id, kalshi_external_id) DO UPDATE SET
                last_seen_at=excluded.last_seen_at
            """,
            (
                poly_external_id,
                kalshi_external_id,
                first_detected_at.isoformat(),
                spread,
                now,
            ),
        )
        await self._db.commit()

    async def update_tracking_liveness(
        self,
        poly_external_id: str,
        kalshi_external_id: str,
        still_live_1h: Optional[int] = None,
        still_live_at_close: Optional[int] = None,
        resolved_correctly: Optional[int] = None,
    ) -> None:
        """Update liveness fields (0=no, 1=yes, -1=unknown) for a tracked pair."""
        sets = []
        vals: list = []
        if still_live_1h is not None:
            sets.append("still_live_1h=?")
            vals.append(still_live_1h)
        if still_live_at_close is not None:
            sets.append("still_live_at_close=?")
            vals.append(still_live_at_close)
        if resolved_correctly is not None:
            sets.append("resolved_correctly=?")
            vals.append(resolved_correctly)
        if not sets:
            return
        vals.extend([poly_external_id, kalshi_external_id])
        await self._db.execute(
            f"UPDATE opportunity_tracking SET {', '.join(sets)} "
            f"WHERE poly_external_id=? AND kalshi_external_id=?",
            vals,
        )
        await self._db.commit()

    async def get_tracking_stats(self) -> dict:
        """Summary stats for tracked opportunities."""
        async with self._db.execute(
            """
            SELECT
                COUNT(*) as total_tracked,
                SUM(CASE WHEN still_live_1h=1 THEN 1 ELSE 0 END) as live_after_1h,
                SUM(CASE WHEN resolved_correctly=1 THEN 1 ELSE 0 END) as resolved_correctly,
                AVG(spread_at_detection) as avg_spread_at_detection
            FROM opportunity_tracking
            """
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else {}

    async def get_best_spread_today(self) -> float:
        today = datetime.utcnow().date().isoformat()
        async with self._db.execute(
            "SELECT MAX(net_spread_after_fees) FROM opportunities WHERE detected_at >= ?",
            (today,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row and row[0] else 0.0

    async def get_opportunity_count_today(self) -> dict:
        today = datetime.utcnow().date().isoformat()
        async with self._db.execute(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN tier=1 THEN 1 ELSE 0 END) as tier1,
                SUM(CASE WHEN tier=2 THEN 1 ELSE 0 END) as tier2,
                SUM(CASE WHEN tier=3 THEN 1 ELSE 0 END) as tier3
            FROM opportunities
            WHERE detected_at >= ?
            """,
            (today,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else {"total": 0, "tier1": 0, "tier2": 0, "tier3": 0}
