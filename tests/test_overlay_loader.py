# tests/test_overlay_loader.py
"""A broken bridge must trade normally AND say so. Never fail silent."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tradetk.config.schema import VaultOverlayConfig
from tradetk.overlay.loader import load_overlay
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


def _load(cfg: VaultOverlayConfig):
    return load_overlay(cfg, base_gate=BASE_GATE, base_sizing=BASE_SIZING)


def test_disabled_overlay_is_a_no_op() -> None:
    overlay = _load(VaultOverlayConfig(enabled=False))
    policy = overlay.for_underlying("BTC", NOW)
    assert policy.blocked is False
    assert policy.sizing_limits == BASE_SIZING
    assert overlay.ok is True  # disabled on purpose is not a failure


def test_missing_config_fails_open() -> None:
    """A broken bridge must not stop trading — the pipeline is safe alone."""
    overlay = _load(VaultOverlayConfig(enabled=True, config_path="nope/missing.yaml"))
    policy = overlay.for_underlying("BTC", NOW)
    assert policy.blocked is False
    assert policy.sizing_limits == BASE_SIZING


def test_missing_config_is_reported_loudly() -> None:
    """Fail-open, never fail-silent: the operator must be able to see it."""
    overlay = _load(VaultOverlayConfig(enabled=True, config_path="nope/missing.yaml"))
    assert overlay.ok is False
    assert overlay.error
    assert overlay.as_dict()["ok"] is False
