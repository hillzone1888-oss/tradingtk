"""Settle one open paper position against the venue's resolved outcome.

Pure. The same contract-payout math the backtest uses: a held side that wins
pays $1 per contract, a losing side pays nothing, and settlement itself is free
(Kalshi charges on trades, not resolution — the entry fee is already in `cost`).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tradetk.venues.base import VenueMarket

_SETTLED = {"settled", "finalized"}


@dataclass(frozen=True)
class SettleOutcome:
    result: str
    proceeds: Decimal
    realized_pnl: Decimal


def settle_position(
    *, side: str, contracts: int, cost: Decimal, market: VenueMarket
) -> SettleOutcome | None:
    """Return the outcome, or ``None`` when the market has not resolved yet."""
    if market.status not in _SETTLED or not market.result:
        return None
    resolved_yes = market.result == "yes"
    side_won = resolved_yes if side == "yes" else not resolved_yes
    proceeds = Decimal(contracts) if side_won else Decimal(0)
    return SettleOutcome(result=market.result, proceeds=proceeds, realized_pnl=proceeds - cost)
