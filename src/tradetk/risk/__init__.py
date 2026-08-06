"""Book-level risk: the shared gate the backtest and the executor both consult.

Pure and stateless. The caller owns its book and derives a `RiskState` snapshot;
the gate only decides. See docs/superpowers/specs/2026-08-05-risk-module-design.md.
"""

from tradetk.risk.gate import RiskDecision, screen_cost, screen_new_entry
from tradetk.risk.limits import RiskLimits
from tradetk.risk.state import OpenRisk, RiskState

__all__ = [
    "RiskLimits",
    "RiskState",
    "OpenRisk",
    "RiskDecision",
    "screen_new_entry",
    "screen_cost",
]
