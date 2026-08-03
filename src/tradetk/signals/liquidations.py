"""Forced-liquidation flow — the typed event, and the window statistic a strategy reads.

A liquidation is not a trade anybody chose to make. When a leveraged perp
position can no longer meet margin the exchange closes it at market: longs are
closed by **selling**, shorts by **buying**. That flow is price-insensitive,
concentrated in time, and mechanically correlated with the move that caused it —
which is why it is worth modelling separately from ordinary volume.

This module does exactly two things:

* **Types the event.** A provider cannot hand a strategy an untyped dict whose
  ``side`` convention silently flips between feeds.
* **Reduces a stream to one window statistic** (:class:`LiquidationProfile`),
  with the look-ahead check inside the reduction, so a backtest cannot summarise
  liquidations that had not happened yet at the moment being replayed.

**Side convention, stated once and pinned by a test.** ``side`` is the side of
the *position that was liquidated*, which is the opposite of the resulting
market order: ``LiquidationSide.long`` means longs were force-**sold**
(downward pressure) and ``LiquidationSide.short`` means shorts were
force-**bought** (upward pressure). Feeds disagree about this, and the two
conventions are indistinguishable from the numbers alone — the sign error is
invisible until it has quietly inverted every trade the strategy makes. An
adapter must map explicitly; it may never pass a feed's string through.

**Nothing here fetches.** No provider advertises :attr:`Capability.LIQUIDATIONS`
yet (Moon Dev's HL-derived endpoints are not implemented — see
:mod:`tradetk.signals.moondev`), so a strategy requiring it fails loudly at
startup via ``require_capabilities`` rather than running on zeros. These types
are the contract that adapter will have to satisfy, and they are testable
without one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field

from tradetk.signals.base import DataValidationError, Finite

#: Default reduction window. An hour is long enough to contain a cascade and
#: short enough that the statistic is still about *now* — but it is a stated
#: choice, not a measured one, which is why it is a parameter everywhere.
DEFAULT_WINDOW_MINUTES = 60


class LiquidationDataError(DataValidationError):
    """Liquidation input that must not be summarised (wrong symbol, look-ahead)."""


class LiquidationSide(str, Enum):
    """The side of the position that was closed — NOT the side of the fill."""

    long = "long"  # longs force-SOLD  -> downward pressure
    short = "short"  # shorts force-BOUGHT -> upward pressure


class _LiqModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LiquidationEvent(_LiqModel):
    """One forced position close, as reported by a feed."""

    symbol: str
    time_ms: int = Field(ge=0)
    side: LiquidationSide
    price: Finite = Field(gt=0)
    notional_usd: Finite = Field(gt=0)


class LiquidationProfile(_LiqModel):
    """What one window of forced flow looked like, as of one instant.

    Carries the raw sums rather than only the derived ratio: a 60/40 split of
    $50k and a 60/40 split of $50m are the same imbalance and completely
    different evidence, and a consumer that only sees the ratio cannot tell.
    """

    symbol: str
    as_of: datetime
    window_minutes: int = Field(gt=0)
    n_events: int = Field(ge=0)
    long_notional_usd: Finite = Field(ge=0)
    short_notional_usd: Finite = Field(ge=0)
    largest_event_usd: Finite = Field(ge=0)

    @property
    def total_notional_usd(self) -> float:
        return self.long_notional_usd + self.short_notional_usd

    @property
    def imbalance(self) -> float:
        """``(short - long) / total`` in ``[-1, 1]``. Positive = net forced *buying*.

        Signed so that the number reads as pressure on price: forced buying is
        positive, forced selling is negative, and an empty or perfectly balanced
        window is 0.0 — which is "no direction", not "no data". The two are
        distinguished by :attr:`n_events`, and the consumer is expected to gate
        on that separately.
        """
        total = self.total_notional_usd
        return 0.0 if total <= 0 else (self.short_notional_usd - self.long_notional_usd) / total

    @property
    def concentration(self) -> float:
        """Share of the window's notional sitting in its single largest event.

        A 90% imbalance built from one whale is a different claim about the
        market than the same imbalance built from four hundred small closes: the
        first is one account's margin call, the second is a cascade. This is the
        number that tells them apart.
        """
        total = self.total_notional_usd
        return 0.0 if total <= 0 else self.largest_event_usd / total

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "as_of": self.as_of.isoformat(),
            "window_minutes": self.window_minutes,
            "n_events": self.n_events,
            "long_notional_usd": round(self.long_notional_usd, 2),
            "short_notional_usd": round(self.short_notional_usd, 2),
            "total_notional_usd": round(self.total_notional_usd, 2),
            "largest_event_usd": round(self.largest_event_usd, 2),
            "imbalance": round(self.imbalance, 6),
            "concentration": round(self.concentration, 6),
        }


def build_liquidation_profile(
    events: Iterable[LiquidationEvent],
    *,
    symbol: str,
    as_of: datetime,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
) -> LiquidationProfile:
    """Reduce a stream of events to the window ending at ``as_of``.

    The window is half-open — ``(as_of - window, as_of]`` — so replaying the
    same tape at consecutive timestamps counts each event exactly once.

    Two inputs are refused rather than filtered, because both are wiring bugs
    that produce a plausible-looking number:

    * an event for a **different symbol**, which would price one asset's claims
      off another asset's forced flow; and
    * an event **after** ``as_of``, which in a replay is the future. Dropping
      those silently would make a look-ahead bug look like a quiet window.
    """
    if window_minutes <= 0:
        raise LiquidationDataError(f"window_minutes must be positive, got {window_minutes}")
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)

    as_of_ms = int(as_of.timestamp() * 1000)
    start_ms = as_of_ms - window_minutes * 60_000

    n = 0
    long_usd = 0.0
    short_usd = 0.0
    largest = 0.0
    for e in events:
        if e.symbol.upper() != symbol.upper():
            raise LiquidationDataError(
                f"event for {e.symbol!r} passed to a {symbol!r} profile; forced flow "
                "is per-asset and must never be pooled across underlyings"
            )
        if e.time_ms > as_of_ms:
            raise LiquidationDataError(
                f"liquidation at {e.time_ms} is after as_of {as_of_ms} — refusing to "
                "summarise the future"
            )
        if e.time_ms <= start_ms:
            continue
        n += 1
        if e.side is LiquidationSide.long:
            long_usd += e.notional_usd
        else:
            short_usd += e.notional_usd
        largest = max(largest, e.notional_usd)

    return LiquidationProfile(
        symbol=symbol,
        as_of=as_of,
        window_minutes=window_minutes,
        n_events=n,
        long_notional_usd=long_usd,
        short_notional_usd=short_usd,
        largest_event_usd=largest,
    )
