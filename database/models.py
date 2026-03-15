"""
Data models — pure dataclasses, no ORM overhead.
These are the canonical data structures passed between all modules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class NormalisedMarket:
    """
    Universal market representation, independent of source platform.
    Both Polymarket and Kalshi are normalised into this structure.
    """
    platform: str                       # "polymarket" | "kalshi"
    external_id: str                    # Platform's native market ID
    title: str                          # Human-readable question
    description: str                    # Full description / rules
    category: str                       # Standardised category
    yes_price: float                    # Probability of YES (0.0–1.0)
    no_price: float                     # Probability of NO (0.0–1.0)
    implied_probability: float          # Best estimate of YES probability
    volume_24h: float                   # USD volume last 24h
    total_volume: float                 # All-time USD volume
    liquidity_depth: float              # Estimated liquidity depth USD
    resolution_date: Optional[datetime]
    status: str                         # "active" | "closed" | "resolved"
    url: str                            # Direct link to the market
    raw_json: str                       # Original API response (JSON string)
    bid_ask_spread: float = 0.0         # YES ask minus YES bid (0 = unknown)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    db_id: Optional[int] = None


@dataclass
class OrderBook:
    """Snapshot of order book depth for a single market side."""
    market_id: str
    platform: str
    yes_bids: list[tuple[float, float]] = field(default_factory=list)  # (price, size)
    yes_asks: list[tuple[float, float]] = field(default_factory=list)
    no_bids: list[tuple[float, float]] = field(default_factory=list)
    no_asks: list[tuple[float, float]] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def best_yes_ask(self) -> Optional[float]:
        if self.yes_asks:
            return min(p for p, _ in self.yes_asks)
        return None

    def best_no_ask(self) -> Optional[float]:
        if self.no_asks:
            return min(p for p, _ in self.no_asks)
        return None

    def yes_liquidity(self, max_price: float = 1.0) -> float:
        """Total USD available to buy YES at or below max_price."""
        return sum(size for price, size in self.yes_asks if price <= max_price)

    def no_liquidity(self, max_price: float = 1.0) -> float:
        """Total USD available to buy NO at or below max_price."""
        return sum(size for price, size in self.no_asks if price <= max_price)


@dataclass
class MarketMatch:
    """
    A confirmed match between a Polymarket market and a Kalshi market.
    Represents the same real-world event on both platforms.
    """
    market_poly: NormalisedMarket
    market_kalshi: NormalisedMarket
    confidence: float                   # 0–100
    confidence_tier: str               # "HIGH" | "MEDIUM"
    polarity_flipped: bool             # True if one market is "NOT X" framing
    string_score: float
    category_score: float
    date_score: float
    volume_score: float
    entity_score: float
    matched_entities: list[str] = field(default_factory=list)


@dataclass
class ProfitScenario:
    """Profit calculation for a single position size."""
    position_usd: float
    gross_profit_usd: float
    net_profit_usd: float
    roi_percent: float
    liquidity_limited: bool            # True if market can't absorb full position


@dataclass
class ArbOpportunity:
    """
    A fully analysed arbitrage opportunity, ready for display and alerting.
    """
    match: MarketMatch
    gross_spread: float                # Raw spread before fees
    net_spread: float                  # After all fees
    tier: int                          # 1, 2, or 3
    risk_score: float                  # 1–10 (10 = safest)
    profit_scenarios: dict[int, ProfitScenario]  # position_size -> scenario
    annualised_return: float           # Projected annualised % return
    days_to_resolution: float
    detected_at: datetime = field(default_factory=datetime.utcnow)
    first_seen_at: Optional[datetime] = None  # When this pair was first observed
    expires_at: Optional[datetime] = None
    status: str = "live"               # "live" | "expired" | "executed"
    db_id: Optional[int] = None
    scan_id: Optional[int] = None

    @property
    def description(self) -> str:
        """Short human-readable description of the opportunity."""
        poly_title = self.match.market_poly.title[:50]
        return f"{poly_title}…"

    @property
    def best_profit_100(self) -> float:
        s = self.profit_scenarios.get(100)
        return s.net_profit_usd if s else 0.0

    @property
    def best_profit_50(self) -> float:
        s = self.profit_scenarios.get(50)
        return s.net_profit_usd if s else 0.0

    @property
    def tier_label(self) -> str:
        labels = {1: "TIER1", 2: "TIER2", 3: "TIER3"}
        return labels.get(self.tier, "TIER?")


@dataclass
class ScanResult:
    """Summary of a single scan cycle."""
    scan_id: Optional[int]
    started_at: datetime
    completed_at: datetime
    duration_ms: int
    markets_fetched_poly: int
    markets_fetched_kalshi: int
    matches_found: int
    tier1_count: int
    tier2_count: int
    tier3_count: int
    errors_count: int
    opportunities: list[ArbOpportunity] = field(default_factory=list)
