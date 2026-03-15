"""
Arbitrage Calculator — turns a MarketMatch into a full ArbOpportunity.

Calculates:
  - Gross spread (before fees)
  - Net spread (after all platform fees)
  - Profit for each configured position size
  - Annualised return
  - Tier classification (1, 2, 3)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import config
from database.models import MarketMatch, ArbOpportunity, ProfitScenario

logger = logging.getLogger(__name__)


class ArbCalculator:
    """
    Calculates arbitrage metrics for confirmed market matches.
    All monetary values in USD.
    """

    def calculate(self, match: MarketMatch) -> Optional[ArbOpportunity]:
        """
        Main entry point. Returns None if no real opportunity exists
        (net spread <= 0 or fails hard blocks).
        """
        pm = match.market_poly
        km = match.market_kalshi

        # ── Determine buy-side prices ────────────────────────────────────────
        # Strategy: BUY YES on the cheaper side, BUY NO on the other side.
        # If polarity flipped, we buy YES on both sides.

        if not match.polarity_flipped:
            # Normal: buy YES on Poly, buy NO on Kalshi (or vice versa)
            # We check both combinations and take the better one.
            spread_a = self._gross_spread_normal(pm.yes_price, km.no_price)
            spread_b = self._gross_spread_normal(km.yes_price, pm.no_price)

            if spread_a >= spread_b:
                gross_spread = spread_a
                buy_yes_on = "polymarket"
                buy_no_on = "kalshi"
                yes_price_used = pm.yes_price
                no_price_used = km.no_price
            else:
                gross_spread = spread_b
                buy_yes_on = "kalshi"
                buy_no_on = "polymarket"
                yes_price_used = km.yes_price
                no_price_used = pm.no_price
        else:
            # Polarity flipped: e.g. "Democrats win" vs "Republicans win".
            # gross = yes_A + yes_B - 1.0. If positive, markets are OVER-sum,
            # so the trade is buy NO on both (cost = no_A + no_B = 2 - yes_A - yes_B < 1).
            gross_spread = self._gross_spread_flipped(pm.yes_price, km.yes_price)
            buy_yes_on = "n/a"
            buy_no_on = "both"
            yes_price_used = pm.yes_price
            no_price_used = km.yes_price

        # ── Net spread after fees ────────────────────────────────────────────
        total_fees = config.POLYMARKET_FEE + config.KALSHI_FEE
        net_spread = gross_spread - total_fees

        # Hard block: no real arb
        if net_spread <= 0:
            return None

        # ── Days to resolution ───────────────────────────────────────────────
        now = datetime.now(tz=timezone.utc)
        resolution_date = pm.resolution_date or km.resolution_date

        if resolution_date:
            if resolution_date.tzinfo is None:
                resolution_date = resolution_date.replace(tzinfo=timezone.utc)
            days_to_resolution = max(
                1.0,
                (resolution_date - now).total_seconds() / 86400.0,
            )
            expires_at = resolution_date
        else:
            days_to_resolution = 30.0  # Conservative assumption
            expires_at = None

        # ── Annualised return ────────────────────────────────────────────────
        annualised_return = (net_spread / days_to_resolution) * 365.0 * 100.0

        # ── Profit scenarios ─────────────────────────────────────────────────
        profit_scenarios: dict[int, ProfitScenario] = {}
        for size in config.POSITION_SIZES:
            scenario = self._calculate_scenario(
                position_usd=size,
                net_spread=net_spread,
                liquidity_poly=pm.liquidity_depth,
                liquidity_kalshi=km.liquidity_depth,
            )
            profit_scenarios[size] = scenario

        # ── Tier classification ──────────────────────────────────────────────
        tier = self._classify_tier(
            net_spread=net_spread,
            confidence=match.confidence,
            vol_poly=pm.total_volume,
            vol_kalshi=km.total_volume,
        )

        opp = ArbOpportunity(
            match=match,
            gross_spread=gross_spread,
            net_spread=net_spread,
            tier=tier,
            risk_score=0.0,  # Filled by risk engine
            profit_scenarios=profit_scenarios,
            annualised_return=annualised_return,
            days_to_resolution=days_to_resolution,
            expires_at=expires_at,
        )

        logger.debug(
            f"[calc] Match: {pm.title[:40]}… ↔ {km.title[:40]}… "
            f"| Gross: {gross_spread:.3f} | Net: {net_spread:.3f} | Tier {tier}"
        )
        return opp

    # ─── Spread calculations ─────────────────────────────────────────────────

    @staticmethod
    def _gross_spread_normal(yes_price: float, no_price: float) -> float:
        """
        Normal arb: buy YES on the cheaper platform, NO on the more expensive one.

        Arguments:
            yes_price — YES price from platform A (what you PAY to go long YES on A)
            no_price  — NO price from platform B  (what you PAY to go long NO on B)
                       = 1 - yes_price_B

        Total cost of the hedge = yes_price + no_price.
        Payout at resolution = $1.00 in either case (YES or NO).

        Profit = $1.00 - cost = 1.0 - (yes_price + no_price)
               = 1 - yes_A - (1 - yes_B) = yes_B - yes_A

        Positive profit when yes_B > yes_A: YES is MORE expensive on B →
        buy YES on A (cheap) and NO on B (cheap relative to A's NO price).
        """
        return 1.0 - (yes_price + no_price)

    @staticmethod
    def _gross_spread_flipped(yes_price_a: float, yes_price_b: float) -> float:
        """
        Polarity-flipped arb: one market is "Will X happen", other is "Will X NOT happen".
        Both YES prices together should sum to 1.0 in efficient market.
        Spread = (yes_a + yes_b) - 1.0
        """
        return (yes_price_a + yes_price_b) - 1.0

    # ─── Profit scenario ─────────────────────────────────────────────────────

    @staticmethod
    def _calculate_scenario(
        position_usd: float,
        net_spread: float,
        liquidity_poly: float,
        liquidity_kalshi: float,
    ) -> ProfitScenario:
        """
        Calculate expected profit for a given position size.

        Capital split: allocate proportionally across both legs.
        Each leg gets approximately half the capital.
        """
        min_liquidity = min(liquidity_poly, liquidity_kalshi)
        liquidity_limited = position_usd > min_liquidity * 0.5

        # Gross profit = position * net spread
        gross_profit = position_usd * net_spread

        # Gas/execution fees (one round trip on Polygon per leg)
        gas = config.GAS_FEE_ESTIMATE * 2  # Both legs

        net_profit = gross_profit - gas
        roi = (net_profit / position_usd) * 100.0 if position_usd > 0 else 0.0

        return ProfitScenario(
            position_usd=position_usd,
            gross_profit_usd=gross_profit,
            net_profit_usd=net_profit,
            roi_percent=roi,
            liquidity_limited=liquidity_limited,
        )

    # ─── Tier classification ─────────────────────────────────────────────────

    @staticmethod
    def _classify_tier(
        net_spread: float,
        confidence: float,
        vol_poly: float,
        vol_kalshi: float,
    ) -> int:
        """
        Classify the opportunity into a tier (1 = best, 3 = informational).
        """
        min_vol = min(vol_poly, vol_kalshi)

        if (
            net_spread >= config.TIER1_NET_SPREAD
            and confidence >= config.TIER1_CONFIDENCE
            and min_vol >= config.TIER1_VOLUME
        ):
            return 1

        if (
            net_spread >= config.TIER2_NET_SPREAD
            and confidence >= config.TIER2_CONFIDENCE
            and min_vol >= config.TIER2_VOLUME
        ):
            return 2

        return 3

    def calculate_all(self, matches: list[MarketMatch]) -> list[ArbOpportunity]:
        """
        Process a list of matches and return all valid opportunities.
        """
        opportunities = []
        for match in matches:
            try:
                opp = self.calculate(match)
                if opp:
                    opportunities.append(opp)
            except Exception as e:
                logger.error(
                    f"[calc] Error calculating opportunity for "
                    f"{match.market_poly.external_id} ↔ {match.market_kalshi.external_id}: {e}"
                )
        logger.info(
            f"[calc] {len(matches)} matches → {len(opportunities)} valid opportunities"
        )
        return opportunities
