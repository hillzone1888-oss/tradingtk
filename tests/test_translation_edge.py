"""The edge gate.

Two things carry most of the weight here. First the identity
``net_edge_pp == (p - avg_price - fee_per_contract) * 100`` — the double-count
bug it guards against is invisible in the output and makes losing trades pass.
Second the NO side: a gate that only ever buys YES silently discards half the
universe, and no amount of downstream testing would reveal it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from tradetk.costs.fees import KalshiFeeModel
from tradetk.translation.claims import Claim, ClaimOperator
from tradetk.translation.edge import (
    Decision,
    GateLimits,
    GateName,
    assess_claim,
    assess_side,
    side_depth,
    side_probability,
)
from tradetk.translation.probability import ProbabilityEstimate
from tradetk.venues.base import BinaryBook, BookLevel, Side

D = Decimal
NOW = dt.datetime(2026, 7, 22, 12, 0, tzinfo=dt.timezone.utc)
LATER = dt.datetime(2026, 7, 22, 18, 0, tzinfo=dt.timezone.utc)  # +6h


@pytest.fixture
def model() -> KalshiFeeModel:
    return KalshiFeeModel()


@pytest.fixture
def limits() -> GateLimits:
    return GateLimits(
        min_net_edge_pp=D("3.0"),
        margin_pp=D("1.0"),
        min_book_depth_multiple=D("5.0"),
        max_book_participation_pct=D("10.0"),
        max_hours_to_resolution=D("168"),
    )


def claim(resolution_time: dt.datetime = LATER) -> Claim:
    return Claim(
        ticker="KXBTCD-TEST", series_ticker="KXBTCD", underlying="BTC",
        operator=ClaimOperator.above, threshold=D("100000"),
        resolution_time=resolution_time, resolution_source="CF Benchmarks BRTI",
        rules_primary="test rules",
    )


def estimate(p: str, *, z: float | None = 0.5, warnings: list[str] | None = None
             ) -> ProbabilityEstimate:
    return ProbabilityEstimate(
        ticker="KXBTCD-TEST", underlying="BTC", p=D(p), method="test",
        computed_at=NOW, spot=100000.0, sigma_annual=0.6, hours_to_resolution=6.0,
        z_score=z, warnings=warnings or [],
    )


def book(*, bids=(("0.40", "500"),), asks=(("0.45", "500"),)) -> BinaryBook:
    return BinaryBook(
        ticker="KXBTCD-TEST", retrieved_at=NOW,
        yes_bids=[BookLevel(price=p, size=s) for p, s in bids],
        yes_asks=[BookLevel(price=p, size=s) for p, s in asks],
    )


# ── side helpers ───────────────────────────────────────────────────


def test_no_probability_is_the_exact_complement() -> None:
    assert side_probability(D("0.62"), Side.no) == D("0.38")
    assert side_probability(D("0.62"), Side.yes) == D("0.62")


def test_side_depth_reads_the_side_you_would_consume() -> None:
    b = book(bids=(("0.40", "300"),), asks=(("0.45", "700"),))
    assert side_depth(b, Side.yes) == D("700")  # buying YES eats the asks
    assert side_depth(b, Side.no) == D("300")  # buying NO eats the YES bids


# ── the core arithmetic ────────────────────────────────────────────


def test_net_edge_equals_expected_value_per_contract(model, limits) -> None:
    """The identity that the double-count bug would break."""
    a = assess_side(
        claim(), estimate("0.60"), book(), side=Side.yes, contracts=4,
        fee_model=model, limits=limits, now=NOW,
    )
    fee_per_contract = a.execution.fee / a.execution.contracts_filled
    expected = (a.p_side - a.average_price - fee_per_contract) * 100
    assert a.net_edge_pp == pytest.approx(float(expected), abs=1e-9)


def test_net_edge_is_gross_minus_costs(model, limits) -> None:
    a = assess_side(
        claim(), estimate("0.60"), book(), side=Side.yes, contracts=4,
        fee_model=model, limits=limits, now=NOW,
    )
    assert a.net_edge_pp == a.gross_edge_pp - a.cost_pp
    assert a.cost_pp == a.slippage_pp + a.fee_pp


def test_gross_edge_is_measured_against_the_best_price(model, limits) -> None:
    a = assess_side(
        claim(), estimate("0.60"), book(), side=Side.yes, contracts=4,
        fee_model=model, limits=limits, now=NOW,
    )
    assert a.best_price == D("0.45")
    assert a.gross_edge_pp == (D("0.60") - D("0.45")) * 100


def test_slippage_appears_when_the_order_walks_levels(model, limits) -> None:
    deep = book(asks=(("0.45", "2"), ("0.50", "1000")))
    a = assess_side(
        claim(), estimate("0.70"), deep, side=Side.yes, contracts=10,
        fee_model=model, limits=limits, now=NOW,
    )
    assert a.average_price > a.best_price
    assert a.slippage_pp > 0


# ── the NO side ────────────────────────────────────────────────────


def test_cheap_estimate_against_a_rich_yes_price_is_a_no_trade(model, limits) -> None:
    """p = 0.30 against a YES ask of 0.50 is not 'no edge' — it is 20 points on
    NO. This is the case a YES-only gate throws away."""
    b = book(bids=(("0.50", "5000"),), asks=(("0.52", "5000"),))
    result = assess_claim(
        claim(), estimate("0.30"), b, contracts=4,
        fee_model=model, limits=limits, now=NOW,
    )
    assert result.has_trade
    assert result.chosen.side is Side.no
    assert not result.yes.passed
    assert result.no.net_edge_pp > 0


def test_no_side_prices_off_one_minus_the_yes_bid(model, limits) -> None:
    b = book(bids=(("0.50", "5000"),), asks=(("0.52", "5000"),))
    a = assess_side(
        claim(), estimate("0.30"), b, side=Side.no, contracts=4,
        fee_model=model, limits=limits, now=NOW,
    )
    assert a.best_price == D("0.50")  # 1 - 0.50
    assert a.p_side == D("0.70")


def test_rich_estimate_picks_the_yes_side(model, limits) -> None:
    b = book(bids=(("0.30", "5000"),), asks=(("0.32", "5000"),))
    result = assess_claim(
        claim(), estimate("0.70"), b, contracts=4,
        fee_model=model, limits=limits, now=NOW,
    )
    assert result.chosen.side is Side.yes


def test_both_sides_cannot_pass_at_once(model, limits) -> None:
    """Their net edges sum to a negative number — that is just the statement
    that spread and fees are positive."""
    b = book(bids=(("0.48", "5000"),), asks=(("0.52", "5000"),))
    for p in ("0.10", "0.30", "0.50", "0.70", "0.90"):
        result = assess_claim(
            claim(), estimate(p, z=0.5), b, contracts=4,
            fee_model=model, limits=limits, now=NOW,
        )
        assert not (result.yes.passed and result.no.passed)


def test_fair_price_produces_no_trade_on_either_side(model, limits) -> None:
    b = book(bids=(("0.48", "5000"),), asks=(("0.52", "5000"),))
    result = assess_claim(
        claim(), estimate("0.50"), b, contracts=4,
        fee_model=model, limits=limits, now=NOW,
    )
    assert not result.has_trade
    assert result.chosen is None


# ── the gates, individually ────────────────────────────────────────


def gate_names(assessment) -> set[GateName]:
    return {f.gate for f in assessment.failures}


def test_thin_edge_is_rejected(model, limits) -> None:
    b = book(bids=(("0.44", "5000"),), asks=(("0.46", "5000"),))
    a = assess_side(
        claim(), estimate("0.48"), b, side=Side.yes, contracts=4,
        fee_model=model, limits=limits, now=NOW,
    )
    assert a.decision is Decision.reject
    assert GateName.net_edge in gate_names(a)


def test_edge_must_clear_floor_plus_margin(limits) -> None:
    assert limits.required_edge_pp == D("4.0")


def test_empty_side_is_rejected(model, limits) -> None:
    b = BinaryBook(ticker="T", retrieved_at=NOW, yes_bids=[], yes_asks=[])
    a = assess_side(
        claim(), estimate("0.90"), b, side=Side.yes, contracts=4,
        fee_model=model, limits=limits, now=NOW,
    )
    assert GateName.book_present in gate_names(a)
    assert a.execution is None


def test_crossed_book_is_rejected_as_broken_not_traded(model, limits) -> None:
    b = book(bids=(("0.60", "5000"),), asks=(("0.40", "5000"),))
    a = assess_side(
        claim(), estimate("0.95"), b, side=Side.yes, contracts=4,
        fee_model=model, limits=limits, now=NOW,
    )
    assert GateName.book_not_crossed in gate_names(a)


def test_partial_fill_is_rejected(model, limits) -> None:
    a = assess_side(
        claim(), estimate("0.90"), book(asks=(("0.45", "2"),)), side=Side.yes,
        contracts=10, fee_model=model, limits=limits, now=NOW,
    )
    assert GateName.fillable in gate_names(a)


def test_thin_depth_is_rejected(model, limits) -> None:
    a = assess_side(
        claim(), estimate("0.90"), book(asks=(("0.45", "10"),)), side=Side.yes,
        contracts=4, fee_model=model, limits=limits, now=NOW,
    )
    assert GateName.depth_multiple in gate_names(a)


def test_participation_cap_is_enforced(model, limits) -> None:
    # 4 contracts against 30 visible is 13%, over the 10% cap, but depth is
    # still >= 5x the order so only participation should fire.
    a = assess_side(
        claim(), estimate("0.90"), book(asks=(("0.45", "30"),)), side=Side.yes,
        contracts=4, fee_model=model, limits=limits, now=NOW,
    )
    assert GateName.participation in gate_names(a)
    assert GateName.depth_multiple not in gate_names(a)


def test_distant_resolution_is_rejected(model, limits) -> None:
    far = NOW + dt.timedelta(days=30)
    a = assess_side(
        claim(far), estimate("0.90"), book(), side=Side.yes, contracts=4,
        fee_model=model, limits=limits, now=NOW,
    )
    assert GateName.horizon in gate_names(a)


def test_deep_tail_estimate_is_rejected_by_default(model, limits) -> None:
    a = assess_side(
        claim(), estimate("0.98", z=4.5), book(), side=Side.yes, contracts=4,
        fee_model=model, limits=limits, now=NOW,
    )
    assert GateName.model_reliability in gate_names(a)


def test_deep_tail_rejection_can_be_turned_off_explicitly(model, limits) -> None:
    permissive = GateLimits(
        min_net_edge_pp=limits.min_net_edge_pp, margin_pp=limits.margin_pp,
        min_book_depth_multiple=limits.min_book_depth_multiple,
        max_book_participation_pct=limits.max_book_participation_pct,
        max_hours_to_resolution=limits.max_hours_to_resolution,
        reject_deep_tail=False,
    )
    a = assess_side(
        claim(), estimate("0.98", z=4.5), book(asks=(("0.45", "5000"),)), side=Side.yes,
        contracts=4, fee_model=model, limits=permissive, now=NOW,
    )
    assert GateName.model_reliability not in gate_names(a)


# ── reporting behaviour ────────────────────────────────────────────


def test_every_failing_gate_is_reported_not_just_the_first(model, limits) -> None:
    """'failed on depth' and 'failed on depth, horizon and edge' are different
    facts when deciding what to fix."""
    far = NOW + dt.timedelta(days=30)
    a = assess_side(
        claim(far), estimate("0.45"), book(asks=(("0.45", "8"),)), side=Side.yes,
        contracts=4, fee_model=model, limits=limits, now=NOW,
    )
    assert {GateName.depth_multiple, GateName.horizon, GateName.net_edge} <= gate_names(a)


def test_estimate_warnings_propagate_into_the_assessment(model, limits) -> None:
    a = assess_side(
        claim(), estimate("0.60", warnings=["measured reference"]), book(),
        side=Side.yes, contracts=4, fee_model=model, limits=limits, now=NOW,
    )
    assert "measured reference" in a.warnings


def test_passing_assessment_reports_capital_at_risk(model, limits) -> None:
    b = book(bids=(("0.30", "5000"),), asks=(("0.32", "5000"),))
    a = assess_side(
        claim(), estimate("0.70"), b, side=Side.yes, contracts=4,
        fee_model=model, limits=limits, now=NOW,
    )
    assert a.passed
    assert a.capital_at_risk == a.execution.total_cost_dollars
    assert a.expected_value_dollars > 0


def test_assessment_serialises_both_sides(model, limits) -> None:
    d = assess_claim(
        claim(), estimate("0.70"), book(), contracts=4,
        fee_model=model, limits=limits, now=NOW,
    ).as_dict()
    assert d["yes"]["side"] == "yes"
    assert d["no"]["side"] == "no"
    assert "chosen_side" in d
