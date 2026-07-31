"""``baseline_vol`` — the reference strategy, and the benchmark to beat.

It does the simplest defensible thing: take the underlying's recent realized
volatility, push spot through the lognormal in
:mod:`tradetk.translation.probability`, and call that the fair probability. If
the venue's price differs by more than costs plus margin, the edge gate takes
it.

**This is a baseline, not an alpha.** Its entire content is "the market's
implied probability sometimes differs from a realized-vol estimate." Every other
strategy has to justify itself by beating this on calibration, and any that
cannot is not worth the extra moving parts.

**It is implicitly short volatility, and that matters.** Implied volatility
usually exceeds subsequent realized volatility — the variance risk premium, one
of the most persistent effects in derivatives, and compensation sellers earn for
carrying gap risk. Feeding *realized* vol into the model therefore makes it
systematically judge far-from-spot contracts as overpriced, so its default
posture is selling tails. That is a real risk premium being harvested rather
than a mispricing being found, and the two behave very differently when they go
wrong: the premium pays out steadily and then takes it all back in one move.

``vol_multiplier`` exists to face that directly. At 1.0 the strategy is
unadjusted and maximally short-vol. Above 1.0 it inflates the vol input, which
raises estimated tail probabilities and makes the strategy less willing to sell
them. It is a parameter and not a fitted constant on purpose: fitting it to make
a backtest look good is exactly the overfitting the operating rules forbid, and
the honest way to set it is from the measured implied-vs-realized gap once
calibration data exists.
"""

from __future__ import annotations

from tradetk.enums import Capability
from tradetk.signals.base import assert_fresh, StaleDataError
from tradetk.strategy.base import (
    BaseStrategy,
    StrategyContext,
    StrategyOpinion,
    register_strategy,
)
from tradetk.translation.claims import Claim
from tradetk.translation.probability import (
    ProbabilityError,
    estimate_claim_probability,
)

#: Below this many return observations the vol estimate is mostly noise. A
#: 30-day 1h lookback yields ~720, so tripping this means the data is broken,
#: not merely thin — which is a reason to abstain, not to widen the window.
DEFAULT_MIN_VOL_SAMPLES = 30

#: A snapshot older than this is not evidence about the current price.
DEFAULT_MAX_SNAPSHOT_AGE_S = 300.0


@register_strategy
class BaselineVolStrategy(BaseStrategy):
    """Realized-vol lognormal fair value versus the venue's price."""

    name = "baseline_vol"
    description = (
        "Lognormal fair probability from realized volatility. The benchmark "
        "every other strategy must beat on calibration."
    )

    def __init__(
        self,
        *,
        vol_multiplier: float = 1.0,
        min_vol_samples: int = DEFAULT_MIN_VOL_SAMPLES,
        max_snapshot_age_seconds: float = DEFAULT_MAX_SNAPSHOT_AGE_S,
        drift_annual: float = 0.0,
        **params: object,
    ) -> None:
        if vol_multiplier <= 0:
            raise ValueError(f"vol_multiplier must be positive, got {vol_multiplier}")
        super().__init__(
            vol_multiplier=vol_multiplier,
            min_vol_samples=min_vol_samples,
            max_snapshot_age_seconds=max_snapshot_age_seconds,
            drift_annual=drift_annual,
            **params,
        )
        self.vol_multiplier = vol_multiplier
        self.min_vol_samples = min_vol_samples
        self.max_snapshot_age_seconds = max_snapshot_age_seconds
        self.drift_annual = drift_annual

    def required_capabilities(self) -> set[Capability]:
        return {Capability.SPOT_PRICE, Capability.CANDLES, Capability.REALIZED_VOL}

    def estimate(self, claim: Claim, context: StrategyContext) -> StrategyOpinion:
        snap = context.snapshot

        # The snapshot must actually be about this claim's underlying. Silently
        # pricing a BTC claim off ETH vol is the kind of wiring error that
        # produces confident, wrong numbers indefinitely.
        if snap.symbol.upper() != claim.underlying.upper():
            return self.abstain(
                claim,
                f"snapshot is for {snap.symbol}, claim is on {claim.underlying}",
            )

        try:
            assert_fresh(snap.as_of, self.max_snapshot_age_seconds, now=context.now)
        except StaleDataError as exc:
            return self.abstain(claim, f"stale market data: {exc}")

        if snap.n_vol_samples < self.min_vol_samples:
            return self.abstain(
                claim,
                f"volatility estimated from {snap.n_vol_samples} samples, below the "
                f"minimum of {self.min_vol_samples}",
            )

        if snap.sigma_annual <= 0:
            return self.abstain(
                claim, f"non-positive volatility ({snap.sigma_annual}) is not a usable input"
            )

        if snap.spot <= 0:
            return self.abstain(claim, f"non-positive spot ({snap.spot})")

        sigma = snap.sigma_annual * self.vol_multiplier
        try:
            estimate = estimate_claim_probability(
                claim,
                spot=snap.spot,
                sigma_annual=sigma,
                now=context.now,
                drift_annual=self.drift_annual,
                method=(
                    "baseline_vol/lognormal"
                    if self.vol_multiplier == 1.0
                    else f"baseline_vol/lognormal(vol x{self.vol_multiplier})"
                ),
            )
        except ProbabilityError as exc:
            return self.abstain(claim, str(exc))

        return self.opine(estimate)
