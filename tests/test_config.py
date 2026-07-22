"""Config schema tests — the safety gates and dollar-ordering invariants.

These guard the flags that keep `execute` and production behind two independent
switches each. If one of these fails, a safety boundary has regressed.
"""

from __future__ import annotations


import pytest
import yaml
from pydantic import ValidationError

from tradetk.config.loader import load_config
from tradetk.config.schema import Config

EXAMPLE = "config/config.example.yaml"


def _base() -> dict:
    with open(EXAMPLE, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_example_config_is_valid() -> None:
    cfg = load_config(EXAMPLE)
    assert isinstance(cfg, Config)
    assert cfg.capital.total_capital == 20.0
    assert cfg.venue.name.value == "kalshi"
    assert cfg.venue.environment.value == "demo"


def test_live_requires_confirmation_flag() -> None:
    raw = _base()
    raw["mode"] = "live"  # but live_trading_confirmed stays false
    with pytest.raises(ValidationError, match="live_trading_confirmed"):
        Config.model_validate(raw)


def test_live_with_both_flags_ok() -> None:
    raw = _base()
    raw["mode"] = "live"
    raw["live_trading_confirmed"] = True
    assert Config.model_validate(raw).mode.value == "live"


def test_prod_requires_use_production_flag() -> None:
    raw = _base()
    raw["venue"]["environment"] = "prod"  # use_production stays false
    with pytest.raises(ValidationError, match="use_production"):
        Config.model_validate(raw)


def test_dollar_ordering_enforced() -> None:
    raw = _base()
    raw["capital"]["per_position_ceiling"] = 1.0  # < position_target (2.0)
    with pytest.raises(ValidationError, match="per_position_ceiling"):
        Config.model_validate(raw)


def test_crossing_needs_explicit_flag() -> None:
    raw = _base()
    raw["orders"]["prefer_maker"] = False  # implies crossing, but flag is false
    with pytest.raises(ValidationError, match="allow_crossing"):
        Config.model_validate(raw)


def test_unknown_key_rejected() -> None:
    raw = _base()
    raw["capital"]["totl_capital"] = 20.0  # typo
    with pytest.raises(ValidationError):
        Config.model_validate(raw)


def test_slots_per_underlying_bounded_by_max_positions() -> None:
    raw = _base()
    raw["capital"]["max_slots_per_underlying"] = 99
    with pytest.raises(ValidationError, match="max_slots_per_underlying"):
        Config.model_validate(raw)
