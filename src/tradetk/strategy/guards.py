"""The preconditions every strategy shares, in one place.

Each of these is a way a strategy can produce a confident number that is
meaningless — the wrong asset's volatility, a stale price, a vol estimate built
from five samples. They were written once inside the baseline strategy; they are
here because the second strategy needs exactly the same set, and a guard that is
copy-pasted is a guard that eventually drifts between strategies and makes their
calibration scores incomparable.

Returns a *reason string* rather than raising, because the caller's response is
always an abstention with that reason attached, never an exception.
"""

from __future__ import annotations

from tradetk.signals.base import StaleDataError, assert_fresh
from tradetk.strategy.base import StrategyContext
from tradetk.translation.claims import Claim


def snapshot_guard(
    claim: Claim,
    context: StrategyContext,
    *,
    min_vol_samples: int,
    max_snapshot_age_seconds: float,
) -> str | None:
    """Return why this snapshot cannot price this claim, or ``None`` if it can."""
    snap = context.snapshot

    # The snapshot must actually be about this claim's underlying. Silently
    # pricing a BTC claim off ETH vol is the kind of wiring error that produces
    # confident, wrong numbers indefinitely.
    if snap.symbol.upper() != claim.underlying.upper():
        return f"snapshot is for {snap.symbol}, claim is on {claim.underlying}"

    try:
        assert_fresh(snap.as_of, max_snapshot_age_seconds, now=context.now)
    except StaleDataError as exc:
        return f"stale market data: {exc}"

    if snap.n_vol_samples < min_vol_samples:
        return (
            f"volatility estimated from {snap.n_vol_samples} samples, below the "
            f"minimum of {min_vol_samples}"
        )

    if snap.sigma_annual <= 0:
        return f"non-positive volatility ({snap.sigma_annual}) is not a usable input"

    if snap.spot <= 0:
        return f"non-positive spot ({snap.spot})"

    return None
