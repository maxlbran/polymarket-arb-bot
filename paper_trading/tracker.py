"""
Paper Trade Tracker — automatically logs every safe opportunity as a paper trade
and resolves them when markets close.

Resolution logic:
  WON    — both platforms agree on the outcome (genuine arb captured the spread)
  LOST   — platforms disagree (basis risk — one leg pays nothing)
  EXPIRED — resolution date passed but insufficient price data to determine outcome

P&L model:
  WON:     position_usd × entry_net_spread
  LOST:    -position_usd × entry_gross_spread  (both legs expire worthless)
  EXPIRED: 0.0 (conservative — unknown)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from database.db import DatabaseManager
    from database.models import ArbOpportunity

logger = logging.getLogger(__name__)

# Price thresholds for considering a market "resolved"
_RESOLVED_HIGH = 0.92   # above this → market resolved YES
_RESOLVED_LOW  = 0.08   # below this → market resolved NO


class PaperTradeTracker:
    """
    Sits alongside the scanner and silently records every opportunity.
    No user interaction needed — fully automatic.
    """

    def __init__(self, db: "DatabaseManager") -> None:
        self.db = db

    async def log_opportunity(self, opp: "ArbOpportunity") -> None:
        """
        Insert this opportunity as a paper trade (INSERT OR IGNORE).
        Only the first observation of each pair is recorded as the entry price.
        """
        try:
            inserted = await self.db.log_paper_trade(opp)
            if inserted:
                pm = opp.match.market_poly
                km = opp.match.market_kalshi
                logger.info(
                    f"[paper] New trade #{opp.tier} logged: "
                    f"{pm.title[:40]} ↔ {km.title[:40]} "
                    f"| net={float(opp.net_spread or 0)*100:.2f}%"
                )
        except Exception as e:
            logger.debug(f"[paper] log_opportunity failed: {e}")

    async def check_resolutions(self, current_opps: "list[ArbOpportunity]") -> int:
        """
        Called each scan. Checks all OPEN paper trades for resolution.
        Returns count of trades newly resolved.
        """
        open_trades = await self.db.get_open_paper_trades()
        if not open_trades:
            return 0

        # Build lookup: (poly_id, kalshi_id) → current live prices
        live_prices: dict[tuple[str, str], tuple[float, float]] = {
            (
                o.match.market_poly.external_id,
                o.match.market_kalshi.external_id,
            ): (
                float(o.match.market_poly.yes_price or 0),
                float(o.match.market_kalshi.yes_price or 0),
            )
            for o in current_opps
        }

        resolved_count = 0
        now_utc = datetime.now(tz=timezone.utc)

        for row in open_trades:
            pair_key = (row["poly_external_id"], row["kalshi_external_id"])
            live = live_prices.get(pair_key)

            outcome: str | None = None
            exit_poly = row.get("exit_poly_yes_price") or 0.0
            exit_kalshi = row.get("exit_kalshi_yes_price") or 0.0

            if live:
                poly_yes, kalshi_yes = live
                exit_poly, exit_kalshi = poly_yes, kalshi_yes

                poly_res_yes = poly_yes > _RESOLVED_HIGH
                poly_res_no  = poly_yes < _RESOLVED_LOW
                kalshi_res_yes = kalshi_yes > _RESOLVED_HIGH
                kalshi_res_no  = kalshi_yes < _RESOLVED_LOW

                poly_resolved   = poly_res_yes or poly_res_no
                kalshi_resolved = kalshi_res_yes or kalshi_res_no

                if poly_resolved and kalshi_resolved:
                    outcome = self._determine_outcome(
                        polarity_flipped=bool(row["polarity_flipped"]),
                        entry_poly_yes=float(row["entry_poly_yes_price"]),
                        entry_kalshi_yes=float(row["entry_kalshi_yes_price"]),
                        poly_final_yes=poly_yes,
                        kalshi_final_yes=kalshi_yes,
                    )

            # Expiry fallback: resolution date passed and we still have no outcome
            if outcome is None and row["resolution_date"]:
                try:
                    res_dt = datetime.fromisoformat(row["resolution_date"])
                    if res_dt.tzinfo is None:
                        res_dt = res_dt.replace(tzinfo=timezone.utc)
                    if res_dt < now_utc:
                        outcome = "EXPIRED"
                except (ValueError, TypeError):
                    pass

            if outcome:
                pnl_100, pnl_1000 = self._compute_pnl(
                    outcome=outcome,
                    net_spread=float(row["entry_net_spread"]),
                    gross_spread=float(row["entry_gross_spread"]),
                )
                await self.db.resolve_paper_trade(
                    poly_external_id=row["poly_external_id"],
                    kalshi_external_id=row["kalshi_external_id"],
                    outcome=outcome,
                    exit_poly_yes_price=exit_poly,
                    exit_kalshi_yes_price=exit_kalshi,
                    pnl_100=pnl_100,
                    pnl_1000=pnl_1000,
                )
                resolved_count += 1
                logger.info(
                    f"[paper] Trade resolved → {outcome}: "
                    f"{row['poly_title'][:40]} "
                    f"| P&L @$100: ${pnl_100:+.2f}"
                )

        return resolved_count

    # ─── Resolution helpers ───────────────────────────────────────────────────

    @staticmethod
    def _determine_outcome(
        polarity_flipped: bool,
        entry_poly_yes: float,
        entry_kalshi_yes: float,
        poly_final_yes: float,
        kalshi_final_yes: float,
    ) -> str:
        """
        Determine WON or LOST based on final prices.

        For non-flipped arb: we hold YES on the cheaper platform, NO on the other.
          - LOST only when the cheap-YES platform resolves NO while the other resolves YES.
            That means our YES leg pays $0 AND our NO leg pays $0 (total wipeout).
          - WON in all other clear resolution scenarios.

        For polarity-flipped arb: we hold NO on both platforms (different framings).
          - LOST only when both platforms resolve YES (both NOs pay $0).
          - WON in all other clear resolution scenarios.
        """
        poly_yes_won  = poly_final_yes > _RESOLVED_HIGH
        poly_no_won   = poly_final_yes < _RESOLVED_LOW
        kalshi_yes_won = kalshi_final_yes > _RESOLVED_HIGH
        kalshi_no_won  = kalshi_final_yes < _RESOLVED_LOW

        if polarity_flipped:
            # Holding NO on both; only lose if BOTH platforms resolve YES
            if poly_yes_won and kalshi_yes_won:
                return "LOST"
            return "WON"

        # Determine which platform we bought YES on (cheaper YES at entry)
        bought_yes_on_poly = entry_poly_yes <= entry_kalshi_yes

        if bought_yes_on_poly:
            # Holding YES Poly / NO Kalshi
            # LOST: Poly resolves NO AND Kalshi resolves YES
            #   → our YES Poly pays $0, our NO Kalshi pays $0
            if poly_no_won and kalshi_yes_won:
                return "LOST"
        else:
            # Holding YES Kalshi / NO Poly
            # LOST: Kalshi resolves NO AND Poly resolves YES
            if kalshi_no_won and poly_yes_won:
                return "LOST"

        return "WON"

    @staticmethod
    def _compute_pnl(outcome: str, net_spread: float, gross_spread: float) -> tuple[float, float]:
        """
        Returns (pnl_100, pnl_1000) for a given outcome.
        WON:     position × net_spread  (locked spread, fees already deducted)
        LOST:    -position × gross_spread (both legs worthless, lose entry cost)
        EXPIRED: 0.0 (unknown — conservative)
        """
        if outcome == "WON":
            return net_spread * 100, net_spread * 1000
        if outcome == "LOST":
            return -gross_spread * 100, -gross_spread * 1000
        return 0.0, 0.0
