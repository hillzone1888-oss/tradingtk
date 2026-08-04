"""Models, realized-vol math, staleness, and capability gating."""

from __future__ import annotations

import math
from datetime import timedelta

import pytest
from pydantic import ValidationError

from tradetk.enums import Capability
from tradetk.signals.base import (
    CapabilityError,
    Candle,
    DataValidationError,
    Level,
    StaleDataError,
    assert_fresh,
    realized_vol_from_closes,
    require_capabilities,
    utcnow,
)


def test_finite_guard_rejects_nan_inf() -> None:
    with pytest.raises(ValidationError):
        Level(px=float("nan"), sz=1.0, n=1)
    with pytest.raises(ValidationError):
        Candle(symbol="BTC", interval="1h", open_ms=0, close_ms=1,
               o=1, h=1, l=1, c=float("inf"), v=0, trades=0)


def test_realized_vol_math_known_series() -> None:
    closes = [100.0, 101.0, 100.0, 102.0]
    rv = realized_vol_from_closes(closes, "1h", lookback_days=1)
    assert rv.n_samples == 3
    # sigma_annual scales sigma_period by sqrt(periods per year) for 1h bars.
    ppy = (365 * 24 * 3600) / 3600
    assert math.isclose(rv.sigma_annual, rv.sigma_period * math.sqrt(ppy), rel_tol=1e-9)
    assert rv.sigma_period > 0


def test_realized_vol_flat_series_is_zero() -> None:
    rv = realized_vol_from_closes([50.0, 50.0, 50.0], "1d", lookback_days=3)
    assert rv.sigma_period == 0.0
    assert rv.sigma_annual == 0.0


def test_realized_vol_requires_two_points() -> None:
    with pytest.raises(DataValidationError):
        realized_vol_from_closes([100.0], "1h", lookback_days=1)


def test_realized_vol_rejects_bad_interval() -> None:
    with pytest.raises(ValueError, match="interval"):
        realized_vol_from_closes([1.0, 2.0], "7h", lookback_days=1)


def test_assert_fresh_pass_and_fail() -> None:
    now = utcnow()
    assert_fresh(now - timedelta(seconds=10), max_age_seconds=90, now=now)  # ok
    with pytest.raises(StaleDataError, match="stale"):
        assert_fresh(now - timedelta(seconds=200), max_age_seconds=90, now=now)


class _FakeProvider:
    name = "fake"

    def __init__(self, caps: set[Capability]) -> None:
        self._caps = caps

    def capabilities(self) -> set[Capability]:
        return self._caps


def test_require_capabilities_passes_when_covered() -> None:
    p = _FakeProvider({Capability.SPOT_PRICE, Capability.ORDERBOOK})
    require_capabilities(p, {Capability.SPOT_PRICE})  # no raise


def test_require_capabilities_fails_loudly_on_missing() -> None:
    p = _FakeProvider({Capability.SPOT_PRICE})
    with pytest.raises(CapabilityError, match="funding"):
        require_capabilities(p, {Capability.SPOT_PRICE, Capability.FUNDING})
