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
from tradetk.shadow.calibration import CalibrationReport, Comparison


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


def _warnings_panel(warnings: list[str], console: Console) -> None:
    if not warnings:
        return
    body = Text()
    for i, warning in enumerate(warnings):
        if i:
            body.append("\n\n")
        body.append("• ", style="bold yellow")
        body.append(warning)
    console.print(
        Panel(
            body,
            title="[bold yellow]Read this before the numbers[/]",
            border_style="yellow",
            padding=(1, 2),
        )
    )
    console.print()


def _comparison_table(comparison: Comparison, title: str) -> Table:
    """Model beside market on the same contracts, with the decomposition.

    Both forecasters in one table on purpose: the model's Brier in isolation is
    not interpretable, and shown alone it invites being read as a grade.
    """
    table = Table(title=title, header_style="dim")
    table.add_column("metric")
    table.add_column("model", justify="right")
    table.add_column("market mid", justify="right")
    table.add_column("", style="dim")

    model, market = comparison.model, comparison.market

    def cell(value: float | None, digits: int = 4) -> str:
        return f"{value:.{digits}f}" if value is not None else "—"

    better = comparison.edge_exists
    brier_note = (
        "lower is better; 0.25 = always 50%"
        if better is None
        else ("model ahead" if better else "market ahead")
    )
    table.add_row("n", str(model.n), str(market.n), "same contracts, both columns")
    table.add_row(
        "Brier",
        Text(cell(model.brier), style="green" if better else "red" if better is False else ""),
        cell(market.brier),
        brier_note,
    )
    table.add_row("  reliability", cell(model.reliability), cell(market.reliability),
                  "lower better — miscalibration is fixable")
    table.add_row("  resolution", cell(model.resolution), cell(market.resolution),
                  "higher better — zero means no signal")
    table.add_row("  uncertainty", cell(model.uncertainty), cell(market.uncertainty),
                  "base rate variance; a property of the contracts")
    table.add_row("base rate", cell(model.base_rate, 3), cell(market.base_rate, 3),
                  "share resolving YES")
    return table


def render_calibration(report: CalibrationReport, console: Console | None = None) -> None:
    """Print a calibration report — verdict first, caveats before that."""
    out = console or Console()

    _warnings_panel(report.honesty_warnings(), out)

    verdict = report.independent.as_dict()["verdict"]
    beats = report.independent.edge_exists
    style = "green" if beats else "red" if beats is False else "yellow"
    out.print(
        Panel(
            Text(verdict),
            title="[bold]Verdict — does the model beat the market's own price?[/]",
            border_style=style,
            padding=(1, 2),
        )
    )
    out.print()

    out.print(_comparison_table(report.independent, "Headline: one forecast per contract"))

    if report.n_observations != report.n_contracts:
        out.print()
        out.print(
            _comparison_table(
                report.overall,
                f"All {report.n_observations} observations (NOT independent samples)",
            )
        )

    buckets = report.independent.model.buckets
    if buckets:
        table = Table(title="Reliability — model", header_style="dim")
        for column in ("bucket", "n", "predicted", "observed", "gap"):
            table.add_column(column)
        for bucket in buckets:
            table.add_row(
                f"{bucket.lower:.1f}–{bucket.upper:.1f}",
                str(bucket.n),
                f"{bucket.mean_predicted:.3f}",
                f"{bucket.observed_frequency:.3f}",
                Text(
                    f"{bucket.gap:+.3f}",
                    style="green" if abs(bucket.gap) < 0.1 else "yellow",
                ),
            )
        out.print()
        out.print(table)

    for title, segments in (
        ("By market type", report.by_measured_reference),
        ("By underlying", report.by_underlying),
        ("By time to resolution", report.by_horizon),
    ):
        rows = [(name, c) for name, c in segments.items() if c.n_common]
        if not rows:
            continue
        table = Table(title=title, header_style="dim")
        table.add_column("segment")
        table.add_column("n", justify="right")
        table.add_column("model Brier", justify="right")
        table.add_column("market Brier", justify="right")
        table.add_column("verdict")
        for name, comparison in rows:
            beats = comparison.edge_exists
            table.add_row(
                name,
                str(comparison.n_common),
                f"{comparison.model.brier:.4f}" if comparison.model.brier is not None else "—",
                f"{comparison.market.brier:.4f}" if comparison.market.brier is not None else "—",
                Text(
                    "model" if beats else "market" if beats is False else "—",
                    style="green" if beats else "red" if beats is False else "dim",
                ),
            )
        out.print()
        out.print(table)
