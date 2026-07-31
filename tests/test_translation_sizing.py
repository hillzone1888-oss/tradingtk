"""Fixed position sizing.

The arithmetic is small and every line of it can cost money at $2 a position,
so the fee-in-the-divisor behaviour, the integer flooring, and each individual
cap are pinned separately rather than through one end-to-end case.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tradetk.costs.fees import KalshiFeeModel
from tradetk.translation.sizing import (
    SizingCap,
    SizingLimits,
    SizingMode,
    contracts_for_stake,
    plan_size,
)

D = Decimal


@pytest.fixture
def model() -> KalshiFeeModel:
    return KalshiFeeModel()


@pytest.fixture
def limits() -> SizingLimits:
    """The shipped config: $2 target, $3 ceiling, $20 book, 10% participation."""
    return SizingLimits(
        position_target=D("2.00"),
        per_position_ceiling=D("3.00"),
        total_capital=D("20.00"),
        max_book_participation_pct=D("10"),
    )


# ── contracts_for_stake ────────────────────────────────────────────


def test_fee_is_inside_the_divisor() -> None:
    """Sizing on price alone overshoots the dollar target by the fee. At $2 a
    position that is not a rounding detail, so it is pinned."""
    price_only = int(D("2.00") / D("0.37"))
    with_fee = contracts_for_stake(D("2.00"), D("0.37"), D("0.02"))
    assert with_fee <= price_only
    assert with_fee == int(D("2.00") / D("0.39"))


def test_result_is_floored_not_rounded() -> None:
    # 2.00 / 0.30 = 6.67 -> 6, never 7. Rounding up would breach the target.
    assert contracts_for_stake(D("2.00"), D("0.30"), D("0")) == 6


def test_never_returns_zero() -> None:
    """Returning 1 keeps 'too small to bother' distinct from 'too expensive to
    allow' — the caller decides the second."""
    assert contracts_for_stake(D("0.01"), D("0.99"), D("0.01")) == 1


def test_degenerate_price_does_not_divide_by_zero() -> None:
    assert contracts_for_stake(D("2.00"), D("0"), D("0")) == 1


# ── plan_size: the normal path ─────────────────────────────────────


def test_fixed_dollar_target_converts_to_integer_contracts(model, limits) -> None:
    plan = plan_size(D("0.37"), model, limits)
    assert plan.tradeable
    assert plan.contracts == contracts_for_stake(D("2.00"), D("0.37"), plan.fee_per_contract)
    assert plan.binding_cap is SizingCap.dollar_target


def test_quantisation_shortfall_is_reported_not_hidden(model, limits) -> None:
    plan = plan_size(D("0.37"), model, limits)
    assert plan.dollars_deployed == plan.contracts * plan.cost_per_contract
    assert plan.quantisation_shortfall == D("2.00") - plan.dollars_deployed
    assert plan.quantisation_shortfall >= 0


def test_cost_per_contract_includes_the_fee(model, limits) -> None:
    plan = plan_size(D("0.50"), model, limits)
    assert plan.cost_per_contract == plan.price_used + plan.fee_per_contract
    assert plan.fee_per_contract == model.fee(1, D("0.50"))


def test_fixed_contracts_mode_ignores_the_dollar_target(model, limits) -> None:
    fixed = SizingLimits(
        position_target=limits.position_target,
        per_position_ceiling=limits.per_position_ceiling,
        total_capital=limits.total_capital,
        max_book_participation_pct=limits.max_book_participation_pct,
        mode=SizingMode.fixed_contracts,
        fixed_contracts=3,
    )
    plan = plan_size(D("0.10"), model, fixed)
    assert plan.contracts == 3  # a $2 target would have bought ~18
    assert plan.mode is SizingMode.fixed_contracts


# ── each cap, in isolation ─────────────────────────────────────────


def test_one_contract_above_the_ceiling_is_untradeable(model, limits) -> None:
    """No integer size fixes this, so it rejects rather than returning 1."""
    tight = SizingLimits(
        position_target=D("2.00"), per_position_ceiling=D("0.50"),
        total_capital=D("20.00"), max_book_participation_pct=D("10"),
    )
    plan = plan_size(D("0.95"), model, tight)
    assert not plan.tradeable
    assert plan.contracts == 0
    assert plan.binding_cap is SizingCap.per_position_ceiling
    assert "ceiling" in plan.reasons[0]


def test_ceiling_caps_the_count_below_the_dollar_target(model) -> None:
    limits = SizingLimits(
        position_target=D("5.00"), per_position_ceiling=D("1.00"),
        total_capital=D("20.00"), max_book_participation_pct=D("100"),
    )
    plan = plan_size(D("0.20"), model, limits)
    assert plan.tradeable
    assert plan.binding_cap is SizingCap.per_position_ceiling
    assert plan.dollars_deployed <= D("1.00")


def test_remaining_capital_caps_the_count(model, limits) -> None:
    plan = plan_size(D("0.50"), model, limits, capital_in_use=D("19.40"))
    assert plan.binding_cap is SizingCap.remaining_capital
    assert plan.dollars_deployed <= D("0.60")


def test_exhausted_capital_is_untradeable(model, limits) -> None:
    plan = plan_size(D("0.50"), model, limits, capital_in_use=D("20.00"))
    assert not plan.tradeable
    assert plan.contracts == 0


def test_book_participation_caps_the_count(model, limits) -> None:
    """10% of a 12-contract book is 1 contract, however much capital is free."""
    plan = plan_size(D("0.10"), model, limits, book_depth=D("12"))
    assert plan.binding_cap is SizingCap.book_participation
    assert plan.contracts == 1


def test_thin_book_makes_a_market_untradeable(model, limits) -> None:
    plan = plan_size(D("0.10"), model, limits, book_depth=D("5"))
    assert not plan.tradeable
    assert plan.binding_cap is SizingCap.book_participation


def test_deep_book_does_not_bind(model, limits) -> None:
    plan = plan_size(D("0.37"), model, limits, book_depth=D("100000"))
    assert plan.binding_cap is SizingCap.dollar_target


def test_binding_cap_is_the_one_that_actually_bound(model) -> None:
    """Several caps apply at once; the reported one must be the tightest, since
    it is what tells you whether to add capital or widen the filter."""
    limits = SizingLimits(
        position_target=D("2.00"), per_position_ceiling=D("3.00"),
        total_capital=D("20.00"), max_book_participation_pct=D("10"),
    )
    plan = plan_size(D("0.10"), model, limits, book_depth=D("40"), capital_in_use=D("19.00"))
    # $1 left funds ~9 contracts; 10% of 40 is 4. Participation is tighter.
    assert plan.binding_cap is SizingCap.book_participation
    assert plan.contracts == 4


def test_plan_serialises_every_number_that_moved_it(model, limits) -> None:
    d = plan_size(D("0.37"), model, limits, book_depth=D("500")).as_dict()
    for key in (
        "contracts", "tradeable", "mode", "target_contracts", "price_used",
        "fee_per_contract", "cost_per_contract", "dollars_deployed",
        "quantisation_shortfall", "binding_cap", "reasons",
    ):
        assert key in d


def test_never_deploys_more_than_the_ceiling_across_the_price_grid(model, limits) -> None:
    for price in ("0.01", "0.05", "0.20", "0.50", "0.75", "0.95", "0.99"):
        plan = plan_size(D(price), model, limits)
        if plan.tradeable:
            assert plan.dollars_deployed <= limits.per_position_ceiling
