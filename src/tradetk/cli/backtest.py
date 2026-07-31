"""``backtest`` — replay the recorded tape through a strategy.

    uv run python -m tradetk.cli.backtest
    uv run python -m tradetk.cli.backtest --strategy baseline_vol --html out.html
    uv run python -m tradetk.cli.backtest --fixed-contracts 2 --json

Free: it replays the orderbook depth this project recorded itself, so there is
no data subscription and no third-party platform involved. The only external
call is for the underlying's historical candles, which are immutable public
history and are filtered to as-of visibility before any strategy sees them.

Read-only and safe to run constantly. It contacts market data and nothing else;
there is no order path anywhere in this module's import graph.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import truststore
from rich.console import Console

from tradetk.backtest.engine import BacktestEngine
from tradetk.backtest.marketdata import CandleSeries, MarketDataSet
from tradetk.backtest.replay import ReplayError, TapeReplay
from tradetk.backtest.settlement import CandleSettlement
from tradetk.costs.fees import FeeRounding, KalshiFeeModel
from tradetk.report.console import render_backtest
from tradetk.report.html import write_backtest_report
from tradetk.signals.hyperliquid import HyperliquidProvider
from tradetk.strategy import available_strategies, get_strategy
from tradetk.translation.claims import UnderlyingRegistry
from tradetk.translation.edge import GateLimits
from tradetk.translation.sizing import SizingLimits, SizingMode

log = logging.getLogger("tradetk.cli.backtest")

#: Hyperliquid's candle endpoint caps a response, so long lookbacks are pulled
#: in chunks. 1h candles over 60 days is ~1440 rows, comfortably inside one call.
CANDLE_INTERVAL = "1h"


def load_underlying_data(
    provider: HyperliquidProvider,
    symbols: set[str],
    *,
    start: datetime,
    end: datetime,
    lookback_days: int,
) -> MarketDataSet:
    """Fetch candles covering the replay window plus the vol lookback.

    The window is extended *backwards* by the lookback so the very first
    observation already has a full vol history — otherwise early observations
    silently abstain and the result quietly under-reports opportunities.
    """
    series: dict[str, CandleSeries] = {}
    fetch_from = start - timedelta(days=lookback_days + 1)
    for symbol in sorted(symbols):
        try:
            candles = provider.candles(
                symbol,
                CANDLE_INTERVAL,
                int(fetch_from.timestamp() * 1000),
                int(end.timestamp() * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - one dead symbol must not kill the run
            log.warning("no candles for %s: %s", symbol, exc)
            continue
        if candles:
            series[symbol] = CandleSeries(symbol, candles, interval=CANDLE_INTERVAL)
    return MarketDataSet(series)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Replay the recorded tape through a strategy and the full gate stack."
    )
    ap.add_argument("--tape-dir", default="data/tape")
    ap.add_argument("--registry", default="config/underlyings.yaml")
    ap.add_argument(
        "--strategy", default="baseline_vol",
        help=f"One of: {', '.join(available_strategies()) or '(none)'}",
    )
    ap.add_argument("--vol-multiplier", type=float, default=1.0,
                    help="Scale the realized-vol input. Do NOT fit this to results.")
    ap.add_argument("--vol-lookback-days", type=int, default=30)

    ap.add_argument("--total-capital", type=str, default="20.00")
    ap.add_argument("--position-target", type=str, default="2.00")
    ap.add_argument("--per-position-ceiling", type=str, default="3.00")
    ap.add_argument("--max-positions", type=int, default=6)
    ap.add_argument("--max-per-underlying", type=int, default=2)
    ap.add_argument(
        "--fixed-contracts", type=int, default=None,
        help="Size every position at exactly N contracts instead of a dollar target.",
    )

    ap.add_argument("--min-edge-pp", type=float, default=3.0)
    ap.add_argument("--margin-pp", type=float, default=1.0)
    ap.add_argument("--min-depth-multiple", type=float, default=5.0)
    ap.add_argument("--max-participation-pct", type=float, default=10.0)
    ap.add_argument("--max-hours", type=float, default=168.0)
    ap.add_argument(
        "--allow-deep-tail", action="store_true",
        help="Permit estimates far into the lognormal's unreliable tail.",
    )
    ap.add_argument("--rounding", default="cent", choices=[r.value for r in FeeRounding])

    ap.add_argument("--html", default=None, help="Write a self-contained HTML report here.")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of the table view.")
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr
    )
    truststore.inject_into_ssl()

    registry = UnderlyingRegistry.from_yaml(args.registry)

    try:
        replay = TapeReplay.from_tape(args.tape_dir)
    except ReplayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    start, end = replay.span
    # Extend the far end to the latest resolution any recorded claim reaches, so
    # positions can actually settle inside the fetched candle window.
    horizon = end
    for ticker in replay.tickers:
        claim = replay.claim_as_of(ticker, end, registry)
        if claim is not None:
            horizon = max(horizon, claim.resolution_time)
    horizon = min(horizon, datetime.now(timezone.utc))

    symbols = {
        claim.underlying
        for ticker in replay.tickers
        if (claim := replay.claim_as_of(ticker, end, registry)) is not None
    }

    with HyperliquidProvider() as provider:
        data = load_underlying_data(
            provider, symbols, start=start, end=horizon,
            lookback_days=args.vol_lookback_days,
        )

    sizing = SizingLimits(
        position_target=Decimal(args.position_target),
        per_position_ceiling=Decimal(args.per_position_ceiling),
        total_capital=Decimal(args.total_capital),
        max_book_participation_pct=Decimal(str(args.max_participation_pct)),
        mode=(
            SizingMode.fixed_contracts if args.fixed_contracts
            else SizingMode.fixed_dollar
        ),
        fixed_contracts=args.fixed_contracts or 1,
    )
    gate = GateLimits(
        min_net_edge_pp=Decimal(str(args.min_edge_pp)),
        margin_pp=Decimal(str(args.margin_pp)),
        min_book_depth_multiple=Decimal(str(args.min_depth_multiple)),
        max_book_participation_pct=Decimal(str(args.max_participation_pct)),
        max_hours_to_resolution=Decimal(str(args.max_hours)),
        reject_deep_tail=not args.allow_deep_tail,
    )

    strategy = get_strategy(args.strategy, vol_multiplier=args.vol_multiplier)
    engine = BacktestEngine(
        strategy=strategy,
        registry=registry,
        data=data,
        settlement_source=CandleSettlement(data),
        fee_model=KalshiFeeModel(rounding=FeeRounding(args.rounding)),
        gate_limits=gate,
        sizing_limits=sizing,
        max_positions=args.max_positions,
        max_slots_per_underlying=args.max_per_underlying,
        vol_lookback_days=args.vol_lookback_days,
    )
    result = engine.run(replay)

    if args.html:
        path = write_backtest_report(
            result, args.html, data, title=f"tradetk backtest — {strategy.name}"
        )
        print(f"report written to {Path(path).resolve()}", file=sys.stderr)

    if args.json:
        print(json.dumps(result.as_dict(), indent=2 if args.pretty else None, default=str))
    else:
        render_backtest(result, Console())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
