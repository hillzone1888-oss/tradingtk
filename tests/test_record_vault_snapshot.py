# tests/test_record_vault_snapshot.py
"""History has to exist before a backtest can ask for it."""

from __future__ import annotations

from datetime import datetime, timezone

from tradetk.cli.record import capture_vault_snapshot
from tradetk.config.schema import VaultOverlayConfig

NOW = datetime(2026, 8, 4, 22, 0, tzinfo=timezone.utc)


def test_disabled_overlay_captures_nothing() -> None:
    assert capture_vault_snapshot(VaultOverlayConfig(enabled=False), NOW) is None


def test_broken_bridge_does_not_stop_the_recorder() -> None:
    """A dead vault must never cost us the market tape."""
    cfg = VaultOverlayConfig(enabled=True, config_path="nope/missing.yaml")
    assert capture_vault_snapshot(cfg, NOW) is None
