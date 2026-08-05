"""Which side is the bullish bet depends on the claim's operator, not the side.

Getting this wrong inverts a stance on half the universe, so every operator is
pinned explicitly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tradetk.overlay.direction import bullish_side
from tradetk.translation.claims import Claim, ClaimOperator
from tradetk.venues.base import Side

NOW = datetime(2026, 8, 4, 22, 0, tzinfo=timezone.utc)


def _claim(operator: ClaimOperator, **over) -> Claim:
    base = dict(
        ticker="KXBTCD-T", series_ticker="KXBTCD", underlying="BTC",
        operator=operator, resolution_time=NOW, resolution_source="CF Benchmarks BRTI",
        rules_primary="rules", threshold=Decimal("100000"),
    )
    base.update(over)
    return Claim(**base)


def test_above_claim_is_bullish_on_yes() -> None:
    assert bullish_side(_claim(ClaimOperator.above)) is Side.yes


def test_at_or_above_claim_is_bullish_on_yes() -> None:
    assert bullish_side(_claim(ClaimOperator.at_or_above)) is Side.yes


def test_below_claim_is_bullish_on_NO() -> None:
    """The subtle one: 'BTC below 90k' resolving NO means BTC held up."""
    assert bullish_side(_claim(ClaimOperator.below)) is Side.no


def test_between_claim_is_not_directional() -> None:
    """A range bet is neither bullish nor bearish; a stance must not touch it."""
    claim = _claim(
        ClaimOperator.between, threshold=None,
        lower_bound=Decimal("90000"), upper_bound=Decimal("100000"),
    )
    assert bullish_side(claim) is None
