"""Stage 2 of the translation layer: :class:`Claim` -> probability.

A claim is a proposition; the venue quotes a price for it. To have any opinion
at all we need our own number for ``P(claim resolves YES)``, produced by a
deterministic, testable function rather than by judgment.

The model here is the plain one: **driftless geometric Brownian motion**. Given
spot ``S``, annualised volatility ``sigma`` and time-to-resolution ``t`` years,
log-price at resolution is treated as ``ln(S_T) ~ Normal(ln S, sigma^2 t)``, so

    P(S_T > K) = Phi( ln(S/K) / (sigma * sqrt(t)) )

**Why zero drift.** Over the minutes-to-days horizons this toolkit trades, any
drift estimate is dominated by its own standard error — fitting one would be
fitting noise, and a wrong drift biases *every* claim in the same direction,
which is the worst possible error structure. Zero log-drift keeps the median at
spot, which is the honest agnostic prior. It is exposed as a parameter so the
choice is visible and testable, not buried.

**Where this model is wrong, stated up front.** Crypto returns have fat tails
and clustered volatility; a lognormal has neither. The practical consequence is
one-directional and matters: **the tails are too thin**, so this model
systematically *underestimates* the probability of far-from-spot strikes. Those
are exactly the cheap longshot contracts that look like free money, and exactly
where the fee model shows costs are highest per dollar staked. Estimates in the
deep tail are therefore flagged (see ``DEEP_TAIL_Z``) rather than trusted.

Nothing here is validated by construction. Step 10's calibration — reliability
diagram and Brier score over resolved contracts — is what says whether these
numbers mean anything, and it is the only thing that can.
"""

from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tradetk.translation.claims import Claim, ClaimOperator

# Probabilities are reported to 6 dp: far beyond the precision the model
# deserves, but exact as a Decimal so every downstream comparison against a
# Decimal price stays in one numeric system.
PROB_QUANTUM = Decimal("0.000001")

HOURS_PER_YEAR = 365.0 * 24.0

# |z| beyond this is "deep tail": the lognormal's thin tails make the estimate
# unreliable in a known direction, so it is flagged rather than quietly used.
DEEP_TAIL_Z = 3.0

# Below this, time-scaling amplifies any vol error enormously and microstructure
# dominates the diffusion. Flagged, not refused — short-dated is the point.
SHORT_HORIZON_HOURS = 0.25


class ProbabilityError(Exception):
    """The estimate could not be produced. Never returns a default."""


def normal_cdf(x: float) -> float:
    """Standard normal CDF via ``erf`` — no scipy dependency.

    ``Phi(x) = (1 + erf(x / sqrt(2))) / 2``. Accurate to ~1e-15, which is many
    orders of magnitude better than the model's own error.
    """
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class ProbabilityEstimate(BaseModel):
    """A probability with the full derivation attached.

    Every input that moved the number is kept. A proposal that cannot be
    explained after the fact is not reviewable, and at this size the review is
    the entire point.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    underlying: str
    p: Decimal = Field(ge=0, le=1, description="P(claim resolves YES).")
    method: str
    computed_at: datetime
    spot: float
    sigma_annual: float
    hours_to_resolution: float
    z_score: float | None = Field(
        default=None,
        description="Standardised log-distance to the strike; None for `between`.",
    )
    inputs: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    @property
    def is_deep_tail(self) -> bool:
        return self.z_score is not None and abs(self.z_score) > DEEP_TAIL_Z

    @property
    def p_float(self) -> float:
        return float(self.p)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "underlying": self.underlying,
            "p": str(self.p),
            "method": self.method,
            "computed_at": self.computed_at.isoformat(),
            "spot": self.spot,
            "sigma_annual": self.sigma_annual,
            "hours_to_resolution": round(self.hours_to_resolution, 4),
            "z_score": round(self.z_score, 4) if self.z_score is not None else None,
            "deep_tail": self.is_deep_tail,
            "inputs": self.inputs,
            "warnings": self.warnings,
        }


def _quantise(p: float) -> Decimal:
    """Clamp to [0, 1] and quantise. Clamping guards float error at the edges,
    not model error — a p of exactly 0 or 1 still means "the model is out of its
    depth", which the deep-tail flag is what actually reports."""
    if not math.isfinite(p):
        raise ProbabilityError(f"model produced a non-finite probability ({p})")
    p = min(1.0, max(0.0, p))
    return Decimal(str(p)).quantize(PROB_QUANTUM)


def prob_above(spot: float, strike: float, sigma_annual: float, years: float,
               *, drift_annual: float = 0.0) -> tuple[float, float]:
    """``P(S_T > K)`` under lognormal diffusion. Returns ``(probability, z)``.

    ``drift_annual`` is drift **in log space** (0.0 = median stays at spot). It
    exists so the assumption is a visible parameter rather than an invisible
    hardcoded zero; see the module docstring for why the default is 0.
    """
    if spot <= 0:
        raise ProbabilityError(f"spot must be positive, got {spot}")
    if strike <= 0:
        raise ProbabilityError(f"strike must be positive, got {strike}")
    if sigma_annual <= 0:
        raise ProbabilityError(
            f"volatility must be positive, got {sigma_annual}; a zero-vol model "
            "would return a degenerate 0/1 and is never a safe default"
        )
    if years <= 0:
        raise ProbabilityError(
            f"time to resolution must be positive, got {years} years"
        )

    sigma_t = sigma_annual * math.sqrt(years)
    z = (math.log(spot / strike) + drift_annual * years) / sigma_t
    return normal_cdf(z), z


def estimate_claim_probability(
    claim: Claim,
    *,
    spot: float,
    sigma_annual: float,
    now: datetime,
    drift_annual: float = 0.0,
    method: str = "lognormal_driftless",
) -> ProbabilityEstimate:
    """Price one claim's YES probability from spot and realized vol.

    Handles every :class:`ClaimOperator`. Note that ``above`` and
    ``at_or_above`` are identical here: the diffusion is continuous, so
    ``P(S_T == K) == 0`` and the boundary carries no mass. That is a property of
    the model, not a shortcut — a discrete settlement grid would break it, and
    the settlement sources in use quote to far more precision than the strikes.
    """
    hours = claim.hours_to_resolution(now)
    if hours <= 0:
        raise ProbabilityError(
            f"{claim.ticker} resolves at {claim.resolution_time.isoformat()}, "
            f"which is {abs(hours):.2f}h in the past — nothing to estimate"
        )
    years = hours / HOURS_PER_YEAR

    warnings: list[str] = []
    if hours < SHORT_HORIZON_HOURS:
        warnings.append(
            f"very short horizon ({hours * 60:.1f} min): diffusion is not the "
            "dominant term at this scale and microstructure is not modelled"
        )
    if claim.reference_is_measured:
        warnings.append(
            "threshold is a measured reference, so this claim is ~50/50 by "
            "construction — do not pool it with round-number strikes when "
            "calibrating"
        )

    z: float | None = None
    if claim.operator is ClaimOperator.between:
        lower = float(claim.lower_bound)  # type: ignore[arg-type]
        upper = float(claim.upper_bound)  # type: ignore[arg-type]
        p_above_lower, z_lower = prob_above(
            spot, lower, sigma_annual, years, drift_annual=drift_annual
        )
        p_above_upper, z_upper = prob_above(
            spot, upper, sigma_annual, years, drift_annual=drift_annual
        )
        p = p_above_lower - p_above_upper
        # A range claim has two distances; the nearer one governs whether the
        # estimate sits in the untrustworthy tail.
        if min(abs(z_lower), abs(z_upper)) > DEEP_TAIL_Z:
            warnings.append(
                "both bounds are deep in the tail; the lognormal understates "
                "far-strike probability and this estimate is unreliable"
            )
        detail: dict[str, Any] = {
            "lower_bound": str(claim.lower_bound),
            "upper_bound": str(claim.upper_bound),
            "z_lower": round(z_lower, 4),
            "z_upper": round(z_upper, 4),
            "p_above_lower": round(p_above_lower, 6),
            "p_above_upper": round(p_above_upper, 6),
        }
    else:
        strike = float(claim.threshold)  # type: ignore[arg-type]
        p_above, z = prob_above(
            spot, strike, sigma_annual, years, drift_annual=drift_annual
        )
        p = p_above if claim.operator is not ClaimOperator.below else 1.0 - p_above
        if abs(z) > DEEP_TAIL_Z:
            warnings.append(
                f"strike is {abs(z):.1f} sigma from spot; the lognormal's thin "
                "tails understate this probability, and cheap far strikes are "
                "also the most fee-expensive per dollar staked"
            )
        detail = {
            "threshold": str(claim.threshold),
            "operator": claim.operator.value,
            "p_above_threshold": round(p_above, 6),
            "boundary_note": (
                "above and at_or_above coincide: a continuous diffusion puts no "
                "mass exactly on the strike"
            ),
        }

    return ProbabilityEstimate(
        ticker=claim.ticker,
        underlying=claim.underlying,
        p=_quantise(p),
        method=method,
        computed_at=now,
        spot=spot,
        sigma_annual=sigma_annual,
        hours_to_resolution=hours,
        z_score=z,
        inputs={
            "drift_annual": drift_annual,
            "years_to_resolution": round(years, 8),
            "sigma_over_horizon": round(sigma_annual * math.sqrt(years), 6),
            **detail,
        },
        warnings=warnings,
    )
