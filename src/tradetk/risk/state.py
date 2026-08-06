"""A snapshot of the open book, from the risk point of view only.

`RiskState` deliberately knows nothing about settlement, PnL, resolution time,
or strikes. Each consumer builds one from its own storage — the backtest from an
in-memory dict, a future executor from a persisted file — so the same decision
functions serve both without either having to share a book format.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class OpenRisk:
    """One open position, reduced to what a risk decision needs."""

    ticker: str
    underlying: str
    capital_at_risk: Decimal


@dataclass(frozen=True)
class RiskState:
    open: tuple[OpenRisk, ...] = ()

    @property
    def slots_used(self) -> int:
        return len(self.open)

    def slots_for(self, underlying: str) -> int:
        return sum(1 for o in self.open if o.underlying == underlying)

    @property
    def capital_deployed(self) -> Decimal:
        return sum((o.capital_at_risk for o in self.open), Decimal(0))
