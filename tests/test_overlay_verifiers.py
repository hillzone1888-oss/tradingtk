"""Evidence that cannot be reproduced does not score."""

from __future__ import annotations

from tradetk.overlay.verifiers import build_registry
from tradetk.signals.base import Candle


def _candle(close: float) -> Candle:
    return Candle(
        symbol="BTC",
        interval="15m",
        open_ms=1_700_000_000_000,
        close_ms=1_700_000_900_000,
        o=close,
        h=close,
        l=close,
        c=close,
        v=1.0,
        trades=1,
    )


class _FakeProvider:
    def __init__(self, *, spot=100_000.0, vol=0.55, boom=False) -> None:
        self._spot, self._vol, self._boom = spot, vol, boom

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def candles(self, symbol, interval, start_ms, end_ms):
        if self._boom:
            raise RuntimeError("provider down")
        return [_candle(self._spot)]

    def realized_vol(self, symbol, lookback_days, interval="1h"):
        if self._boom:
            raise RuntimeError("provider down")
        return type("RV", (), {"sigma_annual": self._vol})()


def _reg(**kw):
    return build_registry(provider_factory=lambda: _FakeProvider(**kw))


def test_spot_within_tolerance_verifies() -> None:
    fn = _reg(spot=100_000.0).get("tradetk.spot")
    assert fn({"symbol": "BTC", "tolerance_pct": 1.0}, 100_400.0) is True


def test_spot_outside_tolerance_fails() -> None:
    """A number that does not reproduce must never pass silently."""
    fn = _reg(spot=100_000.0).get("tradetk.spot")
    assert fn({"symbol": "BTC", "tolerance_pct": 1.0}, 120_000.0) is False


def test_realized_vol_within_tolerance_verifies() -> None:
    fn = _reg(vol=0.55).get("tradetk.realized_vol")
    assert fn({"symbol": "BTC", "lookback_days": 30, "tolerance_pct": 10.0}, 0.57) is True


def test_unreachable_provider_fails_closed() -> None:
    """Evidence that could not be checked is not evidence."""
    fn = _reg(boom=True).get("tradetk.spot")
    assert fn({"symbol": "BTC"}, 100_000.0) is False


def test_all_four_verifiers_are_registered() -> None:
    reg = _reg()
    for name in ("tradetk.spot", "tradetk.realized_vol",
                 "tradetk.price_change_pct", "tradetk.funding"):
        assert name in reg
