"""``chart`` — view an underlying's price against a Kalshi contract's implied odds.

Read-only and keyless. The underlying OHLC comes from Hyperliquid's public
``candleSnapshot``; the contract's implied-probability history is reconstructed
from book snapshots this project recorded itself (the same tape the shadow
evaluator reads). Renders a two-panel PNG so both the assistant and the operator
can actually look at the price action while designing a strategy.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

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
