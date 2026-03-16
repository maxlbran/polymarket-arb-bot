"""
Track Record Export — CSV (always) + PDF (if fpdf2 installed).
Called via: python3 main.py --export-track-record [--export-dir DIR]

Also prints performance metrics (Sharpe ratio, max drawdown) to stdout.
"""
from __future__ import annotations

import csv
import logging
import os
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console

if TYPE_CHECKING:
    from database.db import DatabaseManager

logger = logging.getLogger(__name__)
console = Console()


async def export_track_record(db: "DatabaseManager", output_dir: str = ".") -> None:
    """Main entry point."""
    all_trades = await db.get_paper_trades_all()
    stats      = await db.get_paper_trade_stats()

    if not all_trades:
        console.print("[yellow]No paper trades to export yet. Run a scan first.[/]")
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    csv_path = out / f"track_record_{timestamp}.csv"
    _write_csv(all_trades, csv_path)
    console.print(f"[green]CSV exported:[/] {csv_path}")

    _try_write_pdf(all_trades, stats, out, timestamp)
    _print_performance_metrics(all_trades, stats)


# ─── CSV ──────────────────────────────────────────────────────────────────────

def _write_csv(trades: list[dict], path: Path) -> None:
    if not trades:
        return

    fieldnames = [
        "id", "first_seen_at", "resolved_at", "outcome",
        "poly_title", "poly_url",
        "kalshi_title", "kalshi_url",
        "category", "polarity_flipped",
        "entry_poly_yes_price", "entry_kalshi_yes_price",
        "entry_gross_spread", "entry_net_spread",
        "entry_tier", "entry_risk_score", "entry_annualised_return",
        "entry_days_to_resolution", "match_confidence",
        "exit_poly_yes_price", "exit_kalshi_yes_price",
        "resolution_date", "pnl_100", "pnl_1000",
    ]

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
            quoting=csv.QUOTE_NONNUMERIC,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(trades)


# ─── PDF ──────────────────────────────────────────────────────────────────────

def _try_write_pdf(
    trades: list[dict],
    stats: dict,
    out: Path,
    timestamp: str,
) -> None:
    try:
        from fpdf import FPDF  # type: ignore
    except ImportError:
        console.print(
            "[dim]PDF skipped — install fpdf2 for PDF export:[/] pip3 install fpdf2"
        )
        return

    pdf_path = out / f"track_record_{timestamp}.pdf"
    _write_pdf(trades, stats, pdf_path, FPDF)
    console.print(f"[green]PDF exported:[/] {pdf_path}")


def _write_pdf(trades: list[dict], stats: dict, path: Path, FPDF) -> None:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "PolyKalshi Arb Engine — Paper Trade Record", ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}", ln=True)
    pdf.ln(4)

    # Summary block
    won  = stats.get("won", 0) or 0
    lost = stats.get("lost", 0) or 0
    total = stats.get("total", 0) or 0
    decided = won + lost
    win_rate = f"{won/decided*100:.1f}%" if decided else "n/a"
    pnl_100  = stats.get("total_pnl_100") or 0
    pnl_1000 = stats.get("total_pnl_1000") or 0

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, "Summary", ln=True)
    pdf.set_font("Helvetica", "", 10)
    for line in [
        f"Total trades: {total}   Won: {won}   Lost: {lost}   Win rate: {win_rate}",
        f"Total P&L @$100/trade:  ${pnl_100:+.2f}",
        f"Total P&L @$1,000/trade: ${pnl_1000:+.2f}",
    ]:
        pdf.cell(0, 6, line, ln=True)

    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, "All Trades", ln=True)

    # Table header
    pdf.set_font("Helvetica", "B", 8)
    cols = ["#", "Date", "Poly market", "Kalshi market", "Spread%", "P&L $100", "Outcome"]
    widths = [8, 22, 60, 60, 16, 16, 16]
    for col, w in zip(cols, widths):
        pdf.cell(w, 6, col, border=1)
    pdf.ln()

    # Rows
    pdf.set_font("Helvetica", "", 7)
    for t in trades:
        date   = (t.get("first_seen_at") or "")[:10]
        spread = f"{(t.get('entry_net_spread') or 0)*100:.2f}%"
        pnl    = f"${t['pnl_100']:+.2f}" if t.get("pnl_100") is not None else "open"
        row    = [
            str(t["id"]),
            date,
            str(t.get("poly_title") or "")[:40],
            str(t.get("kalshi_title") or "")[:40],
            spread,
            pnl,
            t.get("outcome", "?"),
        ]
        for val, w in zip(row, widths):
            pdf.cell(w, 5, val, border=1)
        pdf.ln()

    pdf.output(str(path))


# ─── Performance metrics ──────────────────────────────────────────────────────

def _print_performance_metrics(trades: list[dict], stats: dict) -> None:
    resolved = [t for t in trades if t.get("pnl_100") is not None]
    if not resolved:
        console.print("[dim]No resolved trades yet — performance metrics unavailable.[/]")
        return

    console.print("\n[bold]Performance Metrics[/]")
    console.print("─" * 50)

    # Sharpe ratio (annualised, using $100/trade daily series)
    daily_pnl = _daily_pnl_series(resolved)
    sharpe = _sharpe(list(daily_pnl.values()))
    if len(daily_pnl) >= 2:
        console.print(f"  Sharpe ratio (annualised):  {sharpe:.2f}")
    else:
        console.print("  Sharpe ratio: insufficient data (< 2 resolved days)")

    # Max drawdown
    cum_pnl = _cumulative_pnl(resolved)
    max_dd  = _max_drawdown(cum_pnl)
    console.print(f"  Max drawdown (@$100/trade): ${max_dd:.2f}")

    # Win rate by category
    cat_stats: dict[str, dict] = defaultdict(lambda: {"won": 0, "lost": 0})
    for t in resolved:
        cat = t.get("category") or "Other"
        if t["outcome"] == "WON":
            cat_stats[cat]["won"] += 1
        elif t["outcome"] == "LOST":
            cat_stats[cat]["lost"] += 1

    console.print("\n  Win rate by category:")
    for cat in sorted(cat_stats):
        c = cat_stats[cat]
        decided = c["won"] + c["lost"]
        pct = f"{c['won']/decided*100:.0f}%" if decided else "—"
        console.print(f"    {cat:<16} {c['won']}W / {c['lost']}L  ({pct})")

    # Monthly P&L breakdown (compact)
    monthly: dict[str, float] = defaultdict(float)
    for t in resolved:
        key = (t.get("resolved_at") or t.get("first_seen_at") or "")[:7]
        if key:
            monthly[key] += t.get("pnl_100") or 0

    if monthly:
        console.print("\n  Monthly P&L (@$100/trade):")
        for m in sorted(monthly):
            val = monthly[m]
            sign = "+" if val >= 0 else ""
            colour = "green" if val >= 0 else "red"
            console.print(f"    {m}   [{colour}]${sign}{val:.2f}[/]")

    console.print()


# ─── Calculation helpers ──────────────────────────────────────────────────────

def _daily_pnl_series(resolved: list[dict]) -> dict[str, float]:
    """Sum of pnl_100 grouped by resolved_at date."""
    daily: dict[str, float] = defaultdict(float)
    for t in resolved:
        date = (t.get("resolved_at") or t.get("first_seen_at") or "")[:10]
        if date:
            daily[date] += t.get("pnl_100") or 0
    return dict(sorted(daily.items()))


def _sharpe(daily_pnl_values: list[float], risk_free_daily: float = 0.0) -> float:
    """Annualised Sharpe ratio using daily P&L series. Returns 0.0 if < 2 data points."""
    if len(daily_pnl_values) < 2:
        return 0.0
    excess = [v - risk_free_daily for v in daily_pnl_values]
    mean   = statistics.mean(excess)
    stdev  = statistics.stdev(excess)
    if stdev == 0:
        return 0.0
    return (mean / stdev) * (252 ** 0.5)


def _cumulative_pnl(resolved: list[dict]) -> list[float]:
    """Running cumulative P&L at $100/trade, ordered by resolved_at."""
    sorted_trades = sorted(
        resolved,
        key=lambda t: t.get("resolved_at") or t.get("first_seen_at") or "",
    )
    cumulative, total = [], 0.0
    for t in sorted_trades:
        total += t.get("pnl_100") or 0
        cumulative.append(total)
    return cumulative


def _max_drawdown(cum_pnl: list[float]) -> float:
    """Maximum peak-to-trough decline (negative value, or 0 if no drawdown)."""
    peak, max_dd = float("-inf"), 0.0
    for val in cum_pnl:
        peak  = max(peak, val)
        max_dd = min(max_dd, val - peak)
    return max_dd
