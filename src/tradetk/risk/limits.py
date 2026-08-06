"""The portfolio caps: how many slots, how concentrated, how much capital.

`total_capital` is intentionally duplicated with `SizingLimits` rather than
moved: the sizer needs it too (a position is capped against remaining capital).
Both dataclasses read the same `config.capital.total_capital`, so they cannot
disagree — the single source of truth is the config field, not either struct.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class RiskLimits:
    """Book-level caps. Values come from config and are validated there."""

    max_positions: int
    max_slots_per_underlying: int
    total_capital: Decimal

    @classmethod
    def from_config(cls, config: Any) -> "RiskLimits":
        return cls(
            max_positions=int(config.capital.max_positions),
            max_slots_per_underlying=int(config.capital.max_slots_per_underlying),
            total_capital=Decimal(str(config.capital.total_capital)),
        )
