"""Backtest acts on the overlay — and must read it as of the replay clock."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tradetk.overlay.loader import VaultOverlay
from tradetk.overlay.policy import build_policy
from tradetk.translation.claims import Claim, ClaimOperator
from tradetk.translation.edge import GateLimits
from tradetk.translation.sizing import SizingLimits
from tradetk.venues.base import Side

T1 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)

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
    def __init__(self, bias="bearish", risk=50) -> None:
        self.bias = bias
        self.effective_risk = risk
        self.underlying = "BTC"
        self.stance = type("S", (), {
            "id": "s1", "max_position_dollars": None, "created": T2,
        })()


def _claim() -> Claim:
    return Claim(
        ticker="KXBTCD-T", series_ticker="KXBTCD", underlying="BTC",
        operator=ClaimOperator.above, resolution_time=T2,
        resolution_source="CF Benchmarks BRTI", rules_primary="rules",
        threshold=Decimal("100000"),
    )


def test_overlay_restricts_the_tradeable_side() -> None:
    overlay = VaultOverlay(
        base_gate=BASE_GATE, base_sizing=BASE_SIZING,
        stances={"BTC": _FakeStance("bearish")}, enabled=True,
    )
    policy = overlay.for_underlying("BTC", T2)
    assert policy.allowed_sides(_claim()) == (Side.no,)


def test_overlay_shrinks_the_position_target() -> None:
    overlay = VaultOverlay(
        base_gate=BASE_GATE, base_sizing=BASE_SIZING,
        stances={"BTC": _FakeStance(risk=25)}, enabled=True,
    )
    policy = overlay.for_underlying("BTC", T2)
    assert policy.sizing_limits.position_target == Decimal("0.50")


def test_an_underlying_with_no_stance_is_untouched() -> None:
    overlay = VaultOverlay(
        base_gate=BASE_GATE, base_sizing=BASE_SIZING,
        stances={"BTC": _FakeStance()}, enabled=True,
    )
    policy = overlay.for_underlying("ETH", T2)
    assert policy.sizing_limits == BASE_SIZING
    assert set(policy.allowed_sides(_claim())) == {Side.yes, Side.no}


def test_policy_built_without_a_stance_matches_the_globals() -> None:
    """The as-of guarantee, expressed at the policy layer.

    When a replay asks for state before a stance existed, vault-post hands back
    no stance at all — and the policy that results must be indistinguishable
    from running with no vault. Anything else would leak a future view into a
    replay of the past.
    """
    policy = build_policy(
        "BTC", stance=None, catalysts=[],
        base_gate=BASE_GATE, base_sizing=BASE_SIZING, now=T1,
    )
    assert policy.gate_limits == BASE_GATE
    assert policy.sizing_limits == BASE_SIZING
    assert policy.blocked is False


# ── the verdict must actually reach the engine ─────────────────────
#
# The tests above pin the policy math; these prove the backtest engine
# consults it. They reuse the engine + tape fixtures from test_backtest —
# pytest's default prepend import mode puts sibling test modules on the
# path, so the bare `import test_backtest` resolves (there is no
# tests/__init__.py). The overlay is built with the engine's OWN limits so
# the identity case is byte-for-byte, matching how the loader is wired live.

from test_backtest import engine as build_engine, replay  # noqa: E402


def test_blocking_overlay_produces_no_trades() -> None:
    eng = build_engine()
    overlay = VaultOverlay(
        base_gate=eng.gate_limits, base_sizing=eng.sizing_limits,
        stances={"BTC": _FakeStance("neutral", 0)}, enabled=True,
    )
    result = build_engine(overlay=overlay).run(replay())
    assert result.trades == []
    assert result.skipped.get("overlay_blocked", 0) >= 1


def test_bearish_overlay_forbids_the_yes_side_in_the_engine() -> None:
    """KXBTCD is a 'greater' (above) market, so bullish is YES. A bearish
    stance must forbid YES — visible as a skip whether or not NO trades, and
    no trade the engine keeps may be on the YES side."""
    eng = build_engine()
    overlay = VaultOverlay(
        base_gate=eng.gate_limits, base_sizing=eng.sizing_limits,
        stances={"BTC": _FakeStance("bearish", 50)}, enabled=True,
    )
    result = build_engine(overlay=overlay).run(replay())
    assert result.skipped.get("overlay_side_forbidden", 0) >= 1
    assert all(t.side is Side.no for t in result.trades)


def test_empty_enabled_overlay_matches_the_no_overlay_run() -> None:
    """An enabled overlay with no mail is byte-identical to no overlay."""
    baseline = build_engine().run(replay())
    eng = build_engine()
    overlay = VaultOverlay(
        base_gate=eng.gate_limits, base_sizing=eng.sizing_limits, enabled=True,
    )
    with_overlay = build_engine(overlay=overlay).run(replay())
    assert len(with_overlay.trades) == len(baseline.trades)
    assert with_overlay.skipped == baseline.skipped
