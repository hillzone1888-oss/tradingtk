"""Human-readable rendering of results — terminal and self-contained HTML.

Both renderers put the honesty warnings above the numbers. That ordering is the
point of this package, not a styling choice: a P&L figure read without its
sample size feels like evidence, and a report is exactly where that rule gets
quietly broken.
"""

from tradetk.report.console import render_backtest
from tradetk.report.html import render_backtest_report, write_backtest_report

__all__ = ["render_backtest", "render_backtest_report", "write_backtest_report"]
