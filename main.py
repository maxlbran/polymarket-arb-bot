#!/usr/bin/env python3
from __future__ import annotations
"""
PolyKalshi Arb Engine — Entry Point
=====================================
Starts the live terminal dashboard and runs the scanning loop indefinitely.

Usage:
    python main.py [--once] [--no-dashboard] [--debug]

    --once          Run a single scan and exit (useful for testing)
    --no-dashboard  Run without the Rich dashboard (plain logging only)
    --debug         Set log level to DEBUG
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import config
from database.db import DatabaseManager
from alerts.notifier import Notifier
from display.dashboard import Dashboard
from engine.scanner import Scanner

# ─── Logging setup ────────────────────────────────────────────────────────────

def setup_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else getattr(logging, config.LOG_LEVEL, logging.INFO)
    is_daemon = not sys.stdout.isatty()  # running under launchd / systemd

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console/stdout handler:
    #   - Interactive (terminal): WARNING+ only (dashboard owns the screen)
    #   - Daemon (no TTY): INFO+ so scan.log has full detail
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level if is_daemon else (logging.DEBUG if debug else logging.WARNING))
    console_handler.setFormatter(fmt)

    # Dedicated scan log when running as a daemon (launchd / systemd)
    Path(config.ERROR_LOG).parent.mkdir(parents=True, exist_ok=True)
    if is_daemon:
        scan_log = Path(config.ERROR_LOG).parent / "scan.log"
        scan_handler = logging.FileHandler(scan_log)
        scan_handler.setLevel(level)
        scan_handler.setFormatter(fmt)

    # Error log — always, full detail
    file_handler = logging.FileHandler(config.ERROR_LOG)
    file_handler.setLevel(logging.WARNING)
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    if not is_daemon:
        root.addHandler(console_handler)
    root.addHandler(file_handler)
    if is_daemon:
        root.addHandler(scan_handler)

    # Quieten noisy libraries
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


# ─── Single-scan mode (for testing) ─────────────────────────────────────────

async def run_once(db: DatabaseManager, debug: bool = False) -> None:
    """
    Run exactly one scan cycle and print results to stdout.
    Does not start the dashboard. Useful for testing and CI.
    """
    from exchanges.polymarket import PolymarketConnector
    from exchanges.kalshi import KalshiConnector
    from engine.matcher import MatchingEngine
    from engine.calculator import ArbCalculator
    from engine.risk import RiskEngine
    from rich.console import Console
    from rich.table import Table
    from rich import box

    console = Console()
    console.print(f"\n[bold cyan]{config.SYSTEM_NAME} v{config.VERSION}[/] — Single Scan Mode\n")

    poly = PolymarketConnector()
    kalshi = KalshiConnector()

    await poly.open()
    await kalshi.open()

    try:
        # Health checks
        poly_ok, kalshi_ok = await asyncio.gather(
            poly.health_check(), kalshi.health_check()
        )
        console.print(
            f"  Polymarket API: {'[green]OK[/]' if poly_ok else '[red]FAIL[/]'} | "
            f"Kalshi API: {'[green]OK[/]' if kalshi_ok else '[red]FAIL[/]'}"
        )

        # Fetch
        console.print("  Fetching markets…")
        poly_markets, kalshi_markets = await asyncio.gather(
            poly.fetch_markets(), kalshi.fetch_markets()
        )
        console.print(
            f"  Fetched [cyan]{len(poly_markets)}[/] Polymarket markets, "
            f"[cyan]{len(kalshi_markets)}[/] Kalshi markets"
        )

        if not poly_markets and not kalshi_markets:
            console.print("[red]  No markets fetched from either platform. Check connectivity.[/]")
            return

        if not kalshi_markets:
            console.print(
                "[yellow]  Kalshi: No markets fetched — authentication required.[/]\n"
                "  → Get your API key at [cyan]https://kalshi.com → Settings → API[/]\n"
                "  → Add [white]KALSHI_API_KEY=your_key[/] to your [white].env[/] file\n"
            )

        if not poly_markets:
            console.print("[red]  Polymarket returned no markets.[/]")
            return

        # Persist whatever we have
        console.print("  Persisting markets to database…")
        await db.upsert_markets_bulk(poly_markets)
        if kalshi_markets:
            await db.upsert_markets_bulk(kalshi_markets)

        if not kalshi_markets:
            console.print(
                "[dim]  Showing Polymarket-only scan. "
                "Arbitrage detection requires both platforms.[/]\n"
            )
            # Show top Polymarket markets by volume as a sanity check
            console.print(f"  [bold]Top 10 Polymarket markets by volume:[/]")
            top = sorted(poly_markets, key=lambda m: m.total_volume, reverse=True)[:10]
            for m in top:
                console.print(
                    f"    [{m.category:12s}] {m.title[:65]:65s} | "
                    f"YES={m.yes_price:.2f}  vol=${m.total_volume:,.0f}"
                )
            console.print(
                f"\n  [green]✓ System verified: Polymarket connector working correctly.[/]\n"
                f"  [green]✓ {len(poly_markets):,} active markets loaded and normalised.[/]\n"
                f"  [yellow]→ Add Kalshi credentials to .env to enable arbitrage scanning.[/]\n"
            )
            return

        # Match
        console.print("  Running matching engine…")
        matcher = MatchingEngine()
        matches = matcher.find_matches(poly_markets, kalshi_markets)
        console.print(f"  Matches found: [cyan]{len(matches)}[/]")

        if not matches:
            console.print("[yellow]  No matches found. Check category mappings and fuzzy thresholds.[/]")
            # Print sample titles for debugging
            console.print("\n  [dim]Sample Polymarket titles:[/]")
            for m in poly_markets[:5]:
                console.print(f"    [{m.category}] {m.title[:80]}")
            console.print("\n  [dim]Sample Kalshi titles:[/]")
            for m in kalshi_markets[:5]:
                console.print(f"    [{m.category}] {m.title[:80]}")
            return

        # Calculate
        calculator = ArbCalculator()
        opportunities = calculator.calculate_all(matches)
        console.print(f"  Opportunities calculated: [cyan]{len(opportunities)}[/]")

        # Risk score
        risk = RiskEngine(poly_healthy=poly_ok, kalshi_healthy=kalshi_ok)
        scored = risk.score_all(opportunities)
        safe = risk.filter_safe(scored)

        # Exclude markets resolving beyond MAX_DAYS_TO_RESOLUTION
        safe = [o for o in safe if o.days_to_resolution <= config.MAX_DAYS_TO_RESOLUTION]
        console.print(
            f"  Safe opportunities (risk score ≥ {config.MIN_RISK_SCORE}, "
            f"≤ {config.MAX_DAYS_TO_RESOLUTION}d to resolution): [cyan]{len(safe)}[/]\n"
        )

        if not safe:
            # Show all opportunities regardless of risk for visibility
            all_opps = sorted(opportunities, key=lambda o: o.net_spread, reverse=True)
            console.print("[yellow]  No opportunities passed risk filter. Showing all positive spreads:[/]\n")
            _print_opportunities_table(console, all_opps[:20])
        else:
            _print_opportunities_table(console, safe)
            # Full detail for T1/T2 opportunities
            notable = [o for o in safe if o.tier <= 2]
            if notable:
                console.print("\n[bold]Full details — Tier 1/2 opportunities:[/]")
                for o in notable:
                    pm = o.match.market_poly
                    km = o.match.market_kalshi
                    console.print(
                        f"\n  [cyan]Tier {o.tier}[/] | Conf {o.match.confidence:.0f} | "
                        f"Net {o.net_spread:.3f} | Risk {o.risk_score:.1f} | Days {o.days_to_resolution:.0f}\n"
                        f"  POLY   YES={pm.yes_price:.3f}  {pm.title}\n"
                        f"         {pm.url}\n"
                        f"  KALSHI YES={km.yes_price:.3f}  {km.title}\n"
                        f"         {km.url}\n"
                        f"  Action: {_action_label(o)}"
                    )

        # Part 9: Pipeline stats block
        _print_pipeline_stats(
            console,
            matcher_stats=matcher.stats,
            n_poly=len(poly_markets),
            n_kalshi=len(kalshi_markets),
            n_opps=len(opportunities),
            n_safe=len(safe),
        )

        console.print(
            f"  API calls: Poly={poly.api_call_count} Kalshi={kalshi.api_call_count} | "
            f"Cache: Poly={poly.cache_hit_count} Kalshi={kalshi.cache_hit_count}\n"
        )

    finally:
        await poly.close()
        await kalshi.close()


def _action_label(opp) -> str:
    """Generate a compact trade action string for the opportunity."""
    if opp.match.polarity_flipped:
        return "BUY NO Poly + NO Kalshi"
    pm = opp.match.market_poly
    km = opp.match.market_kalshi
    # Determine which side is cheaper YES
    if pm.yes_price <= km.yes_price:
        return "BUY YES Poly / NO Kalshi"
    return "BUY YES Kalshi / NO Poly"


def _kelly_position(net_spread: float, bankroll: float = 1000.0) -> float:
    """Quarter-Kelly position size, capped at $500."""
    if net_spread <= 0 or net_spread >= 1:
        return 0.0
    kelly_f = (net_spread / (1.0 - net_spread)) * 0.25
    return min(kelly_f * bankroll, 500.0)


def _print_opportunities_table(console, opportunities: list) -> None:
    """
    Print a rich table of opportunities with full trade details.
    Columns: Tier | Conf% | Polymarket | Kalshi | Poly YES | Kalshi YES |
             Gross | Fees | Net | Days | $50 | $100 | $500 | Risk | Kelly$ | Action
    """
    from rich.table import Table
    from rich import box

    if not opportunities:
        console.print("[yellow]  No opportunities to display.[/]")
        return

    table = Table(box=box.ROUNDED, show_header=True, header_style="bold", expand=False)
    table.add_column("Tier",       width=5)
    table.add_column("Conf",       width=5)
    table.add_column("Polymarket", max_width=38)
    table.add_column("Kalshi",     max_width=38)
    table.add_column("Poly\nYES",  width=6)
    table.add_column("Kalshi\nYES", width=7)
    table.add_column("Gross",      width=6)
    table.add_column("Fees",       width=5)
    table.add_column("Net",        width=6)
    table.add_column("Days",       width=5)
    table.add_column("$50",        width=6)
    table.add_column("$100",       width=7)
    table.add_column("$500",       width=7)
    table.add_column("Risk",       width=4)
    table.add_column("Kelly$",     width=7)
    table.add_column("Action",     min_width=22)

    total_fees_pct = (config.POLYMARKET_FEE + config.KALSHI_FEE) * 100

    for opp in sorted(opportunities, key=lambda o: (-o.tier * -1, o.net_spread), reverse=True):
        pm  = opp.match.market_poly
        km  = opp.match.market_kalshi
        p50  = opp.profit_scenarios.get(50)
        p100 = opp.profit_scenarios.get(100)
        p500 = opp.profit_scenarios.get(500)

        tier_style   = {1: "bold bright_green", 2: "green", 3: "yellow"}.get(opp.tier, "white")
        spread_style = ("bright_green" if opp.net_spread >= 0.04
                        else "green" if opp.net_spread >= 0.02
                        else "yellow")

        kelly = _kelly_position(opp.net_spread)

        def fmt_profit(scenario) -> str:
            if not scenario:
                return "—"
            liq = "*" if scenario.liquidity_limited else ""
            sign = "+" if scenario.net_profit_usd >= 0 else ""
            return f"{sign}${scenario.net_profit_usd:.1f}{liq}"

        table.add_row(
            f"[{tier_style}]T{opp.tier}[/]",
            f"{opp.match.confidence:.0f}",
            pm.title[:36],
            km.title[:36],
            f"{pm.yes_price*100:.1f}¢",
            f"{km.yes_price*100:.1f}¢",
            f"[{spread_style}]{opp.gross_spread*100:.2f}%[/]",
            f"{total_fees_pct:.0f}%",
            f"[{spread_style}]{opp.net_spread*100:.2f}%[/]",
            f"{opp.days_to_resolution:.1f}d",
            fmt_profit(p50),
            fmt_profit(p100),
            fmt_profit(p500),
            f"{opp.risk_score:.1f}",
            f"${kelly:.0f}",
            _action_label(opp),
        )

    console.print(table)
    console.print(
        "  [dim]* = liquidity-limited position | "
        "Kelly$ = quarter-Kelly optimal position ($1k bankroll)[/]"
    )


def _print_pipeline_stats(console, matcher_stats: dict, n_poly: int, n_kalshi: int,
                           n_opps: int, n_safe: int) -> None:
    """Print a pipeline funnel showing how markets are filtered at each stage."""
    console.print("\n  [bold]Pipeline Funnel:[/]")
    console.print(f"    Poly markets fetched:   {n_poly:>8,}")
    console.print(f"    Kalshi markets fetched: {n_kalshi:>8,}")
    console.print(f"    Pairs considered:       {matcher_stats.get('pairs_considered', 0):>8,}")
    console.print(f"    L1 eliminated (cat):    {matcher_stats.get('layer1_eliminated', 0):>8,}")
    console.print(f"    L2 eliminated (entity): {matcher_stats.get('layer2_eliminated', 0):>8,}")
    console.print(f"    L3 eliminated (fuzzy):  {matcher_stats.get('layer3_eliminated', 0):>8,}")
    console.print(f"    L4 eliminated (sem):    {matcher_stats.get('layer4_eliminated', 0):>8,}")
    console.print(f"    L5 eliminated (conf):   {matcher_stats.get('layer5_eliminated', 0):>8,}")
    console.print(f"    Matches found:          {matcher_stats.get('matches_found', 0):>8,}")
    console.print(f"    Valid opportunities:    {n_opps:>8,}")
    console.print(f"    Passed risk filter:     {n_safe:>8,}")
    console.print()


# ─── Main ────────────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(description=config.SYSTEM_NAME)
    parser.add_argument("--once",               action="store_true", help="Run single scan and exit")
    parser.add_argument("--no-dashboard",       action="store_true", help="Disable Rich dashboard")
    parser.add_argument("--debug",              action="store_true", help="Enable debug logging")
    parser.add_argument("--paper-stats",        action="store_true", help="Show paper trading dashboard")
    parser.add_argument("--export-track-record",action="store_true", help="Export track record to CSV/PDF")
    parser.add_argument("--export-dir",         default=".",         help="Output dir for --export-track-record")
    args = parser.parse_args()

    setup_logging(debug=args.debug)
    logger = logging.getLogger(__name__)

    # Ensure DB directory exists
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)

    db = DatabaseManager()
    await db.connect()

    try:
        if args.paper_stats:
            from paper_trading.stats import show_paper_stats
            await show_paper_stats(db)
            return

        if args.export_track_record:
            from paper_trading.export import export_track_record
            await export_track_record(db, output_dir=args.export_dir)
            return

        if args.once:
            await run_once(db, debug=args.debug)
            return

        notifier = Notifier()
        dashboard = Dashboard(notifier=notifier)

        dashboard.print_startup_banner()

        scanner = Scanner(db=db, dashboard=dashboard, notifier=notifier)

        with dashboard:
            await scanner.start()

    except KeyboardInterrupt:
        logger.info("Shutdown requested via KeyboardInterrupt")
        print("\n\n  Shutting down PolyKalshi Arb Engine…\n")
    finally:
        await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
