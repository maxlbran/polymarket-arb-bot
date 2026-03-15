"""
Terminal Dashboard — Rich-powered live display.

Panels:
  1. Header — system info, API status, timing
  2. Live Opportunities table — sorted by net spread
  3. Stats panel — daily summary numbers
  4. Recent TIER 1 alerts
  5. System health — API calls, cache, errors
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich import box
from rich.text import Text
from rich.columns import Columns
from rich.align import Align

import config
from database.models import ArbOpportunity, ScanResult
from alerts.notifier import Notifier

logger = logging.getLogger(__name__)

console = Console()


def _spread_color(spread: float) -> str:
    if spread >= config.TIER1_NET_SPREAD:
        return "bold bright_green"
    elif spread >= config.TIER2_NET_SPREAD:
        return "green"
    elif spread > 0:
        return "yellow"
    return "red"


def _tier_color(tier: int) -> str:
    return {1: "bold bright_green", 2: "green", 3: "yellow"}.get(tier, "white")


def _risk_color(score: float) -> str:
    if score >= 8:
        return "bold bright_green"
    elif score >= 6:
        return "green"
    elif score >= 4:
        return "yellow"
    return "red"


def _api_indicator(healthy: bool) -> Text:
    if healthy:
        return Text("● LIVE", style="bold bright_green")
    return Text("● DOWN", style="bold red")


class Dashboard:
    """
    Manages the Rich live dashboard.
    Call update() each scan cycle.
    """

    def __init__(self, notifier: Notifier) -> None:
        self.notifier = notifier
        self._live: Optional[Live] = None
        self._opportunities: list[ArbOpportunity] = []
        self._last_scan: Optional[ScanResult] = None
        self._scan_count_today: int = 0
        self._poly_healthy: bool = True
        self._kalshi_healthy: bool = True
        self._next_scan_at: Optional[datetime] = None
        self._start_time: datetime = datetime.now(tz=timezone.utc)
        self._total_api_calls: int = 0
        self._total_cache_hits: int = 0
        self._total_errors: int = 0
        self._avg_scan_ms: float = 0.0
        self._best_spread_today: float = 0.0
        self._opp_count_today: dict = {"total": 0, "tier1": 0, "tier2": 0, "tier3": 0}

    def start(self) -> "Dashboard":
        """Start the live display. Use as context manager or call start/stop."""
        self._live = Live(
            self._build_layout(),
            console=console,
            refresh_per_second=0.1,   # Low refresh; we drive updates manually
            screen=False,
        )
        self._live.start()
        return self

    def stop(self) -> None:
        if self._live:
            self._live.stop()

    def __enter__(self) -> "Dashboard":
        return self.start()

    def __exit__(self, *args) -> None:
        self.stop()

    def update(
        self,
        opportunities: list[ArbOpportunity],
        scan_result: Optional[ScanResult] = None,
        poly_healthy: bool = True,
        kalshi_healthy: bool = True,
        next_scan_at: Optional[datetime] = None,
        api_calls: int = 0,
        cache_hits: int = 0,
        errors: int = 0,
        best_spread_today: float = 0.0,
        opp_count_today: Optional[dict] = None,
    ) -> None:
        """Push updated data to the dashboard."""
        self._opportunities = sorted(
            opportunities,
            key=lambda o: o.net_spread,
            reverse=True,
        )[:config.DASHBOARD_MAX_ROWS]

        self._last_scan = scan_result
        self._poly_healthy = poly_healthy
        self._kalshi_healthy = kalshi_healthy
        self._next_scan_at = next_scan_at
        self._total_api_calls = api_calls
        self._total_cache_hits = cache_hits
        self._total_errors = errors
        self._best_spread_today = best_spread_today
        self._opp_count_today = opp_count_today or self._opp_count_today

        if scan_result:
            self._scan_count_today += 1
            self._avg_scan_ms = (
                (self._avg_scan_ms * (self._scan_count_today - 1) + scan_result.duration_ms)
                / self._scan_count_today
            )

        if self._live:
            self._live.update(self._build_layout())

    # ─── Layout builders ──────────────────────────────────────────────────────

    def _build_layout(self) -> Layout:
        layout = Layout()

        layout.split_column(
            Layout(self._build_header(), name="header", size=7),
            Layout(self._build_opportunities_table(), name="opportunities"),
            Layout(name="bottom", size=12),
        )

        layout["bottom"].split_row(
            Layout(self._build_stats(), name="stats"),
            Layout(self._build_recent_alerts(), name="alerts"),
            Layout(self._build_health(), name="health"),
        )

        return layout

    def _build_header(self) -> Panel:
        now = datetime.now(tz=timezone.utc)
        uptime = now - self._start_time
        uptime_str = str(uptime).split(".")[0]  # Remove microseconds

        if self._next_scan_at:
            remaining = (self._next_scan_at - now).total_seconds()
            remaining = max(0, remaining)
            next_str = f"Next scan in {int(remaining // 60):02d}:{int(remaining % 60):02d}"
        else:
            next_str = "Scanning now…"

        last_scan_str = "Never"
        if self._last_scan:
            last_scan_str = self._last_scan.completed_at.strftime("%H:%M:%S UTC")

        grid = Table.grid(expand=True)
        grid.add_column(ratio=2)
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)

        grid.add_row(
            Text(f"  {config.SYSTEM_NAME} v{config.VERSION}", style="bold bright_white"),
            Text(f"Polymarket: ", style="white") + _api_indicator(self._poly_healthy),
            Text(f"Kalshi: ", style="white") + _api_indicator(self._kalshi_healthy),
            Text(f"Runtime: {uptime_str}", style="dim"),
        )
        grid.add_row(
            Text(f"  {now.strftime('%Y-%m-%d %H:%M:%S UTC')}", style="dim"),
            Text(f"Last scan: {last_scan_str}", style="dim"),
            Text(next_str, style="cyan"),
            Text(f"Scans today: {self._scan_count_today}", style="dim"),
        )

        return Panel(grid, style="on grey11", border_style="bright_blue")

    def _build_opportunities_table(self) -> Panel:
        table = Table(
            box=box.SIMPLE_HEAD,
            show_header=True,
            header_style="bold bright_white",
            expand=True,
            show_lines=False,
            padding=(0, 1),
        )

        table.add_column("#", style="dim", width=3, no_wrap=True)
        table.add_column("Tier", width=6, no_wrap=True)
        table.add_column("Conf%", width=6, no_wrap=True)
        table.add_column("Polymarket Market", ratio=3, no_wrap=True, overflow="ellipsis")
        table.add_column("Kalshi Market", ratio=3, no_wrap=True, overflow="ellipsis")
        table.add_column("Poly YES", width=9, no_wrap=True)
        table.add_column("Kalshi", width=9, no_wrap=True)
        table.add_column("Net Spread", width=10, no_wrap=True)
        table.add_column("$100 Profit", width=11, no_wrap=True)
        table.add_column("$200 Profit", width=11, no_wrap=True)
        table.add_column("Risk", width=5, no_wrap=True)
        table.add_column("Expires", width=10, no_wrap=True)

        if not self._opportunities:
            table.add_row(
                *["—"] * 12,
            )
        else:
            for rank, opp in enumerate(self._opportunities, 1):
                pm = opp.match.market_poly
                km = opp.match.market_kalshi
                p100 = opp.profit_scenarios.get(100)
                p200 = opp.profit_scenarios.get(200)
                tc = _tier_color(opp.tier)
                sc = _spread_color(opp.net_spread)
                rc = _risk_color(opp.risk_score)

                expires_str = f"{opp.days_to_resolution:.1f}d"

                table.add_row(
                    str(rank),
                    Text(opp.tier_label, style=tc),
                    f"{opp.match.confidence:.0f}",
                    pm.title[:55],
                    km.title[:55],
                    f"{pm.yes_price:.3f}",
                    f"{km.yes_price:.3f}" if not opp.match.polarity_flipped else f"{km.no_price:.3f}",
                    Text(f"{opp.net_spread*100:.2f}%", style=sc),
                    Text(
                        f"${p100.net_profit_usd:.2f}" if p100 else "—",
                        style="bright_green" if p100 and p100.net_profit_usd > 0 else "red",
                    ),
                    Text(
                        f"${p200.net_profit_usd:.2f}" if p200 else "—",
                        style="bright_green" if p200 and p200.net_profit_usd > 0 else "red",
                    ),
                    Text(f"{opp.risk_score:.1f}", style=rc),
                    expires_str,
                )

        title = f"Live Opportunities ({len(self._opportunities)} shown)"
        return Panel(table, title=title, border_style="blue")

    def _build_stats(self) -> Panel:
        cnt = self._opp_count_today
        avg_spread = (
            sum(o.net_spread for o in self._opportunities) / len(self._opportunities)
            if self._opportunities else 0.0
        )
        total_pot = sum(
            o.profit_scenarios[100].net_profit_usd
            for o in self._opportunities
            if 100 in o.profit_scenarios
        )

        grid = Table.grid(padding=(0, 1))
        grid.add_column(style="dim", min_width=22)
        grid.add_column(style="bold bright_white")

        grid.add_row("Scans today:", str(self._scan_count_today))
        grid.add_row("Opps today:", str(cnt.get("total", 0)))
        grid.add_row("  TIER 1:", Text(str(cnt.get("tier1", 0)), style="bright_green"))
        grid.add_row("  TIER 2:", Text(str(cnt.get("tier2", 0)), style="green"))
        grid.add_row("  TIER 3:", str(cnt.get("tier3", 0)))
        grid.add_row("Best spread today:", Text(f"{self._best_spread_today*100:.2f}%", style="bright_green"))
        grid.add_row("Avg live spread:", f"{avg_spread*100:.2f}%")
        grid.add_row("Total potential ($100):", Text(f"${total_pot:.2f}", style="bright_white"))

        return Panel(grid, title="Daily Stats", border_style="blue")

    def _build_recent_alerts(self) -> Panel:
        grid = Table.grid(padding=(0, 1))
        grid.add_column(style="dim", min_width=10)
        grid.add_column(overflow="fold")

        recent = self.notifier.recent_tier1
        if not recent:
            grid.add_row("—", "No TIER 1 alerts yet")
        else:
            for ts, opp in reversed(recent):
                grid.add_row(
                    ts.strftime("%H:%M:%S"),
                    Text(
                        f"{opp.match.market_poly.title[:40]} | {opp.net_spread*100:.2f}%",
                        style="bright_green",
                    ),
                )

        grid.add_row("", "")
        grid.add_row(
            "Total T1 today:",
            Text(str(self.notifier.tier1_count_today), style="bold bright_green"),
        )

        return Panel(grid, title="Recent TIER 1 Alerts", border_style="blue")

    def _build_health(self) -> Panel:
        total = self._total_api_calls + self._total_cache_hits
        hit_rate = (self._total_cache_hits / total * 100) if total > 0 else 0.0

        grid = Table.grid(padding=(0, 1))
        grid.add_column(style="dim", min_width=18)
        grid.add_column(style="bold bright_white")

        grid.add_row("API calls:", str(self._total_api_calls))
        grid.add_row("Cache hits:", str(self._total_cache_hits))
        grid.add_row("Cache hit rate:", f"{hit_rate:.1f}%")
        grid.add_row("Avg scan time:", f"{self._avg_scan_ms:.0f}ms")
        grid.add_row("Errors:", Text(str(self._total_errors), style="red" if self._total_errors > 0 else "green"))

        return Panel(grid, title="System Health", border_style="blue")

    def print_startup_banner(self) -> None:
        """Print a one-time startup message before the live display begins."""
        console.print()
        console.print(
            f"  [bold bright_blue]{config.SYSTEM_NAME}[/] v{config.VERSION}",
        )
        console.print(
            f"  Polymarket: [cyan]{config.POLYMARKET_BASE_URL}[/]",
        )
        console.print(
            f"  Kalshi:     [cyan]{config.KALSHI_BASE_URL}[/]",
        )
        console.print(
            f"  Scan interval: [white]{config.SCAN_INTERVAL_MINUTES}m[/] | "
            f"Min spread: [white]{config.MIN_NET_SPREAD*100:.1f}%[/] | "
            f"Min confidence: [white]{config.MIN_MATCH_CONFIDENCE}[/]",
        )
        console.print()
