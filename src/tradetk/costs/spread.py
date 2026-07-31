"""Spread and book-walking slippage, expressed in probability points.

Fees are only part of the cost. In a thin book the spread alone routinely
exceeds any real edge, and an order large enough to clear the top level pays
progressively worse prices. Both are modelled here from the actual book rather
than assumed.

Everything is reported in **probability points** (1 pp = $0.01 of contract
price), because that is the unit the edge gate compares against: an estimate
that BTC finishes above a strike with probability 0.62 against an ask of 0.58 is
a 4 pp gross edge, and costs must be subtracted in the same currency.

On exits: at ~$2 a position, selling into a thin book is usually not available
at any sane price, so holding to resolution is the realistic plan.
:func:`round_trip_cost` reports the exit leg separately and flags when the book
cannot actually absorb it — never assume you can get out.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tradetk.costs.fees import KalshiFeeModel
from tradetk.venues.base import BinaryBook, Side

PP = Decimal(100)  # dollars -> probability points


@dataclass(frozen=True)
class ExecutionCost:
    """What it actually costs to acquire `contracts`, walking the real book."""

    contracts_requested: int
    contracts_filled: Decimal
    fully_filled: bool
    best_ask: Decimal | None
    average_price: Decimal | None
    notional: Decimal
    slippage_pp: Decimal
    spread_pp: Decimal
    fee: Decimal
    fee_pp: Decimal
    total_cost_pp: Decimal
    total_cost_dollars: Decimal
    side: str = "yes"

    def as_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "contracts_requested": self.contracts_requested,
            "contracts_filled": str(self.contracts_filled),
            "fully_filled": self.fully_filled,
            "best_ask": str(self.best_ask) if self.best_ask is not None else None,
            "average_price": str(self.average_price) if self.average_price is not None else None,
            "notional": str(self.notional),
            "slippage_pp": str(self.slippage_pp),
            "spread_pp": str(self.spread_pp),
            "fee": str(self.fee),
            "fee_pp": str(self.fee_pp),
            "total_cost_pp": str(self.total_cost_pp),
            "total_cost_dollars": str(self.total_cost_dollars),
        }


def spread_pp(book: BinaryBook) -> Decimal:
    """Quoted spread in probability points, or 0 when the book is one-sided.

    A one-sided book has no measurable spread — which is itself a reason to
    refuse the trade, handled by the liquidity gate rather than by inventing a
    number here.
    """
    s = book.spread
    return (s * PP) if s is not None else Decimal(0)


def execution_cost(
    book: BinaryBook,
    contracts: int,
    fee_model: KalshiFeeModel,
    *,
    side: Side = Side.yes,
    is_maker: bool = False,
    multiplier: Decimal | None = None,
) -> ExecutionCost:
    """Cost of buying `contracts` of `side` by crossing that side's offers.

    Side-symmetric on purpose. Buying NO consumes the resting YES bids (see
    :meth:`BinaryBook.walk_to_buy_no`), and Kalshi's fee formula is symmetric
    under ``P -> 1-P`` because it is proportional to ``P(1-P)`` — so the same
    fee model prices both sides with no special-casing.

    Slippage is measured against the best price on the side being bought, i.e.
    what a naive model would assume the whole order fills at. Fees are computed
    on the average price actually paid, not on the top of book.
    """
    if side is Side.yes:
        filled, notional = book.walk_to_buy_yes(contracts)
        best = book.best_yes_ask
    else:
        filled, notional = book.walk_to_buy_no(contracts)
        best = book.best_no_ask
    avg = (notional / filled) if filled > 0 else None

    slippage = ((avg - best) * PP) if (avg is not None and best is not None) else Decimal(0)

    # Maker fills rest inside the spread and pay no slippage by construction.
    if is_maker:
        slippage = Decimal(0)

    quote = fee_model.quote(
        int(filled), avg if avg is not None else Decimal(0),
        is_maker=is_maker, multiplier=multiplier,
    )
    fee_points = (quote.fee / filled * PP) if filled > 0 else Decimal(0)

    return ExecutionCost(
        contracts_requested=contracts,
        contracts_filled=filled,
        fully_filled=filled >= Decimal(contracts),
        best_ask=best,
        average_price=avg,
        notional=notional,
        slippage_pp=slippage,
        spread_pp=spread_pp(book),
        fee=quote.fee,
        fee_pp=fee_points,
        total_cost_pp=slippage + fee_points,
        total_cost_dollars=notional + quote.fee,
        side=side.value,
    )


def buy_cost(
    book: BinaryBook,
    contracts: int,
    fee_model: KalshiFeeModel,
    *,
    is_maker: bool = False,
    multiplier: Decimal | None = None,
) -> ExecutionCost:
    """Cost of buying `contracts` YES. Thin alias for the YES side."""
    return execution_cost(
        book, contracts, fee_model, side=Side.yes, is_maker=is_maker, multiplier=multiplier
    )


@dataclass(frozen=True)
class RoundTripCost:
    """Entry plus a modelled exit. The exit is usually theoretical at this size."""

    entry: ExecutionCost
    exit_average_price: Decimal | None
    exit_fee: Decimal
    exit_liquidity_available: bool
    entry_cost_pp: Decimal
    exit_cost_pp: Decimal
    round_trip_pp: Decimal
    hold_to_resolution_pp: Decimal

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry": self.entry.as_dict(),
            "exit_average_price": (
                str(self.exit_average_price) if self.exit_average_price is not None else None
            ),
            "exit_fee": str(self.exit_fee),
            "exit_liquidity_available": self.exit_liquidity_available,
            "entry_cost_pp": str(self.entry_cost_pp),
            "exit_cost_pp": str(self.exit_cost_pp),
            "round_trip_pp": str(self.round_trip_pp),
            "hold_to_resolution_pp": str(self.hold_to_resolution_pp),
        }


def round_trip_cost(
    book: BinaryBook,
    contracts: int,
    fee_model: KalshiFeeModel,
    *,
    is_maker: bool = False,
    multiplier: Decimal | None = None,
) -> RoundTripCost:
    """Entry cost, plus the exit cost *if* the book could absorb the exit.

    `hold_to_resolution_pp` is the number the edge gate should normally use:
    settlement pays $1 or $0 with no exit fee, so a position held to resolution
    pays only the entry cost. The round-trip figure is reported alongside so the
    cost of needing to get out early is visible rather than assumed away.
    """
    entry = buy_cost(book, contracts, fee_model, is_maker=is_maker, multiplier=multiplier)

    sold, proceeds = book.walk_to_sell_yes(entry.contracts_filled)
    can_exit = sold >= entry.contracts_filled and entry.contracts_filled > 0
    exit_avg = (proceeds / sold) if sold > 0 else None

    # Exiting means crossing to the bid, so it is a taker fill even if entry rested.
    exit_fee = (
        fee_model.fee(int(sold), exit_avg, is_maker=False, multiplier=multiplier)
        if exit_avg is not None else Decimal(0)
    )

    if entry.contracts_filled > 0 and exit_avg is not None and entry.average_price is not None:
        exit_pp = ((entry.average_price - exit_avg) * PP) + (
            exit_fee / entry.contracts_filled * PP
        )
    else:
        exit_pp = Decimal(0)

    return RoundTripCost(
        entry=entry,
        exit_average_price=exit_avg,
        exit_fee=exit_fee,
        exit_liquidity_available=can_exit,
        entry_cost_pp=entry.total_cost_pp,
        exit_cost_pp=exit_pp,
        round_trip_pp=entry.total_cost_pp + exit_pp,
        hold_to_resolution_pp=entry.total_cost_pp,
    )
