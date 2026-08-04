"""``chart`` — view an underlying's price against a Kalshi contract's implied odds.

Read-only and keyless. The underlying OHLC comes from Hyperliquid's public
``candleSnapshot``; the contract's implied-probability history is reconstructed
from book snapshots this project recorded itself (the same tape the shadow
evaluator reads). Renders a two-panel PNG so both the assistant and the operator
can actually look at the price action while designing a strategy.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Iterable

import matplotlib

matplotlib.use("Agg")  # headless: never try to open a window
import matplotlib.pyplot as plt  # noqa: E402 - must follow matplotlib.use
import truststore  # noqa: E402 - must follow matplotlib.use

from tradetk.backtest.replay import BookObservation, ReplayError, TapeReplay  # noqa: E402
from tradetk.signals.base import Candle  # noqa: E402
from tradetk.signals.hyperliquid import HyperliquidProvider  # noqa: E402
from tradetk.translation.claims import UnderlyingRegistry  # noqa: E402


def implied_prob_series(
    observations: Iterable[BookObservation], ticker: str
) -> list[tuple[datetime, float]]:
    """Time-ordered ``(observed_at, yes_mid)`` for ``ticker``.

    ``yes_mid`` is the book's informational midpoint — implied probability. A
    one-sided book (no bid or no ask) has no mid and is skipped rather than
    guessed; a chart that invented a price on a half-empty book would mislead.
    """
    out: list[tuple[datetime, float]] = []
    for obs in observations:
        if obs.ticker != ticker:
            continue
        mid = obs.book.mid
        if mid is None:
            continue
        out.append((obs.observed_at, float(mid)))
    out.sort(key=lambda row: row[0])
    return out


def candles_to_ohlc(
    candles: Iterable[Candle],
) -> tuple[list[datetime], list[float], list[float], list[float], list[float]]:
    """Split candles into parallel, time-ordered plotting arrays (UTC)."""
    rows = sorted(candles, key=lambda k: k.open_ms)
    times = [datetime.fromtimestamp(k.open_ms / 1000.0, tz=timezone.utc) for k in rows]
    return (
        times,
        [float(k.o) for k in rows],
        [float(k.h) for k in rows],
        [float(k.l) for k in rows],
        [float(k.c) for k in rows],
    )


def series_span(series: list[tuple[datetime, float]]) -> tuple[datetime, datetime]:
    """First and last timestamp of a non-empty ``(time, value)`` series."""
    if not series:
        raise ValueError("cannot take the span of an empty series")
    times = [row[0] for row in series]
    return min(times), max(times)


def render_chart(
    *,
    ticker: str,
    symbol: str,
    prob_series: list[tuple[datetime, float]],
    ohlc: tuple[list[datetime], list[float], list[float], list[float], list[float]],
    out_path: str,
    strike: float | None = None,
) -> str:
    """Render underlying price (top) vs. implied odds (bottom) to a PNG."""
    times, _opens, highs, lows, closes = ohlc
    fig, (ax_price, ax_prob) = plt.subplots(
        2, 1, figsize=(11, 7), sharex=True, height_ratios=[2, 1]
    )

    # Top: underlying close with a high–low band.
    if times:
        ax_price.plot(times, closes, color="#1f77b4", linewidth=1.3, label=f"{symbol} close")
        ax_price.fill_between(times, lows, highs, color="#1f77b4", alpha=0.15, label="high–low")
    if strike is not None:
        ax_price.axhline(strike, color="#d62728", linestyle="--", linewidth=1.0, label="strike")
    ax_price.set_ylabel(f"{symbol} price")
    ax_price.set_title(f"{ticker}  —  {symbol} price vs. contract implied odds")
    ax_price.legend(loc="upper left", fontsize=8)
    ax_price.grid(True, alpha=0.25)

    # Bottom: implied probability, fixed 0..1.
    if prob_series:
        p_times = [row[0] for row in prob_series]
        p_vals = [row[1] for row in prob_series]
        ax_prob.plot(p_times, p_vals, color="#2ca02c", linewidth=1.3, marker=".", markersize=4)
    ax_prob.set_ylim(0.0, 1.0)
    ax_prob.set_ylabel("implied P(yes)")
    ax_prob.set_xlabel("time (UTC)")
    ax_prob.grid(True, alpha=0.25)

    fig.autofmt_xdate()
    fig.tight_layout()
    parent = os.path.dirname(out_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def _provider_factory() -> HyperliquidProvider:
    """Indirection so tests can substitute a fake candle provider."""
    return HyperliquidProvider()


def _default_out(ticker: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"data/charts/{ticker}-{stamp}.png"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Chart an underlying vs. a contract's implied odds.")
    ap.add_argument("--ticker", required=True, help="Kalshi contract ticker to chart.")
    ap.add_argument("--interval", default="1h", help="Hyperliquid candle interval (default 1h).")
    ap.add_argument("--tape-dir", default="data/tape")
    ap.add_argument("--registry", default="config/underlyings.yaml")
    ap.add_argument("--symbol", default=None,
                    help="Underlying symbol override; inferred from the tape's claim if omitted.")
    ap.add_argument("--out", default=None, help="PNG path (default data/charts/<ticker>-<ts>.png).")
    args = ap.parse_args(argv)

    truststore.inject_into_ssl()
    out_path = args.out or _default_out(args.ticker)

    try:
        replay = TapeReplay.from_tape(args.tape_dir)
    except ReplayError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2

    prob = implied_prob_series(replay.observations(), args.ticker)
    if not prob:
        print(json.dumps({"ok": False, "error":
                          f"no book observations for {args.ticker!r} on the tape at "
                          f"{args.tape_dir}; run `record` for it first"}))
        return 2

    start, end = series_span(prob)

    # Underlying symbol + strike, inferred from the contract's claim unless overridden.
    symbol = args.symbol
    strike: float | None = None
    claim = replay.claim_as_of(args.ticker, end, UnderlyingRegistry.from_yaml(args.registry))
    if claim is not None:
        symbol = symbol or getattr(claim, "underlying", None)
        thr = getattr(claim, "threshold", None)
        strike = float(thr) if thr is not None else None
    if not symbol:
        print(json.dumps({"ok": False, "error":
                          f"could not infer underlying for {args.ticker!r}; pass --symbol"}))
        return 2

    start_ms = int(start.timestamp() * 1000) - 3_600_000  # small left pad
    end_ms = int(end.timestamp() * 1000)
    with _provider_factory() as provider:
        candles = provider.candles(symbol, args.interval, start_ms, end_ms)
    ohlc = candles_to_ohlc(candles)

    render_chart(ticker=args.ticker, symbol=symbol, prob_series=prob, ohlc=ohlc,
                 out_path=out_path, strike=strike)

    print(json.dumps({
        "ok": True, "out": out_path, "ticker": args.ticker, "symbol": symbol,
        "prob_points": len(prob), "candles": len(candles),
        "span": [start.isoformat(), end.isoformat()],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
