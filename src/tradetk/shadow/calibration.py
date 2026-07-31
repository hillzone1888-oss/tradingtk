"""Scoring forecasts: reliability, resolution, and the only benchmark that counts.

**The question this module exists to answer is not "is the model good".** It is
*"is the model better than the price we would have to pay to disagree with it"*.
A model can be beautifully calibrated and still worthless: if Kalshi's mid is a
better forecast than ours, every trade we take is paying a spread to be more
wrong. So every score here is computed twice — once for the model, once for the
market's own mid — and the comparison is the headline.

**Brier score** is mean squared error of a probability forecast, lower better.
Two reference points make it readable:

* ``0.25`` — always saying 50%. A model that cannot beat this carries no
  information at all.
* the market's Brier — the number that decides whether there is an edge.

**Murphy's decomposition** splits it into three parts that fail differently:

    Brier = Reliability - Resolution + Uncertainty

* **Reliability** (lower better): when we say 30%, does it happen 30% of the
  time? A miscalibrated but informative model is *fixable* — the probabilities
  can be remapped.
* **Resolution** (higher better): do our forecasts vary in a way that tracks
  outcomes? Zero resolution means we always say the base rate, which is
  perfectly calibrated and completely useless.
* **Uncertainty**: the base rate's own variance. A property of the contracts,
  not of us — it is why a universe of 90%-likely contracts produces flattering
  Brier scores that mean nothing.

Separating them matters because the fixes are opposite. Bad reliability, good
resolution means recalibrate. Good reliability, no resolution means the model
has no signal and recalibration cannot create one.

**Segmentation is not optional.** The measured-reference series ("up in the next
15 minutes") set their strike *at spot by construction*, so they are ~50/50 by
design and pooling them with round-number strikes hides both. They are reported
separately, always.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from tradetk.backtest.settlement import (
    RecordedStatusSettlement,
    SettlementSource,
    settle_claim,
)
from tradetk.shadow.records import ShadowRecord

log = logging.getLogger("tradetk.shadow.calibration")

#: Below this, a reliability diagram is decoration. From the operating rules.
MIN_SCORED = 200

DEFAULT_HORIZON_EDGES = (1.0, 6.0, 24.0, 72.0)


@dataclass(frozen=True)
class ScoredForecast:
    """One shadow record joined to what actually happened."""

    record: ShadowRecord
    outcome: bool
    near_strike: bool

    @property
    def p_model(self) -> float:
        return float(self.record.p)

    @property
    def p_market(self) -> float | None:
        mid = self.record.market_mid
        return float(mid) if mid is not None else None


@dataclass(frozen=True)
class Bucket:
    lower: float
    upper: float
    n: int
    mean_predicted: float
    observed_frequency: float

    @property
    def gap(self) -> float:
        return self.observed_frequency - self.mean_predicted

    def as_dict(self) -> dict[str, Any]:
        return {
            "bucket": f"{self.lower:.1f}-{self.upper:.1f}",
            "lower": self.lower,
            "upper": self.upper,
            "n": self.n,
            "mean_predicted": round(self.mean_predicted, 4),
            "observed_frequency": round(self.observed_frequency, 4),
            "gap": round(self.gap, 4),
        }


@dataclass(frozen=True)
class Score:
    """A forecaster's performance on one population."""

    label: str
    n: int
    brier: float | None
    reliability: float | None
    resolution: float | None
    uncertainty: float | None
    base_rate: float | None
    buckets: list[Bucket] = field(default_factory=list)

    @property
    def beats_coin_flip(self) -> bool | None:
        return None if self.brier is None else self.brier < 0.25

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "n": self.n,
            "brier": round(self.brier, 5) if self.brier is not None else None,
            "reliability": (
                round(self.reliability, 5) if self.reliability is not None else None
            ),
            "resolution": (
                round(self.resolution, 5) if self.resolution is not None else None
            ),
            "uncertainty": (
                round(self.uncertainty, 5) if self.uncertainty is not None else None
            ),
            "base_rate": round(self.base_rate, 4) if self.base_rate is not None else None,
            "beats_coin_flip": self.beats_coin_flip,
            "buckets": [b.as_dict() for b in self.buckets],
        }


def _buckets(pairs: Sequence[tuple[float, bool]], bins: int) -> list[Bucket]:
    out: list[Bucket] = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        inside = [
            (p, o) for p, o in pairs
            if (lo <= p < hi) or (i == bins - 1 and p == 1.0)
        ]
        if not inside:
            continue
        out.append(
            Bucket(
                lower=lo, upper=hi, n=len(inside),
                mean_predicted=sum(p for p, _ in inside) / len(inside),
                observed_frequency=sum(1 for _, o in inside if o) / len(inside),
            )
        )
    return out


def score(
    pairs: Sequence[tuple[float, bool]], *, label: str, bins: int = 10
) -> Score:
    """Brier plus Murphy's decomposition over ``(probability, outcome)`` pairs.

    The decomposition is computed from the same bins the reliability diagram
    draws, so the chart and the numbers can never tell different stories.

    **The decomposition is approximate, and unavoidably so.** Murphy's identity
    ``Brier = reliability - resolution + uncertainty`` is exact only when
    forecasts are grouped by *identical* value. These forecasts are continuous,
    so exact grouping puts one observation in each group — which makes the
    identity hold trivially and tells you nothing (reliability collapses onto
    Brier and resolution onto uncertainty). Binning is what makes the three
    terms informative, and it drops a small within-bin variance term in
    exchange. So treat the identity as holding to within binning error, not to
    the last decimal, and read the terms as directional rather than exact.
    """
    n = len(pairs)
    if n == 0:
        return Score(label=label, n=0, brier=None, reliability=None,
                     resolution=None, uncertainty=None, base_rate=None)

    base_rate = sum(1 for _, o in pairs if o) / n
    brier = sum((p - (1.0 if o else 0.0)) ** 2 for p, o in pairs) / n
    uncertainty = base_rate * (1.0 - base_rate)

    buckets = _buckets(pairs, bins)
    reliability = sum(
        b.n * (b.mean_predicted - b.observed_frequency) ** 2 for b in buckets
    ) / n
    resolution = sum(
        b.n * (b.observed_frequency - base_rate) ** 2 for b in buckets
    ) / n

    return Score(
        label=label, n=n, brier=brier, reliability=reliability,
        resolution=resolution, uncertainty=uncertainty, base_rate=base_rate,
        buckets=buckets,
    )


def settle_records(
    records: Iterable[ShadowRecord],
    source: SettlementSource,
    *,
    recorded: RecordedStatusSettlement | None = None,
) -> tuple[list[ScoredForecast], dict[str, int]]:
    """Join forecasts to outcomes, dropping the ones that cannot be settled.

    Unsettled records are *excluded*, never defaulted to False. Treating an
    unknown outcome as a loss would bias every score downward by exactly the
    amount of missing data, which is the sort of error that survives review
    because it looks conservative.
    """
    scored: list[ScoredForecast] = []
    dropped: dict[str, int] = {}

    def drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    for record in records:
        outcome = settle_claim(record.to_claim(), source, recorded=recorded)
        if outcome.resolved is None:
            drop("unsettled")
            continue
        scored.append(
            ScoredForecast(
                record=record, outcome=outcome.resolved, near_strike=outcome.near_strike
            )
        )
    return scored, dropped


def _pairs_model(forecasts: Iterable[ScoredForecast]) -> list[tuple[float, bool]]:
    return [(f.p_model, f.outcome) for f in forecasts]


def _pairs_market(forecasts: Iterable[ScoredForecast]) -> list[tuple[float, bool]]:
    return [(f.p_market, f.outcome) for f in forecasts if f.p_market is not None]


@dataclass(frozen=True)
class Comparison:
    """Model against market on the same contracts. The headline result."""

    model: Score
    market: Score
    n_common: int

    @property
    def edge_exists(self) -> bool | None:
        """Whether the model out-forecasts the price it would trade against.

        ``None`` when either side has no data. This is the project's actual
        success criterion — not P&L, which on a $20 book is noise.
        """
        if self.model.brier is None or self.market.brier is None or self.n_common == 0:
            return None
        return self.model.brier < self.market.brier

    @property
    def brier_improvement(self) -> float | None:
        if self.model.brier is None or self.market.brier is None:
            return None
        return self.market.brier - self.model.brier

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self._verdict(),
            "model_beats_market": self.edge_exists,
            "brier_improvement_over_market": (
                round(self.brier_improvement, 5)
                if self.brier_improvement is not None else None
            ),
            "n_contracts_compared": self.n_common,
            "model": self.model.as_dict(),
            "market": self.market.as_dict(),
        }

    def _verdict(self) -> str:
        if self.edge_exists is None:
            return "no comparable data — nothing can be concluded"
        if self.n_common < MIN_SCORED:
            return (
                f"insufficient sample ({self.n_common} < {MIN_SCORED}); the sign of "
                "this comparison is not yet meaningful"
            )
        if self.edge_exists:
            return (
                "model forecasts better than the market mid on this sample — the "
                "necessary condition for edge, though not proof of a tradeable one "
                "once costs are paid"
            )
        return (
            "the market mid forecasts better than the model — on this evidence "
            "every trade pays a spread to be more wrong. Do not trade this."
        )


def compare(forecasts: Sequence[ScoredForecast], *, label: str = "all") -> Comparison:
    """Score model and market on exactly the same contracts.

    Restricted to records that have a market mid, so the two are never scored on
    different populations — a comparison across different samples is not a
    comparison.
    """
    common = [f for f in forecasts if f.p_market is not None]
    return Comparison(
        model=score(_pairs_model(common), label=f"model/{label}"),
        market=score(_pairs_market(common), label=f"market/{label}"),
        n_common=len(common),
    )


def segment(
    forecasts: Sequence[ScoredForecast], key: Callable[[ScoredForecast], str]
) -> dict[str, Comparison]:
    """Split and compare within each segment."""
    groups: dict[str, list[ScoredForecast]] = {}
    for forecast in forecasts:
        groups.setdefault(key(forecast), []).append(forecast)
    return {name: compare(rows, label=name) for name, rows in sorted(groups.items())}


def horizon_label(forecast: ScoredForecast, edges=DEFAULT_HORIZON_EDGES) -> str:
    hours = forecast.record.hours_to_resolution
    previous = 0.0
    for edge in edges:
        if hours < edge:
            return f"{previous:g}-{edge:g}h"
        previous = edge
    return f">{previous:g}h"


@dataclass(frozen=True)
class CalibrationReport:
    """Everything needed to answer 'is it working?' — and to refuse to."""

    overall: Comparison
    independent: Comparison
    by_measured_reference: dict[str, Comparison]
    by_underlying: dict[str, Comparison]
    by_horizon: dict[str, Comparison]
    n_observations: int
    n_contracts: int
    n_near_strike: int
    dropped: dict[str, int]
    strategies: list[str]

    def honesty_warnings(self) -> list[str]:
        warnings: list[str] = []
        if self.n_contracts < MIN_SCORED:
            warnings.append(
                f"{self.n_contracts} independent contracts scored, below the "
                f"~{MIN_SCORED} needed for a reliability diagram to be readable. "
                "Every number here is provisional."
            )
        if self.n_observations > self.n_contracts:
            warnings.append(
                f"{self.n_observations} observations cover only {self.n_contracts} "
                "distinct contracts. Repeated looks at one market are repeated "
                "forecasts of a single outcome, not independent trials — use the "
                "independent view for anything you intend to act on."
            )
        if self.n_near_strike:
            share = self.n_near_strike / max(self.n_observations, 1) * 100
            warnings.append(
                f"{self.n_near_strike} settlements ({share:.0f}%) landed close enough "
                "to the strike that the venue's own index could have resolved them "
                "the other way; those outcomes are proxy-dependent."
            )
        if self.dropped:
            total = sum(self.dropped.values())
            warnings.append(
                f"{total} forecasts could not be settled and were excluded rather "
                "than counted as losses."
            )
        measured = self.by_measured_reference.get("measured_reference")
        if measured is not None and measured.n_common:
            warnings.append(
                f"{measured.n_common} forecasts are on measured-reference markets, "
                "whose strike is set at spot by construction and which are ~50/50 "
                "by design. They are reported separately and must not be pooled."
            )
        return warnings

    def as_dict(self) -> dict[str, Any]:
        return {
            "honesty_warnings": self.honesty_warnings(),
            "headline": self.independent.as_dict(),
            "all_observations": self.overall.as_dict(),
            "counts": {
                "observations": self.n_observations,
                "independent_contracts": self.n_contracts,
                "near_strike_settlements": self.n_near_strike,
                "strategies": self.strategies,
                "dropped": self.dropped,
            },
            "by_measured_reference": {
                k: v.as_dict() for k, v in self.by_measured_reference.items()
            },
            "by_underlying": {k: v.as_dict() for k, v in self.by_underlying.items()},
            "by_horizon": {k: v.as_dict() for k, v in self.by_horizon.items()},
        }


def build_report(
    records: Sequence[ShadowRecord],
    source: SettlementSource,
    *,
    recorded: RecordedStatusSettlement | None = None,
) -> CalibrationReport:
    """Settle, score, segment.

    The headline is the **independent** comparison — one forecast per contract —
    because that is the only view whose sample size means what it says.
    """
    scored, dropped = settle_records(records, source, recorded=recorded)

    # One forecast per contract: the last look before resolution, which is the
    # most informed one and the only view whose n means what it says.
    by_ticker: dict[str, ScoredForecast] = {}
    for forecast in scored:
        current = by_ticker.get(forecast.record.ticker)
        if current is None or forecast.record.observed_at > current.record.observed_at:
            by_ticker[forecast.record.ticker] = forecast
    independent = sorted(by_ticker.values(), key=lambda f: f.record.observed_at)

    return CalibrationReport(
        overall=compare(scored, label="all_observations"),
        independent=compare(independent, label="independent"),
        by_measured_reference=segment(
            scored,
            lambda f: (
                "measured_reference" if f.record.reference_is_measured
                else "fixed_strike"
            ),
        ),
        by_underlying=segment(scored, lambda f: f.record.underlying),
        by_horizon=segment(scored, horizon_label),
        n_observations=len(scored),
        n_contracts=len(independent),
        n_near_strike=sum(1 for f in scored if f.near_strike),
        dropped=dropped,
        strategies=sorted({f.record.strategy for f in scored}),
    )
