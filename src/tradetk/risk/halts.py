"""Book-wide capital circuit-breakers, checked once per poll before any entry.

The step-14 seam: `risk/gate.py` kept reasons as open strings precisely so these
halts could be added without breaking a consumer. Pure and stateless, symmetric
to `RiskLimits`/`RiskState`: the caller derives a `BookHealth` snapshot, the gate
only decides. A halt stops *new* risk; it never freezes an open position from
settling — that ordering lives in the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tradetk.risk.gate import RiskDecision

_ADMIT = RiskDecision(admitted=True, reason=None)


@dataclass(frozen=True)
class HaltLimits:
    """The three circuit-breaker thresholds, from `config.risk`."""

    max_daily_loss_dollars: Decimal
    max_total_drawdown_dollars: Decimal
    data_staleness_halt_seconds: Decimal

    @classmethod
    def from_config(cls, config: Any) -> "HaltLimits":
        return cls(
            max_daily_loss_dollars=Decimal(str(config.risk.max_daily_loss_dollars)),
            max_total_drawdown_dollars=Decimal(
                str(config.risk.max_total_drawdown_dollars)
            ),
            data_staleness_halt_seconds=Decimal(
                str(config.risk.data_staleness_halt_seconds)
            ),
        )


@dataclass(frozen=True)
class BookHealth:
    """The halt-relevant snapshot. `realized_today` is negative when losing."""

    realized_today: Decimal
    drawdown: Decimal
    data_age_seconds: Decimal
    drawdown_latched: bool


def screen_halts(health: BookHealth, limits: HaltLimits) -> RiskDecision:
    """Severity order: permanent drawdown, then daily loss, then transient staleness."""
    if health.drawdown_latched or health.drawdown >= limits.max_total_drawdown_dollars:
        return RiskDecision(False, "drawdown_halt")
    if -health.realized_today >= limits.max_daily_loss_dollars:
        return RiskDecision(False, "daily_loss_halt")
    if health.data_age_seconds > limits.data_staleness_halt_seconds:
        return RiskDecision(False, "stale_data_halt")
    return _ADMIT
