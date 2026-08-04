"""``chart`` — view an underlying's price against a Kalshi contract's implied odds.

Read-only and keyless. The underlying OHLC comes from Hyperliquid's public
``candleSnapshot``; the contract's implied-probability history is reconstructed
from book snapshots this project recorded itself (the same tape the shadow
evaluator reads). Renders a two-panel PNG so both the assistant and the operator
can actually look at the price action while designing a strategy.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Iterable

import matplotlib

matplotlib.use("Agg")  # headless: never try to open a window
import matplotlib.pyplot as plt  # noqa: E402 - must follow matplotlib.use

from tradetk.backtest.replay import BookObservation
from tradetk.signals.base import Candle


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
