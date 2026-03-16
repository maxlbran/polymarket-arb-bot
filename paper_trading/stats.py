"""
Paper Trading Stats Dashboard — rendered with Rich.
Called via: python3 main.py --paper-stats
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box
from rich.text import Text

if TYPE_CHECKING:
    from database.db import DatabaseManager

logger = logging.getLogger(__name__)
console = Console()


async def show_paper_stats(db: "DatabaseManager") -> None:
    """Main entry point — fetches data and renders the full dashboard."""
    stats = await db.get_paper_trade_stats()
    all_trades = await db.get_paper_trades_all()
    trades_30d = await db.get_paper_trades_30d()

    total = stats.get("total", 0)
    if total == 0:
        console.print(
            Panel(
                "[yellow]No paper trades recorded yet.\n"
                "The bot logs opportunities automatically — run a scan first.[/]",
                title="Paper Trading Track Record",
                border_style="yellow",
            )
        )
        return

    _print_summary(stats)
    _print_best_worst(stats)
    _print_open_positions(all_trades)
    _print_rolling_chart(trades_30d)
    _print_monthly_table(all_trades)


# ─── Panels ───────────────────────────────────────────────────────────────────

def _print_summary(stats: dict) -> None:
    won     = stats.get("won", 0) or 0
    lost    = stats.get("lost", 0) or 0
    expired = stats.get("expired", 0) or 0
    open_c  = stats.get("open_count", 0) or 0
    total   = stats.get("total", 0) or 0
    decided = won + lost

    win_rate_str = f"{(won / decided * 100):.1f}%" if decided else "—"
    avg_spread   = (stats.get("avg_entry_spread") or 0) * 100
    total_100    = stats.get("total_pnl_100") or 0
    total_1000   = stats.get("total_pnl_1000") or 0
    avg_100      = stats.get("avg_pnl_100") or 0

    colour_100   = "green" if total_100 >= 0 else "red"
    colour_1000  = "green" if total_1000 >= 0 else "red"

    lines = [
        f"  Total trades: [bold]{total}[/]   "
        f"Open: [cyan]{open_c}[/]   "
        f"Won: [green]{won}[/]   "
        f"Lost: [red]{lost}[/]   "
        f"Expired: [dim]{expired}[/]",
        "",
        f"  Win rate: [bold]{win_rate_str}[/]   "
        f"Avg entry spread: [cyan]{avg_spread:.2f}%[/]   "
        f"Avg P&L per resolved @$100: [{colour_100}]${avg_100:+.2f}[/]",
        "",
        f"  Total P&L @[bold]$100[/]/trade:  [{colour_100}]{total_100:+.2f}[/]",
        f"  Total P&L @[bold]$1,000[/]/trade: [{colour_1000}]{total_1000:+.2f}[/]",
    ]

    console.print(Panel(
        "\n".join(lines),
        title="[bold cyan]Paper Trading Track Record[/]",
        border_style="cyan",
        padding=(1, 2),
    ))


def _print_best_worst(stats: dict) -> None:
    for label, colour, trade in [
        ("Best trade", "green", stats.get("best")),
        ("Worst trade", "red",  stats.get("worst")),
    ]:
        if not trade:
            continue
        spread_pct = (trade.get("entry_net_spread") or 0) * 100
        pnl = trade.get("pnl_100") or 0
        sign = "+" if pnl >= 0 else ""
        console.print(
            f"  [{colour}]{label}:[/]  {trade['poly_title'][:60]}\n"
            f"           Entry spread: {spread_pct:.2f}%  |  "
            f"P&L @$100: [{colour}]${sign}{pnl:.2f}[/]  |  "
            f"[dim]{trade['outcome']}[/]\n"
        )


def _print_open_positions(all_trades: list[dict]) -> None:
    open_trades = [t for t in all_trades if t["outcome"] == "OPEN"]
    if not open_trades:
        console.print("  [dim]No open positions.[/]\n")
        return

    table = Table(
        title="Open Positions",
        box=box.SIMPLE,
        show_header=True,
        header_style="bold",
        padding=(0, 1),
    )
    table.add_column("#",          width=4,  justify="right")
    table.add_column("Poly market",          max_width=35)
    table.add_column("Kalshi market",        max_width=35)
    table.add_column("Entry\nSpread", width=8,  justify="right")
    table.add_column("First\nSeen",   width=11, justify="right")
    table.add_column("Expires",        width=11, justify="right")
    table.add_column("Proj.\n@$100",   width=8,  justify="right")

    now = datetime.now(tz=timezone.utc)
    for i, t in enumerate(open_trades, 1):
        spread_pct = (t.get("entry_net_spread") or 0) * 100
        proj_100   = (t.get("entry_net_spread") or 0) * 100
        first_seen = (t.get("first_seen_at") or "")[:10]
        expires    = (t.get("resolution_date") or "unknown")[:10]

        table.add_row(
            str(i),
            t["poly_title"][:35],
            t["kalshi_title"][:35],
            f"{spread_pct:.2f}%",
            first_seen,
            expires,
            f"[dim]${proj_100:.2f}[/]",
        )

    console.print(table)
    console.print()


def _print_rolling_chart(trades_30d: list[dict]) -> None:
    """ASCII bar chart: daily P&L at $100/trade for the last 30 days."""
    # Group by date
    daily: dict[str, float] = defaultdict(float)
    for t in trades_30d:
        resolved = (t.get("resolved_at") or "")[:10]
        if resolved:
            daily[resolved] += (t.get("pnl_100") or 0)

    if not daily:
        console.print("  [dim]No resolved trades in the last 30 days.[/]\n")
        return

    # Build 30-day date range
    today = datetime.now(tz=timezone.utc).date()
    dates = [(today - timedelta(days=29 - i)).isoformat() for i in range(30)]

    max_abs = max((abs(daily.get(d, 0)) for d in dates), default=1) or 1
    bar_width = 20

    lines: list[Text] = []
    for d in dates:
        val = daily.get(d, 0)
        filled = int(abs(val) / max_abs * bar_width)
        bar_char = "█" if val >= 0 else "░"
        colour   = "green" if val >= 0 else "red"
        sign     = "+" if val >= 0 else ""
        bar_str  = bar_char * filled
        label    = d[5:]  # MM-DD
        lines.append(
            Text.assemble(
                (f"  {label}  ", "dim"),
                (f"{bar_str:<{bar_width}}", colour),
                (f"  {sign}${val:.2f}", colour if val != 0 else "dim"),
            )
        )

    console.print(Panel(
        Text("\n").join(lines),
        title="[bold]30-Day Rolling P&L  (@$100/trade)[/]",
        border_style="dim",
        padding=(1, 1),
    ))
    console.print()


def _print_monthly_table(all_trades: list[dict]) -> None:
    resolved = [t for t in all_trades if t["outcome"] in ("WON", "LOST")]
    if not resolved:
        return

    monthly: dict[str, dict] = defaultdict(lambda: {"won": 0, "lost": 0, "pnl_100": 0.0, "pnl_1000": 0.0})
    for t in resolved:
        key = (t.get("resolved_at") or t.get("first_seen_at") or "")[:7]  # YYYY-MM
        if not key:
            continue
        if t["outcome"] == "WON":
            monthly[key]["won"] += 1
        else:
            monthly[key]["lost"] += 1
        monthly[key]["pnl_100"]  += t.get("pnl_100") or 0
        monthly[key]["pnl_1000"] += t.get("pnl_1000") or 0

    if not monthly:
        return

    table = Table(
        title="Monthly Performance",
        box=box.SIMPLE,
        show_header=True,
        header_style="bold",
        padding=(0, 1),
    )
    table.add_column("Month",      width=8)
    table.add_column("Trades",     width=7,  justify="right")
    table.add_column("Won",        width=5,  justify="right")
    table.add_column("Lost",       width=5,  justify="right")
    table.add_column("Win %",      width=7,  justify="right")
    table.add_column("P&L @$100",  width=11, justify="right")
    table.add_column("P&L @$1k",   width=11, justify="right")

    totals = {"trades": 0, "won": 0, "lost": 0, "pnl_100": 0.0, "pnl_1000": 0.0}
    for month in sorted(monthly):
        m = monthly[month]
        trades = m["won"] + m["lost"]
        win_pct = f"{m['won']/trades*100:.0f}%" if trades else "—"
        c100  = "green" if m["pnl_100"]  >= 0 else "red"
        c1000 = "green" if m["pnl_1000"] >= 0 else "red"
        table.add_row(
            month,
            str(trades),
            f"[green]{m['won']}[/]",
            f"[red]{m['lost']}[/]",
            win_pct,
            f"[{c100}]${m['pnl_100']:+.2f}[/]",
            f"[{c1000}]${m['pnl_1000']:+.2f}[/]",
        )
        totals["trades"]  += trades
        totals["won"]     += m["won"]
        totals["lost"]    += m["lost"]
        totals["pnl_100"] += m["pnl_100"]
        totals["pnl_1000"] += m["pnl_1000"]

    total_trades = totals["trades"]
    total_win_pct = f"{totals['won']/total_trades*100:.0f}%" if total_trades else "—"
    c100  = "green" if totals["pnl_100"]  >= 0 else "red"
    c1000 = "green" if totals["pnl_1000"] >= 0 else "red"
    table.add_section()
    table.add_row(
        "[bold]TOTAL[/]",
        f"[bold]{total_trades}[/]",
        f"[bold green]{totals['won']}[/]",
        f"[bold red]{totals['lost']}[/]",
        f"[bold]{total_win_pct}[/]",
        f"[bold {c100}]${totals['pnl_100']:+.2f}[/]",
        f"[bold {c1000}]${totals['pnl_1000']:+.2f}[/]",
    )

    console.print(table)
    console.print()
