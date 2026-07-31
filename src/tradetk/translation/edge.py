"""Stage 3 of the translation layer: probability + book -> a gated decision.

An estimate is not an edge. The venue's price is what you actually pay, and
between the two sit fees, the spread, and the fact that your order has to walk a
real book. This module subtracts all of it and then applies every gate, in one
place, with every failure reported rather than the first one short-circuiting.

**The arithmetic, stated once so it cannot drift.** For one contract of the side
being bought, held to resolution:

    EV = P(side resolves YES) - average_price_paid - fee_per_contract

expressed in probability points (1 pp = $0.01) as

    gross_edge_pp = (p_side - best_price_side) * 100      # the naive edge
    cost_pp       = slippage_pp + fee_pp                  # from costs.spread
    net_edge_pp   = gross_edge_pp - cost_pp

Slippage is the gap between the best price and the average actually paid, so
``net_edge_pp`` equals ``EV * 100`` exactly. This is worth stating because the
obvious mistake — computing edge against the average fill price *and then* also
subtracting slippage — double-counts and quietly makes bad trades look fine.

**Held to resolution, not round-tripped.** Settlement pays $1 or $0 and charges
no fee, so a held position pays the entry cost only. At ~$2 a position the exit
book usually cannot absorb the order at any sane price anyway; the round-trip
number is reported by ``costs.spread`` for visibility, but the gate does not
assume an exit exists.

**Both sides are evaluated.** A YES ask of 0.50 against an estimate of 0.30 is
not "no edge" — it is a 20-point edge on NO. Gating only the YES side would
discard half the universe and bias the book toward whatever the crowd happens to
have priced high. Each side is assessed independently and the better *passing*
one is chosen.

**Gates are evaluated exhaustively.** Every failed gate is listed, because "this
market failed on depth" and "this market failed on depth, horizon, and edge" are
different facts when you are deciding what to record and what to fix.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from tradetk.costs.fees import KalshiFeeModel
from tradetk.costs.spread import ExecutionCost, execution_cost
from tradetk.translation.claims import Claim
from tradetk.translation.probability import ProbabilityEstimate
from tradetk.venues.base import BinaryBook, Side

PP = Decimal(100)
ONE = Decimal(1)


class Decision(str, Enum):
    trade = "trade"
    reject = "reject"


class GateName(str, Enum):
    """Named so rejection counts aggregate into something actionable."""

    book_present = "book_present"
    book_not_crossed = "book_not_crossed"
    fillable = "fillable"
    depth_multiple = "depth_multiple"
    participation = "participation"
    horizon = "horizon"
    net_edge = "net_edge"
    model_reliability = "model_reliability"


@dataclass(frozen=True)
class GateLimits:
    """The thresholds the gate enforces, decoupled from the config object.

    Taking primitives rather than a :class:`~tradetk.config.schema.Config` keeps
    this module testable without constructing a whole valid config, and makes
    every number that can reject a trade visible in one signature.
    """

    min_net_edge_pp: Decimal
    margin_pp: Decimal
    min_book_depth_multiple: Decimal
    max_book_participation_pct: Decimal
    max_hours_to_resolution: Decimal
    reject_deep_tail: bool = True

    @property
    def required_edge_pp(self) -> Decimal:
        """Edge a trade must clear *after* costs: the floor plus the cushion.

        Two separate numbers on purpose. ``min_net_edge_pp`` is "an edge this
        small is not worth a slot"; ``margin_pp`` is "the cost model itself has
        error and I want headroom against it". They move for different reasons.
        """
        return self.min_net_edge_pp + self.margin_pp

    @classmethod
    def from_config(cls, config: Any, *, reject_deep_tail: bool = True) -> "GateLimits":
        return cls(
            min_net_edge_pp=Decimal(str(config.edge_gate.min_net_edge_pp)),
            margin_pp=Decimal(str(config.edge_gate.margin_pp)),
            min_book_depth_multiple=Decimal(str(config.liquidity.min_book_depth_multiple)),
            max_book_participation_pct=Decimal(str(config.liquidity.max_book_participation_pct)),
            max_hours_to_resolution=Decimal(str(config.horizon.max_hours_to_resolution)),
            reject_deep_tail=reject_deep_tail,
        )


@dataclass(frozen=True)
class GateFailure:
    gate: GateName
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"gate": self.gate.value, "detail": self.detail}


@dataclass(frozen=True)
class EdgeAssessment:
    """One side of one contract, fully assessed and fully explained."""

    ticker: str
    underlying: str
    side: Side
    decision: Decision
    p_side: Decimal
    contracts_requested: int
    best_price: Decimal | None
    average_price: Decimal | None
    gross_edge_pp: Decimal
    slippage_pp: Decimal
    fee_pp: Decimal
    cost_pp: Decimal
    net_edge_pp: Decimal
    required_edge_pp: Decimal
    expected_value_dollars: Decimal
    capital_at_risk: Decimal
    side_depth_contracts: Decimal
    hours_to_resolution: float
    failures: tuple[GateFailure, ...]
    execution: ExecutionCost | None
    warnings: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.decision is Decision.trade

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "underlying": self.underlying,
            "side": self.side.value,
            "decision": self.decision.value,
            "p_side": str(self.p_side),
            "contracts_requested": self.contracts_requested,
            "best_price": str(self.best_price) if self.best_price is not None else None,
            "average_price": (
                str(self.average_price) if self.average_price is not None else None
            ),
            "gross_edge_pp": str(self.gross_edge_pp),
            "slippage_pp": str(self.slippage_pp),
            "fee_pp": str(self.fee_pp),
            "cost_pp": str(self.cost_pp),
            "net_edge_pp": str(self.net_edge_pp),
            "required_edge_pp": str(self.required_edge_pp),
            "expected_value_dollars": str(self.expected_value_dollars),
            "capital_at_risk": str(self.capital_at_risk),
            "side_depth_contracts": str(self.side_depth_contracts),
            "hours_to_resolution": round(self.hours_to_resolution, 4),
            "failures": [f.as_dict() for f in self.failures],
            "warnings": list(self.warnings),
            "execution": self.execution.as_dict() if self.execution else None,
        }


def side_probability(p_yes: Decimal, side: Side) -> Decimal:
    """P(the side you bought pays $1). NO is the complement, exactly."""
    return p_yes if side is Side.yes else ONE - p_yes


def side_depth(book: BinaryBook, side: Side) -> Decimal:
    """Visible contracts available to *buy* on `side`.

    Buying YES consumes `yes_asks`; buying NO consumes `yes_bids` — the same
    resting orders seen from the other side.
    """
    levels = book.yes_asks if side is Side.yes else book.yes_bids
    return sum((lv.size for lv in levels), Decimal(0))


def assess_side(
    claim: Claim,
    estimate: ProbabilityEstimate,
    book: BinaryBook,
    *,
    side: Side,
    contracts: int,
    fee_model: KalshiFeeModel,
    limits: GateLimits,
    now: datetime,
    is_maker: bool = False,
) -> EdgeAssessment:
    """Assess buying `contracts` of `side`, running every gate."""
    failures: list[GateFailure] = []
    warnings: list[str] = list(estimate.warnings)

    p_side = side_probability(estimate.p, side)
    best = book.best_yes_ask if side is Side.yes else book.best_no_ask
    depth = side_depth(book, side)
    hours = claim.hours_to_resolution(now)

    # --- structural gates: is there anything to trade against at all? ---
    if best is None or depth <= 0:
        failures.append(
            GateFailure(
                GateName.book_present,
                f"no offers on the {side.value} side — nothing to buy at any price",
            )
        )
    if book.is_crossed():
        failures.append(
            GateFailure(
                GateName.book_not_crossed,
                f"book is crossed (bid {book.best_yes_bid} > ask {book.best_yes_ask}); "
                "a crossed book is a stale or broken snapshot, not an opportunity",
            )
        )

    execution: ExecutionCost | None = None
    gross = slippage = fee_pp = cost_pp = net = Decimal(0)

    if best is not None and depth > 0:
        execution = execution_cost(
            book, contracts, fee_model, side=side, is_maker=is_maker
        )
        gross = (p_side - best) * PP
        slippage = execution.slippage_pp
        fee_pp = execution.fee_pp
        cost_pp = execution.total_cost_pp
        net = gross - cost_pp

        if not execution.fully_filled:
            failures.append(
                GateFailure(
                    GateName.fillable,
                    f"book holds only {execution.contracts_filled} of {contracts} "
                    "contracts; a partial fill is a different position than the one "
                    "that was gated",
                )
            )

        required_depth = limits.min_book_depth_multiple * Decimal(contracts)
        if depth < required_depth:
            failures.append(
                GateFailure(
                    GateName.depth_multiple,
                    f"visible depth {depth} < {limits.min_book_depth_multiple}x order "
                    f"({required_depth}); too thin to enter without moving the price",
                )
            )

        max_contracts = depth * limits.max_book_participation_pct / PP
        if Decimal(contracts) > max_contracts:
            failures.append(
                GateFailure(
                    GateName.participation,
                    f"order of {contracts} exceeds {limits.max_book_participation_pct}% "
                    f"of visible depth ({max_contracts:.2f} contracts)",
                )
            )

        if net < limits.required_edge_pp:
            failures.append(
                GateFailure(
                    GateName.net_edge,
                    f"net edge {net:.2f} pp < required {limits.required_edge_pp} pp "
                    f"(gross {gross:.2f} - costs {cost_pp:.2f})",
                )
            )

    if Decimal(str(hours)) > limits.max_hours_to_resolution:
        failures.append(
            GateFailure(
                GateName.horizon,
                f"resolves in {hours:.1f}h, beyond the {limits.max_hours_to_resolution}h "
                "limit; capital locked that long is capital not compounding",
            )
        )

    if limits.reject_deep_tail and estimate.is_deep_tail:
        failures.append(
            GateFailure(
                GateName.model_reliability,
                f"estimate sits {abs(estimate.z_score or 0):.1f} sigma into the tail, "
                "where the lognormal is known to understate probability; the apparent "
                "edge is more likely model error than opportunity",
            )
        )

    capital = execution.total_cost_dollars if execution else Decimal(0)
    ev = (net / PP) * Decimal(contracts)

    return EdgeAssessment(
        ticker=claim.ticker,
        underlying=claim.underlying,
        side=side,
        decision=Decision.reject if failures else Decision.trade,
        p_side=p_side,
        contracts_requested=contracts,
        best_price=best,
        average_price=execution.average_price if execution else None,
        gross_edge_pp=gross,
        slippage_pp=slippage,
        fee_pp=fee_pp,
        cost_pp=cost_pp,
        net_edge_pp=net,
        required_edge_pp=limits.required_edge_pp,
        expected_value_dollars=ev,
        capital_at_risk=capital,
        side_depth_contracts=depth,
        hours_to_resolution=hours,
        failures=tuple(failures),
        execution=execution,
        warnings=tuple(warnings),
    )


@dataclass(frozen=True)
class ClaimAssessment:
    """Both sides assessed, with the chosen one (if any) called out."""

    ticker: str
    yes: EdgeAssessment
    no: EdgeAssessment
    chosen: EdgeAssessment | None

    @property
    def has_trade(self) -> bool:
        return self.chosen is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "has_trade": self.has_trade,
            "chosen_side": self.chosen.side.value if self.chosen else None,
            "yes": self.yes.as_dict(),
            "no": self.no.as_dict(),
        }


def assess_claim(
    claim: Claim,
    estimate: ProbabilityEstimate,
    book: BinaryBook,
    *,
    contracts: int,
    fee_model: KalshiFeeModel,
    limits: GateLimits,
    now: datetime,
    is_maker: bool = False,
) -> ClaimAssessment:
    """Assess both sides and pick the better one that passes every gate.

    Both sides can never pass simultaneously with a coherent estimate — their
    net edges sum to a negative number once costs are subtracted, which is just
    the statement that the venue's spread and fees are positive. If both somehow
    pass, the larger net edge wins and the situation is a bug worth noticing,
    not an arbitrage.
    """
    yes = assess_side(
        claim, estimate, book, side=Side.yes, contracts=contracts,
        fee_model=fee_model, limits=limits, now=now, is_maker=is_maker,
    )
    no = assess_side(
        claim, estimate, book, side=Side.no, contracts=contracts,
        fee_model=fee_model, limits=limits, now=now, is_maker=is_maker,
    )

    passing = [a for a in (yes, no) if a.passed]
    chosen = max(passing, key=lambda a: a.net_edge_pp) if passing else None
    return ClaimAssessment(ticker=claim.ticker, yes=yes, no=no, chosen=chosen)
