"""``liquidation_skew`` — the baseline's lognormal, tilted by forced order flow.

The whole content of this strategy is one claim: **when leveraged positions are
being force-closed in one direction, the next few hours are not symmetric.**
Longs liquidated are longs force-*sold*; shorts liquidated are shorts
force-*bought*. That flow is price-insensitive and clustered, so a window in
which it is overwhelmingly one-sided is a window in which the driftless
lognormal — whose median sits exactly at spot by construction — is asserting a
symmetry the order book does not have.

**It is the baseline plus one term, on purpose.** The probability still comes
from :func:`estimate_claim_probability`; the only change is a bounded log-drift.
That is what makes the pair comparable: step 10 scores both strategies on the
same contracts under the same costs, so any difference in Brier score is
attributable to the drift term and to nothing else. A strategy that also
switched vol model, or horizon, or universe would answer no question at all.

**The size of the tilt is capped, not fitted.** ``max_drift_sigma`` is the shift
applied at a *perfectly* one-sided window (imbalance = ±1), measured in standard
deviations of the claim's own horizon; the applied shift is that cap times the
imbalance, so the map is linear, monotone, and bounded by construction. The
default of 0.25σ is a deliberately timid prior, chosen before any backtest and
documented here so that changing it later is visibly a decision rather than a
result. Fitting it to make a backtest look good is the overfitting the operating
rules forbid — and on a signal this noisy it would fit almost perfectly.

**The sign is a hypothesis, and that is the honest part.** Forced flow has two
well-attested and opposite readings: *continuation* (a cascade begets a cascade —
liquidations push price into the next tranche of liquidation levels) and
*reversion* (the liquidation wick — forced flow overshoots and price snaps back
once it is exhausted). Which dominates depends on horizon and on how exhausted
the leverage already is, and asserting one from an armchair would be the exact
"hardcoded judgment call" the operating rules forbid. So it is ``regime``, an
explicit parameter that is recorded in the method string and in the estimate's
inputs, and the calibration harness is what adjudicates it.

⚠️ **Flipping ``regime`` after seeing results on the same tape is fitting**, and
a two-way choice made post-hoc on one sample is worth roughly nothing. Declare
it before the run; if both are scored, both must be reported.

**It abstains far more than the baseline, by design.** No liquidation data, a
thin window, a window whose imbalance rests on one whale, or a claim resolving
too far out to be about this flow at all — each is an abstention, not a fallback
to the baseline. Falling back would make the strategy silently *be* the baseline
on most markets, and its calibration score would then be the baseline's score
wearing a different name.
"""

from __future__ import annotations

import math
from typing import Any

from tradetk.enums import Capability
from tradetk.signals.base import StaleDataError, assert_fresh
from tradetk.signals.liquidations import DEFAULT_WINDOW_MINUTES, LiquidationProfile
from tradetk.strategy.base import (
    BaseStrategy,
    StrategyContext,
    StrategyOpinion,
    register_strategy,
)
from tradetk.strategy.guards import snapshot_guard
from tradetk.translation.claims import Claim
from tradetk.translation.probability import (
    HOURS_PER_YEAR,
    ProbabilityError,
    estimate_claim_probability,
)

#: Where the strategy looks for its signal on :attr:`MarketSnapshot.extras`.
#: A constant so the producer and the consumer cannot disagree by typo.
PROFILE_KEY = "liquidation_profile"

#: Regimes. See the module docstring — this is a hypothesis under test, not a
#: setting to be tuned.
REGIME_CONTINUATION = "continuation"
REGIME_REVERSION = "reversion"
REGIMES = (REGIME_CONTINUATION, REGIME_REVERSION)

DEFAULT_MIN_VOL_SAMPLES = 30
DEFAULT_MAX_SNAPSHOT_AGE_S = 300.0

#: A profile older than this describes a different market. Tighter than the
#: snapshot limit because the whole thesis is that this signal decays fast.
DEFAULT_MAX_PROFILE_AGE_S = 180.0

#: Evidence gates. Both are stated priors about what counts as a signal rather
#: than measurements, and both are per-underlying quantities that an operator
#: should expect to set: $250k of forced flow is a busy hour in one asset and a
#: rounding error in another.
DEFAULT_MIN_EVENTS = 10
DEFAULT_MIN_NOTIONAL_USD = 250_000.0

#: Above this share in a single liquidation, the window is one account's margin
#: call rather than a cascade, and the imbalance is not evidence about flow.
DEFAULT_MAX_CONCENTRATION = 0.5

#: Beyond this horizon the claim is not about this flow. Forced-flow effects are
#: measured in minutes to hours; applying them to a week-out contract would be
#: asserting the tilt persists long after the leverage that caused it is gone.
DEFAULT_MAX_HORIZON_HOURS = 24.0

#: Shift applied at a perfectly one-sided window, in horizon standard deviations.
DEFAULT_MAX_DRIFT_SIGMA = 0.25

#: Multiplier on the vol input. 1.0 = off. Cascades do raise realized vol, but
#: by how much is an empirical question this project has not answered yet, so
#: the honest default is to make no claim.
DEFAULT_VOL_BUMP = 1.0


@register_strategy
class LiquidationSkewStrategy(BaseStrategy):
    """Lognormal fair value with a bounded drift from one-sided forced flow."""

    name = "liquidation_skew"
    description = (
        "Baseline lognormal plus a capped log-drift set by the imbalance of "
        "recent forced liquidations. Abstains without liquidation data."
    )

    def __init__(
        self,
        *,
        regime: str = REGIME_CONTINUATION,
        window_minutes: int = DEFAULT_WINDOW_MINUTES,
        max_drift_sigma: float = DEFAULT_MAX_DRIFT_SIGMA,
        vol_bump: float = DEFAULT_VOL_BUMP,
        min_events: int = DEFAULT_MIN_EVENTS,
        min_notional_usd: float = DEFAULT_MIN_NOTIONAL_USD,
        max_concentration: float = DEFAULT_MAX_CONCENTRATION,
        max_horizon_hours: float = DEFAULT_MAX_HORIZON_HOURS,
        min_vol_samples: int = DEFAULT_MIN_VOL_SAMPLES,
        max_snapshot_age_seconds: float = DEFAULT_MAX_SNAPSHOT_AGE_S,
        max_profile_age_seconds: float = DEFAULT_MAX_PROFILE_AGE_S,
        **params: object,
    ) -> None:
        if regime not in REGIMES:
            raise ValueError(
                f"regime must be one of {', '.join(REGIMES)}; got {regime!r}. "
                "There is no default-by-guess here: the sign of this signal is "
                "the thing being tested."
            )
        if max_drift_sigma < 0:
            raise ValueError(f"max_drift_sigma must be non-negative, got {max_drift_sigma}")
        if vol_bump <= 0:
            raise ValueError(f"vol_bump must be positive, got {vol_bump}")
        if not 0 < max_concentration <= 1:
            raise ValueError(
                f"max_concentration must be in (0, 1], got {max_concentration}"
            )
        super().__init__(
            regime=regime,
            window_minutes=window_minutes,
            max_drift_sigma=max_drift_sigma,
            vol_bump=vol_bump,
            min_events=min_events,
            min_notional_usd=min_notional_usd,
            max_concentration=max_concentration,
            max_horizon_hours=max_horizon_hours,
            min_vol_samples=min_vol_samples,
            max_snapshot_age_seconds=max_snapshot_age_seconds,
            max_profile_age_seconds=max_profile_age_seconds,
            **params,
        )
        self.regime = regime
        self.window_minutes = window_minutes
        self.max_drift_sigma = max_drift_sigma
        self.vol_bump = vol_bump
        self.min_events = min_events
        self.min_notional_usd = min_notional_usd
        self.max_concentration = max_concentration
        self.max_horizon_hours = max_horizon_hours
        self.min_vol_samples = min_vol_samples
        self.max_snapshot_age_seconds = max_snapshot_age_seconds
        self.max_profile_age_seconds = max_profile_age_seconds

    def required_capabilities(self) -> set[Capability]:
        # LIQUIDATIONS is the point of this strategy. No provider advertises it
        # yet, so selecting this strategy halts at startup — which is the
        # correct outcome, and far better than a silent fallback.
        return {
            Capability.SPOT_PRICE,
            Capability.CANDLES,
            Capability.REALIZED_VOL,
            Capability.LIQUIDATIONS,
        }

    # ── the signal ────────────────────────────────────────────────

    def _profile_reason(
        self, profile: object, claim: Claim, context: StrategyContext
    ) -> str | None:
        """Why this profile is not usable evidence, or ``None`` if it is."""
        if profile is None:
            return (
                f"no {PROFILE_KEY} in snapshot extras; this strategy has no opinion "
                "without forced-flow data and does not fall back to the baseline"
            )
        if not isinstance(profile, LiquidationProfile):
            return (
                f"{PROFILE_KEY} is {type(profile).__name__}, not LiquidationProfile; "
                "refusing to interpret an untyped liquidation payload"
            )
        if profile.symbol.upper() != claim.underlying.upper():
            return (
                f"liquidation profile is for {profile.symbol}, claim is on "
                f"{claim.underlying}"
            )
        if profile.window_minutes != self.window_minutes:
            return (
                f"profile covers {profile.window_minutes}min but the strategy is "
                f"configured for {self.window_minutes}min; the gates below are "
                "calibrated to the window and do not transfer"
            )
        try:
            assert_fresh(profile.as_of, self.max_profile_age_seconds, now=context.now)
        except StaleDataError as exc:
            return f"stale liquidation profile: {exc}"
        if profile.n_events < self.min_events:
            return (
                f"{profile.n_events} liquidations in the window, below the minimum "
                f"of {self.min_events}"
            )
        if profile.total_notional_usd < self.min_notional_usd:
            return (
                f"${profile.total_notional_usd:,.0f} of forced flow in the window, "
                f"below the minimum of ${self.min_notional_usd:,.0f}"
            )
        if profile.concentration > self.max_concentration:
            return (
                f"{profile.concentration:.0%} of the window's notional is one "
                f"liquidation (limit {self.max_concentration:.0%}); that is a margin "
                "call, not a cascade"
            )
        return None

    def _shift_sigma(self, profile: LiquidationProfile) -> float:
        """Signed tilt in horizon standard deviations. Bounded by construction."""
        signed = profile.imbalance
        if self.regime == REGIME_REVERSION:
            signed = -signed
        return self.max_drift_sigma * signed

    def estimate(self, claim: Claim, context: StrategyContext) -> StrategyOpinion:
        snap = context.snapshot

        reason = snapshot_guard(
            claim,
            context,
            min_vol_samples=self.min_vol_samples,
            max_snapshot_age_seconds=self.max_snapshot_age_seconds,
        )
        if reason is not None:
            return self.abstain(claim, reason)

        profile = snap.extras.get(PROFILE_KEY)
        reason = self._profile_reason(profile, claim, context)
        if reason is not None:
            return self.abstain(claim, reason)
        assert isinstance(profile, LiquidationProfile)  # narrowed by _profile_reason

        hours = claim.hours_to_resolution(context.now)
        if hours > self.max_horizon_hours:
            return self.abstain(
                claim,
                f"resolves in {hours:.1f}h, beyond the {self.max_horizon_hours:.0f}h "
                "horizon over which forced flow is claimed to say anything",
            )

        shift_sigma = self._shift_sigma(profile)
        sigma = snap.sigma_annual * self.vol_bump

        # z is (log(S/K) + mu*t) / (sigma*sqrt(t)), so a shift of `k` standard
        # deviations of the horizon needs mu = k * sigma / sqrt(t). Expressing
        # the tilt in horizon sigmas rather than in annualised drift is what
        # keeps it comparable across a 2-hour and a 20-hour contract.
        years = max(hours, 0.0) / HOURS_PER_YEAR
        drift_annual = 0.0
        if years > 0 and shift_sigma != 0.0:
            drift_annual = shift_sigma * sigma / math.sqrt(years)

        try:
            estimate = estimate_claim_probability(
                claim,
                spot=snap.spot,
                sigma_annual=sigma,
                now=context.now,
                drift_annual=drift_annual,
                method=f"liquidation_skew/lognormal({self.regime}, {shift_sigma:+.3f}sigma)",
            )
        except ProbabilityError as exc:
            return self.abstain(claim, str(exc))

        detail: dict[str, Any] = {
            "regime": self.regime,
            "shift_sigma": round(shift_sigma, 6),
            "max_drift_sigma": self.max_drift_sigma,
            "vol_bump": self.vol_bump,
            "sigma_before_bump": snap.sigma_annual,
            "liquidations": profile.as_dict(),
        }
        warnings = list(estimate.warnings)
        if self.max_drift_sigma > 0 and abs(shift_sigma) >= self.max_drift_sigma * 0.999:
            warnings.append(
                "forced flow was one-sided enough to hit the drift cap; the tilt "
                "here is the cap's value, not a measurement"
            )
        if self.vol_bump != 1.0:
            warnings.append(
                f"volatility input was scaled by {self.vol_bump}x, so this estimate "
                "is not comparable to an unscaled one"
            )
        # Frozen model: rebuild rather than mutate, so the full derivation
        # travels with the estimate into the proposal and the shadow log.
        estimate = estimate.model_copy(
            update={"inputs": {**estimate.inputs, **detail}, "warnings": warnings}
        )
        return self.opine(estimate)
