"""Venue protocol and canonical binary-contract models.

The strategy, cost, and risk layers must never learn which venue they are on.
That is enforced here by normalising away the single biggest venue-specific
quirk in prediction markets:

**A binary book has bids on both sides and no asks at all.** Kalshi (and
Polymarket) publish resting bids to buy YES *and* resting bids to buy NO. A NO
bid at 0.96 is economically a YES ask at 0.04 — the same order, seen from the
other side. Code that treats the raw feed as a normal bid/ask book will silently
price every trade wrong.

So :class:`BinaryBook` exposes a canonical **YES-denominated** view — real bids
and derived asks — and every adapter is responsible for producing it. A new
venue is then purely an adapter, with no change to anything downstream.

Prices are :class:`~decimal.Decimal`, not float. Contracts trade on a one-cent
grid and fees round *up* to the cent, so binary-float error is not acceptable
here: at $2 a position, a fraction of a cent is a material share of the trade.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

ONE = Decimal("1")
CENT = Decimal("0.01")


class VenueError(Exception):
    """Base class for venue failures."""


class VenueAuthError(VenueError):
    """Missing or rejected credentials."""


class VenueDataError(VenueError):
    """A response parsed structurally but failed a sanity check."""


class Side(str, Enum):
    """Which side of the binary contract an order is denominated in."""

    yes = "yes"
    no = "no"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class BookLevel(_Model):
    """One price level. `price` is always YES-denominated, in dollars 0..1."""

    price: Decimal = Field(ge=0, le=1)
    size: Decimal = Field(ge=0, description="Contracts resting at this level.")

    @field_validator("price", "size", mode="before")
    @classmethod
    def _coerce(cls, v: object) -> Decimal:
        # The venue sends decimal strings ("0.0330"). Decimal(str) keeps them exact;
        # going via float would not.
        return Decimal(str(v))


class BinaryBook(_Model):
    """Canonical YES-denominated order book.

    `yes_bids` are what you can sell YES into (best = highest price first).
    `yes_asks` are what you can buy YES from (best = lowest price first), and on
    most venues these are *derived* from resting NO bids.
    """

    ticker: str
    retrieved_at: datetime
    yes_bids: list[BookLevel] = Field(default_factory=list)
    yes_asks: list[BookLevel] = Field(default_factory=list)

    @property
    def best_yes_bid(self) -> Decimal | None:
        return self.yes_bids[0].price if self.yes_bids else None

    @property
    def best_yes_ask(self) -> Decimal | None:
        return self.yes_asks[0].price if self.yes_asks else None

    @property
    def best_no_bid(self) -> Decimal | None:
        """A YES ask at 0.04 is a NO bid at 0.96 — the same resting order."""
        ask = self.best_yes_ask
        return None if ask is None else ONE - ask

    @property
    def best_no_ask(self) -> Decimal | None:
        bid = self.best_yes_bid
        return None if bid is None else ONE - bid

    @property
    def spread(self) -> Decimal | None:
        if self.best_yes_bid is None or self.best_yes_ask is None:
            return None
        return self.best_yes_ask - self.best_yes_bid

    @property
    def mid(self) -> Decimal | None:
        """Informational only. Never price a trade off the mid — you cannot
        transact there. The edge calculation uses the side you actually pay."""
        if self.best_yes_bid is None or self.best_yes_ask is None:
            return None
        return (self.best_yes_bid + self.best_yes_ask) / 2

    def is_crossed(self) -> bool:
        if self.best_yes_bid is None or self.best_yes_ask is None:
            return False
        return self.best_yes_bid > self.best_yes_ask

    def depth(self, side: Side) -> Decimal:
        """Total visible contracts on a side. Used by the liquidity gate."""
        levels = self.yes_asks if side is Side.yes else self.yes_bids
        return sum((lv.size for lv in levels), Decimal(0))

    def walk_to_buy_yes(self, contracts: Decimal | int) -> tuple[Decimal, Decimal]:
        """Walk the ask side buying `contracts` YES.

        Returns ``(filled_contracts, total_cost_dollars)``. Never assumes a
        single price: in a thin book the top level is often smaller than the
        order, and pretending otherwise is how a backtest becomes fiction.
        Partial fills are reported honestly rather than raising.
        """
        want = Decimal(str(contracts))
        if want <= 0:
            return Decimal(0), Decimal(0)
        filled = Decimal(0)
        cost = Decimal(0)
        for level in self.yes_asks:
            if filled >= want:
                break
            take = min(level.size, want - filled)
            filled += take
            cost += take * level.price
        return filled, cost

    def walk_to_sell_yes(self, contracts: Decimal | int) -> tuple[Decimal, Decimal]:
        """Walk the bid side selling `contracts` YES -> (filled, proceeds)."""
        want = Decimal(str(contracts))
        if want <= 0:
            return Decimal(0), Decimal(0)
        filled = Decimal(0)
        proceeds = Decimal(0)
        for level in self.yes_bids:
            if filled >= want:
                break
            take = min(level.size, want - filled)
            filled += take
            proceeds += take * level.price
        return filled, proceeds


class VenueMarket(_Model):
    """A tradeable binary contract, venue-agnostic.

    Structured strike fields (`strike_type`, `floor_strike`, `cap_strike`) are
    kept separate from the human `title`: the claim parser in step 6 must prefer
    machine-readable strikes over regexing English, and refuse markets where the
    structured form is absent or ambiguous.
    """

    ticker: str
    series_ticker: str | None = None
    event_ticker: str | None = None
    title: str
    status: str
    close_time: datetime | None = None
    expiration_time: datetime | None = None
    strike_type: str | None = None
    floor_strike: Decimal | None = None
    cap_strike: Decimal | None = None
    rules_primary: str | None = None
    yes_bid: Decimal | None = None
    yes_ask: Decimal | None = None
    volume: Decimal | None = None
    liquidity: Decimal | None = None

    @property
    def has_machine_readable_strike(self) -> bool:
        """Whether this market can be parsed into a typed claim without NLP."""
        return self.strike_type not in (None, "", "custom") and (
            self.floor_strike is not None or self.cap_strike is not None
        )


class VenueMinimums(_Model):
    """Venue-imposed floors. Checked at startup: if these make a ~$2 position
    impossible, the operator must be told immediately, not designed around."""

    min_order_contracts: int = 1
    price_tick: Decimal = CENT
    min_price: Decimal = CENT
    max_price: Decimal = Decimal("0.99")
    min_deposit_dollars: Decimal | None = None
    min_withdrawal_dollars: Decimal | None = None
    per_order_min_fee_dollars: Decimal | None = None
    notes: str = ""


class FeeSchedule(_Model):
    """Per-series fee parameters as reported by the venue itself.

    The multiplier constant is NOT hardcoded anywhere: it is read from the venue
    so a schedule change surfaces as data rather than as a silently wrong model.
    """

    series_ticker: str
    fee_type: str
    fee_multiplier: Decimal
    maker_fees_charged: bool = False


@runtime_checkable
class Venue(Protocol):
    """Execution-venue interface.

    Deliberately **read-only**. Order submission is not part of this protocol:
    the only code path that may contact an order endpoint lives in the `execute`
    command module, so no adapter can accidentally provide one.
    """

    name: str
    environment: str

    def markets(self, *, series_ticker: str | None = ..., status: str = ...,
                limit: int = ...) -> list[VenueMarket]: ...

    def market(self, ticker: str) -> VenueMarket: ...

    def orderbook(self, ticker: str, *, depth: int = ...) -> BinaryBook: ...

    def minimums(self) -> VenueMinimums: ...

    def fee_schedule(self, series_ticker: str) -> FeeSchedule: ...
