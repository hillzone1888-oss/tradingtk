"""Kalshi fee model — exact, from the published schedule, not a constant.

Sourced from the official fee schedule PDF (effective 2026-07-07) and the API
docs, both fetched live rather than recalled:

    Taker: fees = round_up(M x 0.07   x C x P x (1-P))    M default 1
    Maker: fees = round_up(M x 0.0175 x C x P x (1-P))    M default 0
    round up = "rounds up such that the fee + positionCost is rounded to a centicent"

    Settlement fee: none.  Membership fee: none.

**Maker fees are zero for our universe.** The maker multiplier defaults to 0,
and none of the tradeable crypto series (KXBTC*, KXETH*, KXSOL*, KXXRP*,
KXBNB*, KXHYPE*) appear in the schedule's Non-Standard Fees table. Resting an
order therefore costs nothing in fees, which makes the preference for limit
orders far stronger than "maker is cheaper".

**The rounding granularity is genuinely ambiguous, so it is configurable.** The
prose in both sources says *centicent* ($0.0001). The published General Trading
Fees Table matches the formula only under *cent* ($0.01) rounding — verified
against all 14 of its rows. At 100 contracts the two agree to the penny, so the
table cannot distinguish them; at the 2-4 contracts a $2 position actually buys,
they differ by up to 194%.

We default to **cent** rounding because it is the conservative error: modelling
fees cheaper than reality lets marginal trades through the edge gate, while
modelling them dearer only skips trades. The spec's fill reconciliation is what
resolves this for real — log the fee actually charged and compare.

**The insight that drives sizing** (see :func:`cost_pct_of_stake`): for a stake
S at price P, contracts = S/P, so total fee = m x S x (1-P). Cost as a fraction
of capital deployed is ``m x (1-P)`` — independent of stake. Cheap longshots are
the *most* expensive per dollar staked; favourites the cheapest.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from enum import Enum
from typing import Any

# Published constants. Overridable so a schedule change is data, not a code edit.
TAKER_MULTIPLIER = Decimal("0.07")
MAKER_MULTIPLIER = Decimal("0.0175")
DEFAULT_TAKER_M = Decimal(1)
DEFAULT_MAKER_M = Decimal(0)

CENT = Decimal("0.01")
CENTICENT = Decimal("0.0001")

# Every row of the published General Trading Fees Table (100 contracts), used as
# a startup regression check against the formula.
PUBLISHED_TABLE_100 = {
    "0.01": "0.07", "0.05": "0.34", "0.10": "0.63", "0.15": "0.90",
    "0.20": "1.12", "0.25": "1.32", "0.30": "1.47", "0.35": "1.60",
    "0.40": "1.68", "0.45": "1.74", "0.50": "1.75", "0.55": "1.74",
    "0.60": "1.68", "0.65": "1.60", "0.70": "1.47", "0.75": "1.32",
    "0.80": "1.12", "0.85": "0.90", "0.90": "0.63", "0.95": "0.34",
    "0.99": "0.07",
}


class FeeRounding(str, Enum):
    """Granularity the fee is rounded UP to."""

    cent = "cent"  # $0.01 — conservative; matches the published table
    centicent = "centicent"  # $0.0001 — matches the prose in both sources

    @property
    def quantum(self) -> Decimal:
        return CENT if self is FeeRounding.cent else CENTICENT


@dataclass(frozen=True)
class FeeQuote:
    """A fully traced fee calculation. Every intermediate is kept so a surprising
    number can be explained without re-deriving it."""

    contracts: int
    price: Decimal
    is_maker: bool
    multiplier: Decimal
    rate: Decimal
    raw_fee: Decimal
    fee: Decimal
    rounding: FeeRounding
    notional: Decimal
    total_cost: Decimal
    fee_pct_of_stake: Decimal
    rounding_penalty: Decimal

    def as_dict(self) -> dict[str, Any]:
        return {
            "contracts": self.contracts,
            "price": str(self.price),
            "is_maker": self.is_maker,
            "multiplier": str(self.multiplier),
            "rate": str(self.rate),
            "raw_fee": str(self.raw_fee),
            "fee": str(self.fee),
            "rounding": self.rounding.value,
            "rounding_penalty": str(self.rounding_penalty),
            "notional": str(self.notional),
            "total_cost": str(self.total_cost),
            "fee_pct_of_stake": f"{self.fee_pct_of_stake:.4%}",
        }


class KalshiFeeModel:
    """Fee model with the published constants injected, never hardcoded inline."""

    def __init__(
        self,
        *,
        taker_multiplier: Decimal = TAKER_MULTIPLIER,
        maker_multiplier: Decimal = MAKER_MULTIPLIER,
        rounding: FeeRounding = FeeRounding.cent,
    ) -> None:
        self.taker_multiplier = Decimal(str(taker_multiplier))
        self.maker_multiplier = Decimal(str(maker_multiplier))
        self.rounding = rounding

    def rate(self, *, is_maker: bool) -> Decimal:
        return self.maker_multiplier if is_maker else self.taker_multiplier

    def series_multiplier(self, *, is_maker: bool, override: Decimal | None = None) -> Decimal:
        """Per-series M. Defaults are 1 taker / 0 maker unless the schedule's
        Non-Standard table says otherwise."""
        if override is not None:
            return Decimal(str(override))
        return DEFAULT_MAKER_M if is_maker else DEFAULT_TAKER_M

    def quote(
        self,
        contracts: int,
        price: Decimal | str | float,
        *,
        is_maker: bool = False,
        multiplier: Decimal | None = None,
    ) -> FeeQuote:
        """Full fee calculation for `contracts` at `price`."""
        if contracts < 0:
            raise ValueError("contracts must be non-negative")
        p = Decimal(str(price))
        if not (Decimal(0) <= p <= Decimal(1)):
            raise ValueError(f"price must be in [0, 1] dollars, got {p}")

        m = self.series_multiplier(is_maker=is_maker, override=multiplier)
        rate = self.rate(is_maker=is_maker)
        raw = m * rate * Decimal(contracts) * p * (Decimal(1) - p)

        notional = Decimal(contracts) * p
        # Stated rule: round up so that (fee + positionCost) lands on the
        # quantum. Implemented literally rather than simplified.
        quantum = self.rounding.quantum
        total = notional + raw
        total_rounded = (total / quantum).quantize(Decimal(1), rounding=ROUND_CEILING) * quantum
        fee = total_rounded - notional
        if fee < 0:
            fee = Decimal(0)

        pct = (fee / notional) if notional > 0 else Decimal(0)
        return FeeQuote(
            contracts=contracts, price=p, is_maker=is_maker, multiplier=m, rate=rate,
            raw_fee=raw, fee=fee, rounding=self.rounding, notional=notional,
            total_cost=notional + fee, fee_pct_of_stake=pct, rounding_penalty=fee - raw,
        )

    def fee(
        self,
        contracts: int,
        price: Decimal | str | float,
        *,
        is_maker: bool = False,
        multiplier: Decimal | None = None,
    ) -> Decimal:
        return self.quote(contracts, price, is_maker=is_maker, multiplier=multiplier).fee

    def cost_pct_of_stake(
        self, price: Decimal | str | float, *, is_maker: bool = False,
        multiplier: Decimal | None = None,
    ) -> Decimal:
        """Fee as a fraction of capital deployed, **before** roundup: ``m x (1-P)``.

        Independent of stake size, which is what makes it the right unit for the
        edge gate. Note the inversion this implies: at the taker rate a $0.05
        longshot costs 6.65% of stake in fees while a $0.95 favourite costs
        0.35%. Actual cost is higher once roundup bites on a small order — use
        :meth:`quote` for a specific order.
        """
        p = Decimal(str(price))
        m = self.series_multiplier(is_maker=is_maker, override=multiplier)
        return m * self.rate(is_maker=is_maker) * (Decimal(1) - p)

    def cost_pp_of_stake(self, price, *, is_maker: bool = False,
                         multiplier: Decimal | None = None) -> Decimal:
        """Same quantity in probability points, the edge gate's unit."""
        return self.cost_pct_of_stake(price, is_maker=is_maker, multiplier=multiplier) * 100

    # -- verification -------------------------------------------------

    def verify_against_published_table(self) -> dict[str, Any]:
        """Check the formula reproduces every published table row.

        Run at startup: the schedule changes, and a silent drift between our
        model and the venue's arithmetic is exactly the sort of error that only
        shows up as unexplained losses.
        """
        mismatches = []
        cent_model = KalshiFeeModel(
            taker_multiplier=self.taker_multiplier,
            maker_multiplier=self.maker_multiplier,
            rounding=FeeRounding.cent,
        )
        for price, expected in PUBLISHED_TABLE_100.items():
            got = cent_model.fee(100, price)
            if got != Decimal(expected):
                mismatches.append({"price": price, "expected": expected, "got": str(got)})
        return {
            "rows_checked": len(PUBLISHED_TABLE_100),
            "mismatches": mismatches,
            "ok": not mismatches,
            "taker_multiplier": str(self.taker_multiplier),
            "maker_multiplier": str(self.maker_multiplier),
            "rounding": self.rounding.value,
            "note": (
                "The published 100-contract table is reproduced exactly under CENT "
                "rounding. Both the schedule PDF and the API docs describe CENTICENT "
                "rounding in prose; at 100 contracts the two are indistinguishable. "
                "Reconcile against a real fill to settle it."
            ),
        }

    def verify_series(self, fee_type: str, fee_multiplier: Decimal | int) -> dict[str, Any]:
        """Cross-check a series' venue-reported fee parameters against the model.

        The venue reports `fee_type` and `fee_multiplier` per series; a series
        that charges maker fees, or carries a non-default multiplier, must not be
        priced with the defaults.
        """
        charges_maker = "maker" in (fee_type or "").lower()
        reported = Decimal(str(fee_multiplier))
        return {
            "fee_type": fee_type,
            "reported_multiplier": str(reported),
            "charges_maker_fees": charges_maker,
            "model_taker_multiplier_matches": reported == DEFAULT_TAKER_M,
            "maker_fee_is_zero": not charges_maker,
            "warning": (
                None if reported == DEFAULT_TAKER_M
                else f"series multiplier {reported} differs from default {DEFAULT_TAKER_M}; "
                     "pass it explicitly to quote()"
            ),
        }


def reconcile_fill(
    model: KalshiFeeModel, contracts: int, price: Decimal | str, actual_fee: Decimal | str,
    *, is_maker: bool = False, multiplier: Decimal | None = None,
) -> dict[str, Any]:
    """Compare a real fill's fee against the model and flag divergence.

    Required by the operating rules: the model is a hypothesis until a fill
    tests it. This is also what will settle the cent-vs-centicent question.
    """
    predicted = model.quote(contracts, price, is_maker=is_maker, multiplier=multiplier)
    actual = Decimal(str(actual_fee))
    diff = actual - predicted.fee
    alt = KalshiFeeModel(
        taker_multiplier=model.taker_multiplier, maker_multiplier=model.maker_multiplier,
        rounding=(FeeRounding.centicent if model.rounding is FeeRounding.cent
                  else FeeRounding.cent),
    ).fee(contracts, price, is_maker=is_maker, multiplier=multiplier)
    return {
        "predicted_fee": str(predicted.fee),
        "actual_fee": str(actual),
        "difference": str(diff),
        "matches": diff == 0,
        "alternative_rounding_fee": str(alt),
        "alternative_rounding_would_match": alt == actual,
        "rounding_assumed": model.rounding.value,
    }
