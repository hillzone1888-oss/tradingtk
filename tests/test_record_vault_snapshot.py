# tests/test_record_vault_snapshot.py
"""History has to exist before a backtest can ask for it."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from tradetk.cli.record import _vault_overlay_cfg, capture_vault_snapshot
from tradetk.config.schema import VaultOverlayConfig

NOW = datetime(2026, 8, 4, 22, 0, tzinfo=timezone.utc)


def test_disabled_overlay_captures_nothing() -> None:
    assert capture_vault_snapshot(VaultOverlayConfig(enabled=False), NOW) is None


def test_broken_bridge_does_not_stop_the_recorder() -> None:
    """A dead vault must never cost us the market tape."""
    cfg = VaultOverlayConfig(enabled=True, config_path="nope/missing.yaml")
    assert capture_vault_snapshot(cfg, NOW) is None


def test_a_broken_config_warns_loudly_and_degrades_to_disabled(caplog) -> None:
    """Fail open, never fail silent: a torn config that would have disabled an
    intended overlay must surface at WARNING, not whisper at INFO — otherwise an
    operator can believe their research is steering trades when it is not."""
    with caplog.at_level(logging.WARNING, logger="tradetk.cli.record"):
        cfg = _vault_overlay_cfg("nope/missing.yaml")
    assert cfg.enabled is False
    assert any(
        r.levelno == logging.WARNING and "vault_overlay config unavailable" in r.message
        for r in caplog.records
    )
