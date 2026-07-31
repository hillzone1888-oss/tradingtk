"""Historical underlying data, queryable only as-of a moment.

The recorded tape holds the venue's books but not the underlying's price, so the
replay reads that from historical candles. Candles are immutable history, which
makes fetching them at backtest time legitimate — but only if the engine can
never see one that had not closed yet.

**That guarantee lives here and is structural.** :class:`CandleSeries` indexes on
``close_ms`` and every accessor takes a timestamp and bisects to it, so there is
no method that returns a candle from the future. The engine could not look ahead
if it tried, which is the only version of this property worth having: a backtest
that merely *intends* not to peek always eventually peeks, and the symptom is
excellent results.

A candle is only visible once it has **closed**. Using the in-progress candle's
close as "spot" would leak the rest of that interval — a subtle, small, and
completely fatal leak on 15-minute contracts.
"""

from __future__ import annotations

import logging
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from tradetk.signals.base import Candle, DataValidationError, realized_vol_from_closes
from tradetk.strategy.base import MarketSnapshot

log = logging.getLogger("tradetk.backtest.marketdata")


def _ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


@dataclass(frozen=True)
class VolEstimate:
    sigma_annual: float
    n_samples: int
    interval: str
    lookback_days: int


class CandleSeries:
    """Closed candles for one symbol, with strictly as-of access."""

    def __init__(self, symbol: str, candles: Iterable[Candle], *, interval: str = "1h") -> None:
        self.symbol = symbol.upper()
        self.interval = interval
        ordered = sorted(candles, key=lambda c: c.close_ms)
        self._candles = ordered
        self._close_ms = [c.close_ms for c in ordered]
        self._closes = [c.c for c in ordered]

    def __len__(self) -> int:
        return len(self._candles)

    @property
    def is_empty(self) -> bool:
        return not self._candles

    def _visible_count(self, when: datetime) -> int:
        """How many candles had *closed* by `when`. The one guard everything
        else is built on: strictly ``<=``, so the in-progress candle is unseen."""
        return bisect_right(self._close_ms, _ms(when))

    def spot_at(self, when: datetime) -> float | None:
        """Close of the most recent candle to have closed at or before `when`."""
        count = self._visible_count(when)
        return self._closes[count - 1] if count else None

    def closes_until(self, when: datetime, *, limit: int | None = None) -> list[float]:
        count = self._visible_count(when)
        closes = self._closes[:count]
        return closes[-limit:] if limit is not None else closes

    def realized_vol_at(
        self, when: datetime, *, lookback_days: int = 30
    ) -> VolEstimate | None:
        """Close-to-close realized vol from candles visible at `when`.

        Reuses the same pure function the live provider uses, so a backtest and
        a live run cannot drift apart on the definition of volatility.
        """
        cutoff = when - timedelta(days=lookback_days)
        start = bisect_right(self._close_ms, _ms(cutoff))
        end = self._visible_count(when)
        closes = self._closes[start:end]
        if len(closes) < 2:
            return None
        try:
            vol = realized_vol_from_closes(closes, self.interval, lookback_days)
        except DataValidationError as exc:
            log.warning("%s: realized vol unavailable at %s: %s", self.symbol, when, exc)
            return None
        return VolEstimate(
            sigma_annual=vol.sigma_annual,
            n_samples=vol.n_samples,
            interval=self.interval,
            lookback_days=lookback_days,
        )

    def snapshot_at(
        self, when: datetime, *, lookback_days: int = 30
    ) -> MarketSnapshot | None:
        """Build the frozen snapshot a strategy is allowed to see at `when`."""
        spot = self.spot_at(when)
        if spot is None:
            return None
        vol = self.realized_vol_at(when, lookback_days=lookback_days)
        if vol is None:
            return None
        return MarketSnapshot(
            symbol=self.symbol,
            as_of=when,
            spot=spot,
            sigma_annual=vol.sigma_annual,
            sigma_source=f"realized_vol {vol.interval}/{vol.lookback_days}d",
            n_vol_samples=vol.n_samples,
        )

    def coverage(self) -> dict[str, Any]:
        if self.is_empty:
            return {"symbol": self.symbol, "candles": 0}
        return {
            "symbol": self.symbol,
            "candles": len(self._candles),
            "interval": self.interval,
            "first_close": datetime.fromtimestamp(
                self._close_ms[0] / 1000, tz=self._tz()
            ).isoformat(),
            "last_close": datetime.fromtimestamp(
                self._close_ms[-1] / 1000, tz=self._tz()
            ).isoformat(),
        }

    @staticmethod
    def _tz():
        from datetime import timezone

        return timezone.utc


class MarketDataSet:
    """Candle series by symbol, with the same as-of contract."""

    def __init__(self, series: dict[str, CandleSeries]) -> None:
        self._series = {k.upper(): v for k, v in series.items()}

    def __contains__(self, symbol: str) -> bool:
        return symbol.upper() in self._series

    @property
    def symbols(self) -> set[str]:
        return set(self._series)

    def series(self, symbol: str) -> CandleSeries | None:
        return self._series.get(symbol.upper())

    def snapshot_at(
        self, symbol: str, when: datetime, *, lookback_days: int = 30
    ) -> MarketSnapshot | None:
        series = self.series(symbol)
        if series is None:
            return None
        return series.snapshot_at(when, lookback_days=lookback_days)

    def spot_at(self, symbol: str, when: datetime) -> float | None:
        series = self.series(symbol)
        return series.spot_at(when) if series else None

    def coverage(self) -> list[dict[str, Any]]:
        return [s.coverage() for s in self._series.values()]
