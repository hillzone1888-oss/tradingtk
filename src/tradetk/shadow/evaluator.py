"""Scoring the whole eligible universe, without trading any of it.

Six slots against ~2,100 eligible contracts. The evaluator runs the strategy
over every one of them and records the forecast, so evidence accumulates at the
rate the *universe* moves rather than the rate capital recycles.

**Rejected markets are recorded too, and that is the point.** Only scoring the
markets that passed the gates would measure the model on its own selection —
the population you already act on and therefore need evidence about least. The
interesting question is whether the model is right about the 2,094 contracts it
declined, and that question has no answer unless the declines were written down.

**Nothing here touches capital.** No slots, no book, no position limits. Those
gates still run, but only so their verdict can be *recorded* alongside the
forecast; they never stop a record being written.

**Repeated looks at one contract are not independent samples.** The same market
observed at 17:05 and 17:10 is two forecasts of one outcome, and pooling them
as if they were two independent trials inflates apparent sample size — which
narrows confidence intervals that should be wide. The evaluator records every
look because forecast sharpening as resolution approaches is itself worth
measuring, and :mod:`tradetk.shadow.calibration` reports the independent
contract count alongside the observation count so the difference is never
invisible.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tradetk.backtest.marketdata import MarketDataSet
from tradetk.backtest.replay import TapeReplay
from tradetk.costs.fees import KalshiFeeModel
from tradetk.shadow.records import ShadowRecord
from tradetk.strategy.base import BaseStrategy, StrategyContext
from tradetk.translation.claims import UnderlyingRegistry
from tradetk.translation.edge import GateLimits, assess_claim
from tradetk.translation.sizing import SizingLimits, plan_size
from tradetk.venues.base import BinaryBook, Side

log = logging.getLogger("tradetk.shadow.evaluator")


@dataclass(frozen=True)
class ShadowRun:
    """Records produced, plus an account of everything that produced none."""

    records: list[ShadowRecord]
    skipped: dict[str, int]
    tape: dict[str, Any]

    @property
    def gated_in(self) -> int:
        return sum(1 for r in self.records if r.gate_decision == "trade")

    def summary(self) -> dict[str, Any]:
        return {
            "observations_scored": len(self.records),
            "distinct_contracts": len({r.ticker for r in self.records}),
            "would_have_traded": self.gated_in,
            "gated_out": len(self.records) - self.gated_in,
            "skipped": dict(sorted(self.skipped.items(), key=lambda kv: -kv[1])),
            "tape": self.tape,
        }


class ShadowEvaluator:
    """Runs a strategy across every eligible market and records the forecast."""

    def __init__(
        self,
        *,
        strategy: BaseStrategy,
        registry: UnderlyingRegistry,
        data: MarketDataSet,
        fee_model: KalshiFeeModel,
        gate_limits: GateLimits,
        sizing_limits: SizingLimits,
        vol_lookback_days: int = 30,
    ) -> None:
        self.strategy = strategy
        self.registry = registry
        self.data = data
        self.fee_model = fee_model
        self.gate_limits = gate_limits
        self.sizing_limits = sizing_limits
        self.vol_lookback_days = vol_lookback_days

    def _contracts_for(self, book: BinaryBook) -> int:
        """Size a nominal position purely so the gate verdict is realistic.

        The record is a forecast, not a trade, but a gate decision made at a
        size nobody would take is not the decision that would actually have been
        made — so the same sizer runs, on the cheaper side.
        """
        price = book.best_yes_ask or book.best_no_ask
        if price is None:
            return 1
        plan = plan_size(price, self.fee_model, self.sizing_limits)
        return max(1, plan.contracts)

    def run(self, replay: TapeReplay) -> ShadowRun:
        records: list[ShadowRecord] = []
        skipped: Counter[str] = Counter()

        for observation in replay.observations():
            now = observation.observed_at
            claim = replay.claim_as_of(observation.ticker, now, self.registry)
            if claim is None:
                skipped["no_parseable_claim"] += 1
                continue
            if claim.resolution_time <= now:
                skipped["already_resolved"] += 1
                continue

            snapshot = self.data.snapshot_at(
                claim.underlying, now, lookback_days=self.vol_lookback_days
            )
            if snapshot is None:
                skipped["no_underlying_data"] += 1
                continue

            opinion = self.strategy.estimate(
                claim, StrategyContext(now=now, snapshot=snapshot, book=observation.book)
            )
            if opinion.abstained:
                skipped[f"abstained: {opinion.reason}"[:80]] += 1
                continue

            estimate = opinion.estimate
            assessment = assess_claim(
                claim, estimate, observation.book,
                contracts=self._contracts_for(observation.book),
                fee_model=self.fee_model, limits=self.gate_limits, now=now,
            )
            chosen = assessment.chosen
            # When nothing passed, report the near-miss side's failures rather
            # than an empty list: "why not" is the useful half of the record.
            reference = chosen or max(
                (assessment.yes, assessment.no), key=lambda a: a.net_edge_pp
            )

            records.append(
                ShadowRecord(
                    observed_at=now,
                    ticker=claim.ticker,
                    series_ticker=claim.series_ticker,
                    underlying=claim.underlying,
                    strategy=self.strategy.name,
                    method=estimate.method,
                    p=estimate.p,
                    market_mid=observation.book.mid,
                    best_yes_bid=observation.book.best_yes_bid,
                    best_yes_ask=observation.book.best_yes_ask,
                    spread=observation.book.spread,
                    operator=claim.operator,
                    threshold=claim.threshold,
                    lower_bound=claim.lower_bound,
                    upper_bound=claim.upper_bound,
                    resolution_time=claim.resolution_time,
                    hours_to_resolution=claim.hours_to_resolution(now),
                    reference_is_measured=claim.reference_is_measured,
                    resolution_source=claim.resolution_source,
                    rules_primary=claim.rules_primary,
                    z_score=estimate.z_score,
                    deep_tail=estimate.is_deep_tail,
                    spot=snapshot.spot,
                    sigma_annual=snapshot.sigma_annual,
                    gate_decision="trade" if chosen else "reject",
                    chosen_side=chosen.side.value if chosen else None,
                    net_edge_pp=reference.net_edge_pp,
                    failures=tuple(f.gate.value for f in reference.failures),
                )
            )

        return ShadowRun(
            records=records, skipped=dict(skipped), tape=replay.summary()
        )


def market_implied_probability(record: ShadowRecord) -> Decimal | None:
    """The venue's own forecast for the YES side.

    The mid, not a tradeable price — deliberately. This number is used to score
    the *market as a forecaster*, and a forecast is not a transaction. Using the
    ask would conflate "what does the market think" with "what would it cost me
    to disagree", and those are different questions answered in different
    sections of the calibration report.
    """
    return record.market_mid


def side_probability_of(record: ShadowRecord, p_yes: Decimal) -> Decimal:
    """Convert a YES probability to the side the gate actually chose."""
    if record.chosen_side == Side.no.value:
        return Decimal(1) - p_yes
    return p_yes


def observed_at_bucket(record: ShadowRecord, edges: tuple[float, ...]) -> str:
    """Label a record by time-to-resolution, for segmented calibration."""
    hours = record.hours_to_resolution
    previous = 0.0
    for edge in edges:
        if hours < edge:
            return f"{previous:g}-{edge:g}h"
        previous = edge
    return f">{previous:g}h"


def latest_per_contract(records: list[ShadowRecord]) -> list[ShadowRecord]:
    """One record per contract — the last look before resolution.

    The independence-respecting view. Repeated observations of one market are
    repeated forecasts of a single outcome, so treating them as separate trials
    understates uncertainty.
    """
    best: dict[str, ShadowRecord] = {}
    for record in records:
        current = best.get(record.ticker)
        if current is None or record.observed_at > current.observed_at:
            best[record.ticker] = record
    return sorted(best.values(), key=lambda r: r.observed_at)
