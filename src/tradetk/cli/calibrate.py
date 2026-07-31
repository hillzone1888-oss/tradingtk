"""``calibrate`` — is the model better than the price it would trade against?

    uv run python -m tradetk.cli.calibrate
    uv run python -m tradetk.cli.calibrate --html calibration.html
    uv run python -m tradetk.cli.calibrate --json --pretty

This is the command that answers "is it working?". Not the balance, which on a
$20 book is noise, and not the backtest P&L, which is the same noise with extra
steps. The headline is a single comparison: the model's Brier score against the
Brier score of Kalshi's own mid, on exactly the same contracts.

If the market wins that comparison, there is no edge — every trade would be
paying a spread to be more wrong — and the report says so in those words.

Reads the shadow log and historical candles. Contacts no venue.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import truststore
from rich.console import Console

from tradetk.backtest.settlement import CandleSettlement
from tradetk.cli.backtest import load_underlying_data
from tradetk.report.calibration_html import write_calibration_report
from tradetk.report.console import render_calibration
from tradetk.shadow.calibration import build_report
from tradetk.shadow.records import ShadowStore
from tradetk.signals.hyperliquid import HyperliquidProvider

log = logging.getLogger("tradetk.cli.calibrate")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Score shadow forecasts against outcomes, and against the market."
    )
    ap.add_argument("--shadow-dir", default="data/shadow")
    ap.add_argument("--strategy", default=None, help="Restrict to one strategy.")
    ap.add_argument("--vol-lookback-days", type=int, default=1,
                    help="Candle history padding either side of the forecast window.")
    ap.add_argument("--html", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr
    )
    truststore.inject_into_ssl()

    records = ShadowStore(args.shadow_dir).read()
    if args.strategy:
        records = [r for r in records if r.strategy == args.strategy]

    if not records:
        print(
            "error: the shadow log is empty. Run `record` to build a tape, then "
            "`shadow` to score it — calibration has nothing to score until then.",
            file=sys.stderr,
        )
        return 2

    # Candles must cover every forecast's resolution time, not just the window
    # the forecasts were made in.
    symbols = {r.underlying for r in records}
    earliest = min(r.observed_at for r in records)
    latest = min(
        max(r.resolution_time for r in records) + timedelta(hours=1),
        datetime.now(timezone.utc),
    )
    with HyperliquidProvider() as provider:
        data = load_underlying_data(
            provider, symbols, start=earliest, end=latest,
            lookback_days=args.vol_lookback_days,
        )

    report = build_report(records, CandleSettlement(data))

    if args.html:
        path = write_calibration_report(report, args.html)
        print(f"report written to {Path(path).resolve()}", file=sys.stderr)

    if args.json:
        print(json.dumps(report.as_dict(), indent=2 if args.pretty else None, default=str))
    else:
        render_calibration(report, Console())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
