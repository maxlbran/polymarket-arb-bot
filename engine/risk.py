"""
Risk Scoring Engine — assigns a risk score (1–10) to each opportunity.

Higher = safer. Hard blocks prevent flagging of dangerous opportunities.
Never trade an opportunity the risk engine has not approved.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import config
from database.models import ArbOpportunity

logger = logging.getLogger(__name__)


class RiskEngine:
    """
    Scores opportunities across four risk dimensions and enforces hard blocks.

    Scoring breakdown (max 10 points):
      Liquidity risk    0–3 pts  (can the position be filled?)
      Execution risk    0–2 pts  (time to resolution)
      Match quality     0–3 pts  (confidence in the match)
      Platform risk     0–2 pts  (API health status)
    """

    def __init__(
        self,
        poly_healthy: bool = True,
        kalshi_healthy: bool = True,
    ) -> None:
        self.poly_healthy = poly_healthy
        self.kalshi_healthy = kalshi_healthy

    def score(self, opp: ArbOpportunity) -> ArbOpportunity:
        """
        Score a single opportunity. Modifies opp.risk_score in place.
        Returns the opportunity (or raises if it fails a hard block — caller handles).
        """
        if self._fails_hard_block(opp):
            opp.risk_score = 0.0
            return opp

        liquidity_score = self._liquidity_score(opp)
        execution_score = self._execution_score(opp)
        match_quality_score = self._match_quality_score(opp)
        platform_score = self._platform_score()

        total = liquidity_score + execution_score + match_quality_score + platform_score
        opp.risk_score = round(min(10.0, max(1.0, total)), 2)

        logger.debug(
            f"[risk] Score {opp.risk_score:.1f}/10 — "
            f"liq:{liquidity_score:.1f} exec:{execution_score:.1f} "
            f"match:{match_quality_score:.1f} platform:{platform_score:.1f}"
        )
        return opp

    def score_all(self, opportunities: list[ArbOpportunity]) -> list[ArbOpportunity]:
        """Score all opportunities. Filter out those with risk_score below minimum."""
        scored = []
        for opp in opportunities:
            try:
                self.score(opp)
                scored.append(opp)
            except Exception as e:
                logger.error(f"[risk] Error scoring opportunity: {e}")
        return scored

    def filter_safe(self, opportunities: list[ArbOpportunity]) -> list[ArbOpportunity]:
        """Return only opportunities that pass the minimum risk threshold."""
        safe = [
            o for o in opportunities
            if o.risk_score >= config.MIN_RISK_SCORE
        ]
        filtered = len(opportunities) - len(safe)
        if filtered:
            logger.info(f"[risk] Filtered {filtered} opportunities below risk threshold")
        return safe

    # ─── Hard blocks ─────────────────────────────────────────────────────────

    def _fails_hard_block(self, opp: ArbOpportunity) -> bool:
        """
        Hard blocks — these are absolute disqualifiers regardless of any score.
        If ANY condition is true, the opportunity must not be flagged.
        """
        pm = opp.match.market_poly
        km = opp.match.market_kalshi

        # Net spread must be positive after ALL fees
        if opp.net_spread <= 0:
            logger.debug("[risk] HARD BLOCK: net_spread <= 0")
            return True

        # Sanity cap: genuine prediction market arb can't exceed ~15%.
        # Larger gross spreads indicate false matches (different outcomes of
        # the same event series, e.g., FOMC "hold" matched with "25bps hike").
        if opp.gross_spread > 0.15:
            logger.debug(f"[risk] HARD BLOCK: gross_spread {opp.gross_spread:.2f} > 15% (false match)")
            return True

        # Resolution date must not have passed
        now = datetime.now(tz=timezone.utc)
        if opp.expires_at:
            exp = opp.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp <= now:
                logger.debug("[risk] HARD BLOCK: resolution date passed")
                return True

        # Minimum volume on both sides ($5,000 floor)
        if pm.total_volume < config.MIN_VOLUME_USD:
            logger.debug(f"[risk] HARD BLOCK: Poly volume {pm.total_volume:.0f} < minimum")
            return True
        if km.total_volume < config.MIN_VOLUME_USD:
            logger.debug(f"[risk] HARD BLOCK: Kalshi volume {km.total_volume:.0f} < minimum")
            return True

        # Bid/ask spread > 5% indicates illiquid or stale market
        if pm.bid_ask_spread > 0.05:
            logger.debug(f"[risk] HARD BLOCK: Poly bid/ask spread {pm.bid_ask_spread:.3f} > 5%")
            return True
        if km.bid_ask_spread > 0.05:
            logger.debug(f"[risk] HARD BLOCK: Kalshi bid/ask spread {km.bid_ask_spread:.3f} > 5%")
            return True

        # Match confidence floor
        if opp.match.confidence < config.MIN_MATCH_CONFIDENCE:
            logger.debug(f"[risk] HARD BLOCK: confidence {opp.match.confidence:.1f} < {config.MIN_MATCH_CONFIDENCE}")
            return True

        # Must be able to place minimum position
        min_scenario = opp.profit_scenarios.get(config.MIN_POSITION_USD)
        if min_scenario and min_scenario.liquidity_limited:
            # Check if liquidity is truly insufficient (not just tight)
            min_liq = min(pm.liquidity_depth, km.liquidity_depth)
            if min_liq < config.MIN_POSITION_USD:
                logger.debug(f"[risk] HARD BLOCK: insufficient liquidity {min_liq:.0f}")
                return True

        # Both platforms must be up
        if not self.poly_healthy or not self.kalshi_healthy:
            logger.debug("[risk] HARD BLOCK: platform health check failed")
            return True

        return False

    # ─── Score components ─────────────────────────────────────────────────────

    def _liquidity_score(self, opp: ArbOpportunity) -> float:
        """
        0–3 points based on liquidity depth relative to standard position sizes.
        Uses a $200 reference position.
        """
        pm = opp.match.market_poly
        km = opp.match.market_kalshi
        min_liq = min(pm.liquidity_depth, km.liquidity_depth)
        ref_position = 200.0

        ratio = min_liq / ref_position if ref_position > 0 else 0.0

        if ratio >= 5.0:
            return 3.0
        elif ratio >= 3.0:
            return 2.0
        elif ratio >= 1.0:
            return 1.0
        else:
            return 0.0  # Will be blocked by hard block anyway

    def _execution_score(self, opp: ArbOpportunity) -> float:
        """
        0–2 points based on time remaining until resolution.
        Very short-dated markets are too risky to execute.
        """
        hours = opp.days_to_resolution * 24.0

        if hours < 6:
            return 0.0   # Too close — execution risk too high
        elif hours < 24:
            return 1.0   # Tight but possible
        elif hours <= 168:  # 7 days
            return 2.0   # Comfortable window
        else:
            return 2.0   # Long-dated is fine

    def _match_quality_score(self, opp: ArbOpportunity) -> float:
        """
        0–3 points based on the confidence score from the matching engine.
        """
        confidence = opp.match.confidence

        if confidence >= 85:
            return 3.0
        elif confidence >= 70:
            return 1.0
        else:
            return 0.0

    def _platform_score(self) -> float:
        """
        0–2 points based on API health of both platforms.
        """
        if self.poly_healthy and self.kalshi_healthy:
            return 2.0
        elif self.poly_healthy or self.kalshi_healthy:
            return 1.0
        else:
            return 0.0
