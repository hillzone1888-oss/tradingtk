"""Every dial narrows. No mail must be a byte-for-byte no-op."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from vaultpost.schema import Bias, Catalyst

from tradetk.overlay.policy import build_policy
from tradetk.translation.claims import Claim, ClaimOperator
from tradetk.translation.edge import GateLimits
from tradetk.translation.sizing import SizingLimits
from tradetk.venues.base import Side

NOW = datetime(2026, 8, 4, 22, 0, tzinfo=timezone.utc)

BASE_GATE = GateLimits(
    min_net_edge_pp=Decimal("3.0"), margin_pp=Decimal("1.0"),
    min_book_depth_multiple=Decimal("5.0"), max_book_participation_pct=Decimal("10.0"),
    max_hours_to_resolution=Decimal("168"),
)
BASE_SIZING = SizingLimits(
    position_target=Decimal("2.00"), per_position_ceiling=Decimal("3.00"),
    total_capital=Decimal("20.00"), max_book_participation_pct=Decimal("10.0"),
)


class _FakeStance:
    """Stands in for vaultpost.ApprovedStance without needing a vault."""

    def __init__(self, bias: Bias, effective_risk: int, max_dollars=None) -> None:
        self.bias = bias.value
        self.effective_risk = effective_risk
        self.stance = type("S", (), {
            "id": "stance-btc-a", "max_position_dollars": max_dollars,
        })()


def _catalyst(action: str, *, start_offset_h: float, end_offset_h: float,
              margin=2.0) -> Catalyst:
    return Catalyst.model_validate({
        "id": "cat-fomc", "type": "catalyst", "from_agent": "daily-sweep",
        "created": NOW, "status": "approved", "review_by": "2026-12-31",
        "underlyings": ["BTC"], "event": "FOMC",
        "window_start": NOW + timedelta(hours=start_offset_h),
        "window_end": NOW + timedelta(hours=end_offset_h),
        "action": action,
        **({"extra_margin_pp": margin} if action == "widen_edge" else {}),
        "evidence": [{
            "class": "event", "claim": "FOMC", "source_tier": "primary",
            "source_url": "https://federalreserve.gov/x",
            "datum": {"value": "x", "unit": "date", "date": "2026-08-04"},
            "observed_at": NOW,
        }],
    })


def _claim(operator=ClaimOperator.above) -> Claim:
    return Claim(
        ticker="KXBTCD-T", series_ticker="KXBTCD", underlying="BTC",
        operator=operator, resolution_time=NOW, resolution_source="CF Benchmarks BRTI",
        rules_primary="rules", threshold=Decimal("100000"),
    )


def _policy(stance=None, catalysts=()):
    return build_policy(
        "BTC", stance=stance, catalysts=list(catalysts),
        base_gate=BASE_GATE, base_sizing=BASE_SIZING, now=NOW,
    )


# ── the identity case ──────────────────────────────────────────────


def test_no_mail_is_a_no_op() -> None:
    """An empty vault must leave the pipeline exactly as it was."""
    p = _policy()
    assert p.blocked is False
    assert p.bias is None
    assert p.gate_limits == BASE_GATE
    assert p.sizing_limits == BASE_SIZING


def test_no_mail_allows_both_sides() -> None:
    assert set(_policy().allowed_sides(_claim())) == {Side.yes, Side.no}


# ── bias restricts the side, through the operator ──────────────────


def test_bearish_stance_allows_only_no_on_an_above_claim() -> None:
    p = _policy(_FakeStance(Bias.bearish, 50))
    assert p.allowed_sides(_claim(ClaimOperator.above)) == (Side.no,)


def test_bearish_stance_allows_only_YES_on_a_below_claim() -> None:
    """The inversion case: 'BTC below 90k' resolving YES is the bearish bet."""
    p = _policy(_FakeStance(Bias.bearish, 50))
    assert p.allowed_sides(_claim(ClaimOperator.below)) == (Side.yes,)


def test_bullish_stance_allows_only_yes_on_an_above_claim() -> None:
    p = _policy(_FakeStance(Bias.bullish, 50))
    assert p.allowed_sides(_claim(ClaimOperator.above)) == (Side.yes,)


def test_neutral_stance_allows_both_sides() -> None:
    """neutral is 'no directional view', never a brake."""
    p = _policy(_FakeStance(Bias.neutral, 50))
    assert set(p.allowed_sides(_claim())) == {Side.yes, Side.no}


def test_directional_stance_leaves_between_claims_alone() -> None:
    claim = Claim(
        ticker="KXBTCD-R", series_ticker="KXBTCD", underlying="BTC",
        operator=ClaimOperator.between, resolution_time=NOW,
        resolution_source="CF Benchmarks BRTI", rules_primary="rules",
        lower_bound=Decimal("90000"), upper_bound=Decimal("100000"),
    )
    p = _policy(_FakeStance(Bias.bearish, 50))
    assert set(p.allowed_sides(claim)) == {Side.yes, Side.no}


# ── risk scales the size ───────────────────────────────────────────


def test_risk_scales_the_position_target() -> None:
    p = _policy(_FakeStance(Bias.bearish, 50))
    assert p.sizing_limits.position_target == Decimal("1.00")


def test_risk_zero_blocks() -> None:
    p = _policy(_FakeStance(Bias.neutral, 0))
    assert p.blocked is True


def test_max_position_dollars_is_a_ceiling_not_a_floor() -> None:
    """The per-stance cap may only shrink the target."""
    p = _policy(_FakeStance(Bias.bearish, 100, max_dollars=0.75))
    assert p.sizing_limits.position_target == Decimal("0.75")


def test_max_position_dollars_never_raises_the_target() -> None:
    p = _policy(_FakeStance(Bias.bearish, 25, max_dollars=99.0))
    assert p.sizing_limits.position_target == Decimal("0.50")


# ── catalysts gate the edge ────────────────────────────────────────


def test_catalyst_raises_required_edge_inside_its_window() -> None:
    p = _policy(catalysts=[_catalyst("widen_edge", start_offset_h=-1, end_offset_h=1)])
    assert p.gate_limits.required_edge_pp == BASE_GATE.required_edge_pp + Decimal("2.0")


def test_catalyst_does_nothing_outside_its_window() -> None:
    p = _policy(catalysts=[_catalyst("widen_edge", start_offset_h=48, end_offset_h=50)])
    assert p.gate_limits == BASE_GATE


def test_blocking_catalyst_blocks_inside_its_window() -> None:
    p = _policy(catalysts=[_catalyst("block", start_offset_h=-1, end_offset_h=1)])
    assert p.blocked is True


# ── provenance ─────────────────────────────────────────────────────


def test_policy_names_the_mail_that_moved_a_number() -> None:
    p = _policy(_FakeStance(Bias.bearish, 40))
    assert "stance-btc-a" in p.source_mail
    assert any("40" in r for r in p.reasons)
