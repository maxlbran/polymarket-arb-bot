"""
Alert System — fires on TIER 1 opportunities and logs all opportunities.

On TIER 1:
  - Prints ASCII art banner to terminal
  - Sounds bell 3 times
  - Logs full details to arb.log with exact trade instructions
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime

import config
from database.models import ArbOpportunity

logger = logging.getLogger(__name__)

# Separate file logger for opportunities
arb_logger = logging.getLogger("arb_opportunities")


def _setup_arb_logger() -> None:
    if not arb_logger.handlers:
        fh = logging.FileHandler(config.ARB_LOG)
        fh.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        fh.setFormatter(fmt)
        arb_logger.addHandler(fh)
        arb_logger.setLevel(logging.INFO)
        arb_logger.propagate = False


_setup_arb_logger()


TIER1_BANNER = """
╔══════════════════════════════════════════════════════════════════╗
║                                                                  ║
║   ██████╗ ██████╗ ██████╗     ███████╗ ██████╗ ██╗   ██╗███╗   ║
║   ██╔══██╗██╔══██╗██╔══██╗    ██╔════╝██╔═══██╗██║   ██║████╗  ║
║   ███████║██████╔╝██████╔╝    █████╗  ██║   ██║██║   ██║██╔██╗ ║
║   ██╔══██║██╔══██╗██╔══██╗    ██╔══╝  ██║   ██║██║   ██║██║╚██╗║
║   ██║  ██║██║  ██║██████╔╝    ██║     ╚██████╔╝╚██████╔╝██║ ╚█╗║
║   ╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝     ╚═╝      ╚═════╝  ╚═════╝ ╚═╝  ╚╝║
║                                                                  ║
║              *** TIER 1 ARBITRAGE OPPORTUNITY ***               ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
"""


class Notifier:
    """Handles all alerting for arbitrage opportunities."""

    def __init__(self) -> None:
        self._alerted_today: set[str] = set()  # Tracks seen opportunities by key
        self._tier1_count_today: int = 0
        self._last_tier1: list[tuple[datetime, ArbOpportunity]] = []

    def _opportunity_key(self, opp: ArbOpportunity) -> str:
        """Unique key for deduplication."""
        return (
            f"{opp.match.market_poly.external_id}:"
            f"{opp.match.market_kalshi.external_id}"
        )

    def notify(self, opp: ArbOpportunity, is_new: bool = True) -> None:
        """
        Process an opportunity. Fires alerts for high-value opportunities.

        Alert criteria (union):
          - TIER 1 (any days)
          - net_spread > 2% AND days_to_resolution < 30
        """
        key = self._opportunity_key(opp)

        # Log to arb file regardless of tier
        self._log_to_file(opp)

        is_alertable = opp.tier == 1 or (
            opp.net_spread > 0.02 and opp.days_to_resolution < 30
        )

        if is_alertable and is_new and key not in self._alerted_today:
            self._alerted_today.add(key)
            if opp.tier == 1:
                self._tier1_count_today += 1
                self._last_tier1.append((datetime.utcnow(), opp))
                if len(self._last_tier1) > 10:
                    self._last_tier1 = self._last_tier1[-10:]
            self._fire_tier1_alert(opp)

    def notify_all(self, opportunities: list[ArbOpportunity], is_new_scan: bool = True) -> None:
        """Process a batch of opportunities from a scan."""
        for opp in opportunities:
            key = self._opportunity_key(opp)
            is_new = key not in self._alerted_today or is_new_scan
            self.notify(opp, is_new=is_new)

    def reset_daily(self) -> None:
        """Call at start of new UTC day."""
        self._alerted_today.clear()
        self._tier1_count_today = 0
        self._last_tier1.clear()

    @property
    def tier1_count_today(self) -> int:
        return self._tier1_count_today

    @property
    def recent_tier1(self) -> list[tuple[datetime, ArbOpportunity]]:
        return self._last_tier1[-5:]

    # ─── Alert methods ────────────────────────────────────────────────────────

    @staticmethod
    def _kelly_position(net_spread: float, bankroll: float = 1000.0) -> float:
        """Quarter-Kelly optimal position size, capped at $500."""
        if net_spread <= 0 or net_spread >= 1:
            return 0.0
        return min((net_spread / (1.0 - net_spread)) * 0.25 * bankroll, 500.0)

    def _fire_tier1_alert(self, opp: ArbOpportunity) -> None:
        """Alert banner with trade instructions and Kelly sizing."""
        print(TIER1_BANNER, flush=True)

        pm = opp.match.market_poly
        km = opp.match.market_kalshi
        p100 = opp.profit_scenarios.get(100)
        p500 = opp.profit_scenarios.get(500)
        kelly = self._kelly_position(opp.net_spread)
        tier_label = f"TIER {opp.tier}" if opp.tier > 1 else "TIER 1 ★"

        print(f"  {tier_label} | Detected at: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
        net = float(opp.net_spread or 0)
        gross = float(opp.gross_spread or 0)
        risk = float(opp.risk_score or 0)
        days = float(opp.days_to_resolution or 0)
        ann = float(opp.annualised_return or 0)
        print(f"  Net Spread:   {net*100:.2f}%  "
              f"(Gross: {gross*100:.2f}% | Fees: {(gross - net)*100:.2f}%)")
        print(f"  Risk Score:   {risk:.1f}/10  |  Confidence: {float(opp.match.confidence or 0):.1f}%")
        print(f"  Expires in:   {days:.1f} days  "
              f"| Annualised: {ann:.0f}%/yr")
        print(f"  Kelly Size:   ${kelly:.0f} (quarter-Kelly, $1k bankroll)")
        print()
        print("  ─── TRADE INSTRUCTIONS ──────────────────────────────────────────")
        print()

        if not opp.match.polarity_flipped:
            # Determine which side to buy YES on
            if pm.yes_price <= km.yes_price:
                yes_platform, yes_market, yes_price = "Polymarket", pm, pm.yes_price
                no_platform, no_market, no_price = "Kalshi", km, km.no_price
            else:
                yes_platform, yes_market, yes_price = "Kalshi", km, km.yes_price
                no_platform, no_market, no_price = "Polymarket", pm, pm.no_price

            print(f"  LEG A — BUY YES on {yes_platform}")
            print(f"    Market:  {yes_market.title}")
            print(f"    Price:   {yes_price:.3f}  ({yes_price*100:.1f}¢)")
            print(f"    URL:     {yes_market.url}")
            print()
            print(f"  LEG B — BUY NO on {no_platform}")
            print(f"    Market:  {no_market.title}")
            print(f"    Price:   {no_price:.3f}  ({no_price*100:.1f}¢)")
            print(f"    URL:     {no_market.url}")
        else:
            print(f"  POLARITY-FLIPPED trade — BUY NO on both platforms")
            print()
            print(f"  LEG A — BUY NO on Polymarket")
            print(f"    Market:  {pm.title}")
            print(f"    Price:   {pm.no_price:.3f}  ({pm.no_price*100:.1f}¢)")
            print(f"    URL:     {pm.url}")
            print()
            print(f"  LEG B — BUY NO on Kalshi")
            print(f"    Market:  {km.title}")
            print(f"    Price:   {km.no_price:.3f}  ({km.no_price*100:.1f}¢)")
            print(f"    URL:     {km.url}")

        print()
        print("  ─── EXPECTED RETURNS ────────────────────────────────────────────")
        for size_label, scenario in [("$100", p100), (f"${kelly:.0f} (Kelly)", p500)]:
            if scenario:
                liq = " ⚠ liq-limited" if scenario.liquidity_limited else ""
                print(
                    f"  Deploy {size_label:<14} → "
                    f"Net: +${scenario.net_profit_usd:.2f} | ROI: {scenario.roi_percent:.1f}%{liq}"
                )
        if p500:
            print(
                f"  Deploy $500            → "
                f"Net: +${p500.net_profit_usd:.2f} | ROI: {p500.roi_percent:.1f}%"
            )
        print()
        print("  " + "═" * 68)
        print()

        # Sound the bell
        if config.ALERT_SOUND:
            for _ in range(config.ALERT_BELL_REPEATS):
                print("\a", end="", flush=True)

    def _log_to_file(self, opp: ArbOpportunity) -> None:
        """Write opportunity details to arb.log."""
        pm = opp.match.market_poly
        km = opp.match.market_kalshi
        p100 = opp.profit_scenarios.get(100)

        net = float(opp.net_spread or 0)
        risk = float(opp.risk_score or 0)
        days = float(opp.days_to_resolution or 0)
        conf = float(opp.match.confidence or 0)
        profit_100 = float(p100.net_profit_usd if p100 else 0)
        arb_logger.info(
            f"TIER{opp.tier} | spread={net*100:.2f}% | "
            f"risk={risk:.1f} | conf={conf:.0f} | "
            f"profit_100=${profit_100:.2f} | "
            f"poly=[{pm.title[:60]}] @ yes={float(pm.yes_price or 0):.3f} | "
            f"kalshi=[{km.title[:60]}] @ yes={float(km.yes_price or 0):.3f} | "
            f"expires_in={days:.1f}d | "
            f"poly_url={pm.url} | kalshi_url={km.url}"
        )
