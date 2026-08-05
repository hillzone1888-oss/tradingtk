"""The overlay is opt-in and defaults to off, so nothing changes until asked."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tradetk.config.schema import VaultOverlayConfig


def test_defaults_to_disabled() -> None:
    """An unconfigured install must behave exactly as it does today."""
    cfg = VaultOverlayConfig()
    assert cfg.enabled is False


def test_default_path_points_at_the_sibling_repo() -> None:
    assert VaultOverlayConfig().config_path.endswith("config.yaml")


def test_unknown_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        VaultOverlayConfig.model_validate({"enabled": True, "typo_key": 1})
