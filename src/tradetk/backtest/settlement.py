"""Deciding what actually happened — and being honest about how we know.

A backtest is only as good as its settlement truth. Every trade's P&L is
``payout - cost``, and the payout is entirely determined by whether the claim
resolved YES. Get settlement wrong and every other number is decoration.

**The settlement here is a proxy, and the gap is real.** Kalshi settles these
contracts on a specific index — typically the 60-second average of CF
Benchmarks' real-time index immediately before the close, named in each market's
own rules text and captured by the claim parser. We do not have that series.
What we have is Hyperliquid candles, which are a different venue's trades over a
different averaging window.

For a strike far from spot the two agree and it does not matter. **For a strike
near spot they can disagree, and disagreement flips the outcome from $1 to $0.**
That is not a rounding error, it is the whole position, and near-the-money is
exactly where a vol model produces its most confident estimates. So:

* every settled trade records which source decided it, and
* :class:`SettlementReport` counts how many settlements landed close enough to
  the strike that the proxy could plausibly have been wrong.

That count is the number to look at before believing a win rate. The honest fix
is reconciling against the venue's own settled results once the recorder has
seen contracts through resolution; :class:`RecordedStatusSettlement` is where
that plugs in.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from tradetk.backtest.marketdata import MarketDataSet
from tradetk.translation.claims import Claim

log = logging.getLogger("tradetk.backtest.settlement")

#: A settlement whose underlying landed within this fraction of the strike is
#: flagged: a different index or averaging window could have resolved it the
#: other way. 0.1% is roughly the spread between major spot venues.
NEAR_STRIKE_FRACTION = 0.001


@runtime_checkable
class SettlementSource(Protocol):
    """Anything that can say what an underlying was worth at a moment."""

    name: str

    def value_at(self, symbol: str, when: datetime) -> float | None: ...


@dataclass(frozen=True)
class Settlement:
    """One claim's outcome, with the evidence that decided it."""

    ticker: str
    resolved: bool | None
    settled_value: float | None
    source: str
    near_strike: bool
    distance_to_strike_pct: float | None
    reason: str | None = None

    @property
    def is_known(self) -> bool:
        return self.resolved is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "resolved": self.resolved,
            "settled_value": self.settled_value,
            "source": self.source,
            "near_strike": self.near_strike,
            "distance_to_strike_pct": (
                round(self.distance_to_strike_pct, 6)
                if self.distance_to_strike_pct is not None else None
            ),
            "reason": self.reason,
        }


class CandleSettlement:
    """Settles claims from historical candles.

    A proxy for the venue's index, with the limitation stated in the module
    docstring and surfaced per-settlement via `near_strike`.
    """

    name = "hyperliquid_candles"

    def __init__(self, data: MarketDataSet) -> None:
        self._data = data

    def value_at(self, symbol: str, when: datetime) -> float | None:
        return self._data.spot_at(symbol, when)


class RecordedStatusSettlement:
    """Settles from the venue's own recorded result — the authoritative source.

    Empty until the recorder has followed contracts through to resolution. It
    exists now so the engine is already written against the real thing, and
    swapping it in later is a constructor argument rather than a rewrite.
    """

    name = "venue_recorded_result"

    def __init__(self, results: dict[str, bool]) -> None:
        self._results = dict(results)

    def value_at(self, symbol: str, when: datetime) -> float | None:  # noqa: ARG002
        return None

    def resolved(self, ticker: str) -> bool | None:
        return self._results.get(ticker)


def settle_claim(
    claim: Claim,
    source: SettlementSource,
    *,
    recorded: RecordedStatusSettlement | None = None,
) -> Settlement:
    """Resolve one claim, preferring the venue's own result when we have it.

    Falls through to the price proxy only when the authoritative result is
    absent, and always says which one was used.
    """
    if recorded is not None:
        known = recorded.resolved(claim.ticker)
        if known is not None:
            return Settlement(
                ticker=claim.ticker, resolved=known, settled_value=None,
                source=recorded.name, near_strike=False, distance_to_strike_pct=None,
            )

    value = source.value_at(claim.underlying, claim.resolution_time)
    if value is None:
        return Settlement(
            ticker=claim.ticker, resolved=None, settled_value=None, source=source.name,
            near_strike=False, distance_to_strike_pct=None,
            reason=(
                f"no {claim.underlying} price available at "
                f"{claim.resolution_time.isoformat()}; the claim is unsettled and "
                "must be excluded from results, not counted as a loss"
            ),
        )

    resolved = claim.resolves_yes(Decimal(str(value)))

    # How close the outcome came to flipping. Measured against the nearer bound
    # for a range claim, since either edge can flip it.
    bounds = [b for b in (claim.threshold, claim.lower_bound, claim.upper_bound) if b is not None]
    distance = min(abs(value - float(b)) / value for b in bounds) if bounds and value else None
    near = distance is not None and distance <= NEAR_STRIKE_FRACTION
    if near:
        log.info(
            "%s settled %.4f%% from its strike — close enough that a different "
            "settlement index could have resolved it the other way",
            claim.ticker, (distance or 0) * 100,
        )

    return Settlement(
        ticker=claim.ticker,
        resolved=resolved,
        settled_value=value,
        source=source.name,
        near_strike=near,
        distance_to_strike_pct=distance,
    )


@dataclass(frozen=True)
class SettlementReport:
    """Aggregate settlement quality. Read before believing any win rate."""

    settled: int
    unsettled: int
    near_strike: int
    by_source: dict[str, int]

    @property
    def proxy_risk_pct(self) -> float:
        return (self.near_strike / self.settled * 100.0) if self.settled else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "settled": self.settled,
            "unsettled": self.unsettled,
            "near_strike": self.near_strike,
            "proxy_risk_pct": round(self.proxy_risk_pct, 2),
            "by_source": dict(sorted(self.by_source.items())),
            "note": (
                "near_strike counts settlements decided by a margin small enough "
                "that the venue's own index could have resolved them the other "
                "way. A high share means the win rate is not trustworthy."
            ),
        }


def summarise_settlements(settlements: list[Settlement]) -> SettlementReport:
    by_source: dict[str, int] = {}
    settled = unsettled = near = 0
    for s in settlements:
        if s.is_known:
            settled += 1
            by_source[s.source] = by_source.get(s.source, 0) + 1
            if s.near_strike:
                near += 1
        else:
            unsettled += 1
    return SettlementReport(
        settled=settled, unsettled=unsettled, near_strike=near, by_source=by_source
    )
