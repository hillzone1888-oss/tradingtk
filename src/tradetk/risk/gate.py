"""The decision: admit a candidate, or refuse it with a reason.

Two functions, not one, on purpose. The backtest screens slots and concentration
*before* sizing a candidate — so a full book does not burn sizing work, and the
reason is recorded distinctly — and screens capital *after* sizing, because the
cost is not known until then. Collapsing the two would change which reason
surfaces for a candidate that fails more than one check.

The reasons are open strings, not a closed enum, so the step-15 halt seam can add
`daily_loss_halt` / `drawdown_halt` without breaking any consumer.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tradetk.risk.limits import RiskLimits
from tradetk.risk.state import RiskState


@dataclass(frozen=True)
class RiskDecision:
    admitted: bool
    reason: str | None = None


_ADMIT = RiskDecision(admitted=True, reason=None)


def screen_new_entry(underlying: str, state: RiskState, limits: RiskLimits) -> RiskDecision:
    """Pre-sizing: is there room for one more position in this underlying?"""
    if state.slots_used >= limits.max_positions:
        return RiskDecision(False, "no_free_slot")
    if state.slots_for(underlying) >= limits.max_slots_per_underlying:
        return RiskDecision(False, "underlying_concentration_limit")
    return _ADMIT


def screen_cost(capital_at_risk: Decimal, state: RiskState, limits: RiskLimits) -> RiskDecision:
    """Post-sizing: does this cost fit under the book's capital ceiling?"""
    assert capital_at_risk >= 0, "capital_at_risk must be non-negative"
    if state.capital_deployed + capital_at_risk > limits.total_capital:
        return RiskDecision(False, "insufficient_capital")
    return _ADMIT
