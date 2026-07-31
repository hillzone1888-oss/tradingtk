"""Terminal rendering of a backtest result.

Same ordering rule as the HTML report: the caveats print *before* the numbers.
A win rate that scrolls past on its own reads as a finding; the same number
under a red panel reading "17 minutes of tape, 0 settled contracts" reads as
what it is.

JSON remains the machine-readable output of every command (``--json``). This is
the human view, not a replacement for it.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from tradetk.backtest.engine import BacktestResult, calibration_buckets


def _pnl_text(value: Any) -> Text:
    amount = float(value)
    colour = "green" if amount > 0 else "red" if amount < 0 else "dim"
    return Text(f"${value}", style=colour)


def render_backtest(result: BacktestResult, console: Console | None = None) -> None:
    """Print a full backtest result, caveats first."""
    out = console or Console()
    summary = result.summary()

    warnings = result.honesty_warnings()
    if warnings:
        body = Text()
        for i, warning in enumerate(warnings):
            if i:
                body.append("\n\n")
            body.append("• ", style="bold yellow")
            body.append(warning)
        out.print(
            Panel(
                body,
                title="[bold yellow]Read this before the numbers[/]",
                border_style="yellow",
                padding=(1, 2),
            )
        )
        out.print()

    stats = Table.grid(padding=(0, 3))
    stats.add_column(style="dim")
    stats.add_column(justify="right")
    stats.add_row("strategy", result.strategy)
    stats.add_row("tape span", f'{result.tape.get("tape_span_days", 0):.4f} days')
    stats.add_row("observations", str(result.tape.get("observations", 0)))
    stats.add_row("positions opened", str(summary["trades_opened"]))
    stats.add_row("positions settled", str(summary["trades_settled"]))
    stats.add_row(
        "win rate",
        f'{summary["win_rate"]:.1%}' if summary["win_rate"] is not None else "—",
    )
    stats.add_row("realized P&L", _pnl_text(summary["realized_pnl"]))
    brier = summary["brier_score"]
    stats.add_row(
        "Brier score",
        f"{brier:.4f}  (0.25 = always 50%)" if brier is not None else "—",
    )
    out.print(Panel(stats, title="Result", border_style="blue", padding=(1, 2)))

    if result.trades:
        table = Table(title="Trades", header_style="dim", expand=False)
        for column in ("ticker", "side", "entry", "n", "avg px", "cost", "edge pp", "p"):
            table.add_column(column)
        table.add_column("outcome")
        table.add_column("P&L", justify="right")
        for trade in result.trades:
            outcome = (
                Text("unsettled", style="dim") if trade.resolved is None
                else Text("YES", style="green") if trade.resolved
                else Text("NO", style="red")
            )
            table.add_row(
                trade.ticker,
                trade.side.value.upper(),
                trade.entry_time.strftime("%m-%d %H:%M"),
                str(trade.contracts),
                str(trade.average_price),
                f"${trade.cost}",
                f"{trade.net_edge_pp:.2f}",
                f"{trade.p_estimate:.3f}",
                outcome,
                _pnl_text(trade.pnl),
            )
        out.print()
        out.print(table)

    buckets = calibration_buckets(result.trades)
    if buckets:
        table = Table(title="Calibration", header_style="dim")
        for column in ("bucket", "n", "predicted", "observed", "gap"):
            table.add_column(column)
        for bucket in buckets:
            gap = bucket.observed_frequency - bucket.mean_predicted
            table.add_row(
                f"{bucket.lower:.1f}–{bucket.upper:.1f}",
                str(bucket.n),
                f"{bucket.mean_predicted:.3f}",
                f"{bucket.observed_frequency:.3f}",
                Text(f"{gap:+.3f}", style="green" if abs(gap) < 0.1 else "yellow"),
            )
        out.print()
        out.print(table)

    if result.skipped:
        table = Table(title="Why markets were skipped", header_style="dim")
        table.add_column("reason")
        table.add_column("count", justify="right")
        for reason, count in sorted(result.skipped.items(), key=lambda kv: -kv[1]):
            table.add_row(reason.replace("_", " "), str(count))
        out.print()
        out.print(table)
