"""The book-level risk gate: pure decisions over an immutable snapshot.

These are known-answer tests. The boundary operators matter — a slot cap that
admits one position too many, or a capital cap off by a cent, is a real-money
error that no downstream test would catch — so the exact `>=` / `>` edges are
pinned here.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tradetk.risk import (
    OpenRisk,
    RiskLimits,
    RiskState,
    screen_cost,
    screen_new_entry,
)

D = Decimal

LIMITS = RiskLimits(max_positions=3, max_slots_per_underlying=2, total_capital=D("20.00"))


def _state(*positions: tuple[str, str, str]) -> RiskState:
    return RiskState(
        open=tuple(OpenRisk(t, u, D(c)) for t, u, c in positions)
    )


# ── slot cap ───────────────────────────────────────────────────────


def test_empty_book_admits() -> None:
    decision = screen_new_entry("BTC", RiskState(), LIMITS)
    assert decision.admitted is True
    assert decision.reason is None


def test_a_full_book_is_refused_a_new_slot() -> None:
    state = _state(("A", "BTC", "2"), ("B", "ETH", "2"), ("C", "SOL", "2"))
    decision = screen_new_entry("DOGE", state, LIMITS)
    assert decision.admitted is False
    assert decision.reason == "no_free_slot"


def test_the_slot_cap_binds_at_greater_or_equal() -> None:
    """3 open against a cap of 3 must refuse, not admit a fourth."""
    state = _state(("A", "BTC", "2"), ("B", "ETH", "2"), ("C", "SOL", "2"))
    assert screen_new_entry("BTC", state, LIMITS).reason == "no_free_slot"


# ── per-underlying concentration ───────────────────────────────────


def test_an_underlying_at_its_cap_is_refused() -> None:
    state = _state(("A", "BTC", "2"), ("B", "BTC", "2"))
    decision = screen_new_entry("BTC", state, LIMITS)
    assert decision.admitted is False
    assert decision.reason == "underlying_concentration_limit"


def test_a_different_underlying_is_still_admitted() -> None:
    state = _state(("A", "BTC", "2"), ("B", "BTC", "2"))
    assert screen_new_entry("ETH", state, LIMITS).admitted is True


def test_the_slot_cap_is_checked_before_concentration() -> None:
    """A full book reports no_free_slot even if the underlying also maxed —
    the order the engine records reasons in must not change."""
    full = RiskLimits(max_positions=2, max_slots_per_underlying=2, total_capital=D("20.00"))
    state = _state(("A", "BTC", "2"), ("B", "BTC", "2"))
    assert screen_new_entry("BTC", state, full).reason == "no_free_slot"


# ── capital cap ────────────────────────────────────────────────────


def test_a_cost_within_remaining_capital_is_admitted() -> None:
    state = _state(("A", "BTC", "18.00"))
    assert screen_cost(D("2.00"), state, LIMITS).admitted is True


def test_a_cost_that_exceeds_remaining_capital_is_refused() -> None:
    state = _state(("A", "BTC", "18.50"))
    decision = screen_cost(D("2.00"), state, LIMITS)
    assert decision.admitted is False
    assert decision.reason == "insufficient_capital"


def test_spending_the_book_to_the_penny_is_allowed() -> None:
    """The capital cap binds at strictly greater-than: exactly total is fine."""
    state = _state(("A", "BTC", "18.00"))
    assert screen_cost(D("2.00"), state, LIMITS).admitted is True


def test_one_cent_over_the_book_is_refused() -> None:
    state = _state(("A", "BTC", "18.00"))
    assert screen_cost(D("2.01"), state, LIMITS).reason == "insufficient_capital"


def test_a_negative_cost_is_a_programming_error() -> None:
    with pytest.raises(AssertionError):
        screen_cost(D("-0.01"), RiskState(), LIMITS)


# ── state helpers ──────────────────────────────────────────────────


def test_state_reports_slots_and_capital() -> None:
    state = _state(("A", "BTC", "2.00"), ("B", "BTC", "3.00"), ("C", "ETH", "1.50"))
    assert state.slots_used == 3
    assert state.slots_for("BTC") == 2
    assert state.slots_for("ETH") == 1
    assert state.capital_deployed == D("6.50")


def test_an_empty_state_deploys_zero_capital() -> None:
    assert RiskState().capital_deployed == D("0")


# ── limits from config ─────────────────────────────────────────────


def test_limits_are_read_from_config() -> None:
    """from_config reads config.capital.* and coerces total_capital to Decimal,
    matching how SizingLimits.from_config reads the same block."""
    from types import SimpleNamespace

    config = SimpleNamespace(capital=SimpleNamespace(
        max_positions=6, max_slots_per_underlying=2, total_capital=20.0,
    ))
    limits = RiskLimits.from_config(config)
    assert limits.max_positions == 6
    assert limits.max_slots_per_underlying == 2
    assert limits.total_capital == D("20.0")
    assert isinstance(limits.total_capital, Decimal)
