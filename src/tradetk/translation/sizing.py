"""Stage 4 of the translation layer: how many contracts, and why not more.

Sizing here is **fixed**, not proportional. A dollar target is converted into an
integer contract count, every hard cap is applied, and the binding one is named
in the output.

**Why fixed sizing and not Kelly.** Kelly sizing is the textbook answer and it
is the wrong answer here, for a reason specific to this book rather than a
matter of taste. Kelly's bet size is proportional to your edge, and edge is
``p - price`` — so Kelly is a lever that multiplies whatever error is in ``p``.
This toolkit's ``p`` comes from a lognormal fitted to realized vol, is known to
be biased in the tails, and has not yet been calibrated against a single
resolved contract. Sizing proportionally to an uncalibrated estimate converts a
model error into a capital error, and it does so fastest exactly where the model
is worst. Fixed sizing caps that blast radius at one slot.

Once step 10 produces a reliability diagram over a few hundred resolved
contracts, proportional sizing becomes a *defensible* conversation. Before then
it is leverage on an unvalidated number.

**Contracts are the decision; dollars are the constraint.** Contracts are
indivisible, so a $2.00 target against a $0.37 ask buys 5 contracts and deploys
$1.85 — the $0.15 shortfall is reported, never silently rounded away. This is
also why the per-position ceiling is checked against *one* contract first: if a
single contract already breaches it, no integer size works and the market is
simply not tradeable at this book size.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from tradetk.costs.fees import KalshiFeeModel

PP = Decimal(100)


class SizingMode(str, Enum):
    """How the contract count is chosen before caps are applied."""

    fixed_dollar = "fixed_dollar"  # target N dollars, convert to contracts
    fixed_contracts = "fixed_contracts"  # always the same contract count


class SizingCap(str, Enum):
    """Which constraint actually bound the size. Exactly one is the reason a
    position is smaller than the target, and knowing which one is the difference
    between "widen the book filter" and "add capital"."""

    none = "none"
    dollar_target = "dollar_target"
    per_position_ceiling = "per_position_ceiling"
    remaining_capital = "remaining_capital"
    book_participation = "book_participation"
    venue_minimum = "venue_minimum"


@dataclass(frozen=True)
class SizingLimits:
    """Hard caps. All dollar amounts, all enforced in code."""

    position_target: Decimal
    per_position_ceiling: Decimal
    total_capital: Decimal
    max_book_participation_pct: Decimal
    min_order_contracts: int = 1
    mode: SizingMode = SizingMode.fixed_dollar
    fixed_contracts: int = 1

    @classmethod
    def from_config(cls, config: Any, *, mode: SizingMode = SizingMode.fixed_dollar,
                    fixed_contracts: int = 1) -> "SizingLimits":
        return cls(
            position_target=Decimal(str(config.capital.position_target)),
            per_position_ceiling=Decimal(str(config.capital.per_position_ceiling)),
            total_capital=Decimal(str(config.capital.total_capital)),
            max_book_participation_pct=Decimal(str(config.liquidity.max_book_participation_pct)),
            mode=mode,
            fixed_contracts=fixed_contracts,
        )


@dataclass(frozen=True)
class SizingPlan:
    """A contract count with the full derivation and the binding constraint."""

    contracts: int
    tradeable: bool
    price_used: Decimal
    fee_per_contract: Decimal
    cost_per_contract: Decimal
    dollars_deployed: Decimal
    quantisation_shortfall: Decimal
    binding_cap: SizingCap
    mode: SizingMode
    target_contracts: int
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "contracts": self.contracts,
            "tradeable": self.tradeable,
            "mode": self.mode.value,
            "target_contracts": self.target_contracts,
            "price_used": str(self.price_used),
            "fee_per_contract": str(self.fee_per_contract),
            "cost_per_contract": str(self.cost_per_contract),
            "dollars_deployed": str(self.dollars_deployed),
            "quantisation_shortfall": str(self.quantisation_shortfall),
            "binding_cap": self.binding_cap.value,
            "reasons": list(self.reasons),
        }


def contracts_for_stake(
    stake: Decimal, price: Decimal, fee_per_contract: Decimal
) -> int:
    """``floor(stake / (price + fee_per_contract))``, floored at 1.

    Fees are inside the divisor because they are paid at entry: sizing on price
    alone reliably overshoots the dollar target by the fee, which on a $2
    position is not a rounding detail.

    Returning at least 1 is deliberate — the caller still has to check that one
    contract clears the ceiling, and conflating "too small to bother" with "too
    expensive to allow" would hide the second behind the first.
    """
    unit = price + fee_per_contract
    if unit <= 0:
        return 1
    return max(1, int(stake / unit))


def plan_size(
    price: Decimal,
    fee_model: KalshiFeeModel,
    limits: SizingLimits,
    *,
    book_depth: Decimal | None = None,
    capital_in_use: Decimal = Decimal(0),
    is_maker: bool = False,
) -> SizingPlan:
    """Turn a price and a set of caps into an integer contract count.

    Caps are applied in order and the *last* one to actually reduce the number
    is reported as binding. A plan with ``tradeable=False`` carries the reason;
    it is never returned as a silent zero.
    """
    reasons: list[str] = []
    fee_one = fee_model.fee(1, price, is_maker=is_maker)
    cost_one = price + fee_one

    if limits.mode is SizingMode.fixed_contracts:
        target = max(1, limits.fixed_contracts)
        cap = SizingCap.none
    else:
        target = contracts_for_stake(limits.position_target, price, fee_one)
        cap = SizingCap.dollar_target

    n = target

    # A single contract that breaches the ceiling makes the market untradeable
    # at this book size — no integer count fixes it.
    if cost_one > limits.per_position_ceiling:
        return SizingPlan(
            contracts=0,
            tradeable=False,
            price_used=price,
            fee_per_contract=fee_one,
            cost_per_contract=cost_one,
            dollars_deployed=Decimal(0),
            quantisation_shortfall=Decimal(0),
            binding_cap=SizingCap.per_position_ceiling,
            mode=limits.mode,
            target_contracts=target,
            reasons=(
                f"one contract costs {cost_one} (price {price} + fee {fee_one}), "
                f"above the per-position ceiling of {limits.per_position_ceiling}",
            ),
        )

    ceiling_max = int(limits.per_position_ceiling / cost_one)
    if ceiling_max < n:
        n = ceiling_max
        cap = SizingCap.per_position_ceiling
        reasons.append(
            f"per-position ceiling {limits.per_position_ceiling} allows {ceiling_max} "
            f"contracts at {cost_one} each"
        )

    remaining = limits.total_capital - capital_in_use
    capital_max = int(remaining / cost_one) if cost_one > 0 else 0
    if capital_max < n:
        n = max(0, capital_max)
        cap = SizingCap.remaining_capital
        reasons.append(
            f"only {remaining} of {limits.total_capital} capital remains, funding "
            f"{capital_max} contracts"
        )

    if book_depth is not None:
        participation_max = int(book_depth * limits.max_book_participation_pct / PP)
        if participation_max < n:
            n = max(0, participation_max)
            cap = SizingCap.book_participation
            reasons.append(
                f"{limits.max_book_participation_pct}% of {book_depth} visible "
                f"contracts caps the order at {participation_max}"
            )

    if n < limits.min_order_contracts:
        return SizingPlan(
            contracts=0,
            tradeable=False,
            price_used=price,
            fee_per_contract=fee_one,
            cost_per_contract=cost_one,
            dollars_deployed=Decimal(0),
            quantisation_shortfall=Decimal(0),
            binding_cap=(
                cap if cap is not SizingCap.dollar_target else SizingCap.venue_minimum
            ),
            mode=limits.mode,
            target_contracts=target,
            reasons=tuple(
                reasons
                + [
                    f"caps reduce the order to {n}, below the venue minimum of "
                    f"{limits.min_order_contracts}"
                ]
            ),
        )

    deployed = Decimal(n) * cost_one
    shortfall = (
        limits.position_target - deployed
        if limits.mode is SizingMode.fixed_dollar
        else Decimal(0)
    )

    return SizingPlan(
        contracts=n,
        tradeable=True,
        price_used=price,
        fee_per_contract=fee_one,
        cost_per_contract=cost_one,
        dollars_deployed=deployed,
        quantisation_shortfall=shortfall,
        binding_cap=cap,
        mode=limits.mode,
        target_contracts=target,
        reasons=tuple(reasons),
    )
