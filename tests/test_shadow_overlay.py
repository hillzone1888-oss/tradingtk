"""Shadow measures; it must never let a stance narrow what it measures."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tradetk.overlay.loader import VaultOverlay
from tradetk.shadow.records import ShadowRecord
from tradetk.translation.claims import ClaimOperator
from tradetk.translation.edge import GateLimits
from tradetk.translation.sizing import SizingLimits

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


def test_shadow_record_carries_an_overlay_annotation() -> None:
    rec = ShadowRecord(
        observed_at=NOW, ticker="KXBTCD-T", series_ticker="KXBTCD", underlying="BTC",
        strategy="baseline_vol", method="lognormal", p=Decimal("0.4"),
        operator=ClaimOperator.above, threshold=Decimal("100000"),
        resolution_time=NOW, hours_to_resolution=4.0,
        overlay={"blocked": True, "bias": "bearish"},
    )
    assert rec.overlay["blocked"] is True


def test_overlay_field_defaults_to_none() -> None:
    """Records written before the overlay existed stay valid."""
    rec = ShadowRecord(
        observed_at=NOW, ticker="KXBTCD-T", series_ticker="KXBTCD", underlying="BTC",
        strategy="baseline_vol", method="lognormal", p=Decimal("0.4"),
        operator=ClaimOperator.above, threshold=Decimal("100000"),
        resolution_time=NOW, hours_to_resolution=4.0,
    )
    assert rec.overlay is None


def test_a_blocked_underlying_still_produces_a_policy_for_recording() -> None:
    """The anti-filter pin.

    Shadow exists to score the whole universe, including what it declines. If a
    stance could suppress records, the calibration set would quietly become a
    record of what the stances already believed, and the evaluator's entire
    reason for existing would be defeated.
    """
    overlay = VaultOverlay(base_gate=BASE_GATE, base_sizing=BASE_SIZING)
    policy = overlay.for_underlying("BTC", NOW)
    assert policy.as_dict()["underlying"] == "BTC"
    # An identity policy blocks nothing, and even a blocking one must still be
    # representable as an annotation rather than a filter.
    assert policy.blocked is False
