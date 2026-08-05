"""Evidence that cannot be reproduced does not score."""

from __future__ import annotations

from tradetk.overlay.verifiers import build_registry
from tradetk.signals.base import Candle, FundingPoint


def _candle(close: float, *, open_: float | None = None, open_ms: int = 1_700_000_000_000) -> Candle:
    o = open_ if open_ is not None else close
    return Candle(
        symbol="BTC",
        interval="15m",
        open_ms=open_ms,
        close_ms=open_ms + 900_000,
        o=o,
        h=max(o, close),
        l=min(o, close),
        c=close,
        v=1.0,
        trades=1,
    )


def _funding(rate: float, time_ms: int) -> FundingPoint:
    return FundingPoint(symbol="BTC", time_ms=time_ms, rate=rate)


class _FakeProvider:
    def __init__(
        self,
        *,
        spot=100_000.0,
        vol=0.55,
        boom=False,
        price_candles=None,
        funding_points=None,
    ) -> None:
        self._spot = spot
        self._vol = vol
        self._boom = boom
        # None means "use the single-candle default derived from `spot`" — the
        # price_change_pct tests override this with an explicit multi-candle
        # series; funding_points defaults to empty (no funding history).
        self._price_candles = price_candles
        self._funding_points = funding_points if funding_points is not None else []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def candles(self, symbol, interval, start_ms, end_ms):
        if self._boom:
            raise RuntimeError("provider down")
        if self._price_candles is not None:
            return self._price_candles
        return [_candle(self._spot)]

    def funding_history(self, symbol, start_ms, end_ms):
        if self._boom:
            raise RuntimeError("provider down")
        return self._funding_points

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


def test_missing_symbol_param_fails_closed() -> None:
    """A malformed/missing param is unverifiable, not verified."""
    fn = _reg().get("tradetk.spot")
    assert fn({}, 100_000.0) is False


# ── price_change_pct ────────────────────────────────────────────────
#
# `first == 0` (a zero open price) is not constructible via a real Candle:
# the model enforces `o: Finite = Field(gt=0)`, so that branch is exercised
# structurally by the `len(candles) < 2` guard tests below and by code
# inspection, not by a zero-open fixture — building one would mean weakening
# the model to serve the test, which the model is right to forbid.


def test_price_change_pct_within_tolerance_verifies() -> None:
    series = [_candle(110.0, open_=100.0, open_ms=1_700_000_000_000),
              _candle(120.0, open_=110.0, open_ms=1_700_003_600_000)]
    fn = _reg(price_candles=series).get("tradetk.price_change_pct")
    # first .o = 100.0, last .c = 120.0 -> +20.0%
    assert fn({"symbol": "BTC", "tolerance_pp": 1.0}, 20.0) is True


def test_price_change_pct_outside_tolerance_fails() -> None:
    series = [_candle(110.0, open_=100.0, open_ms=1_700_000_000_000),
              _candle(120.0, open_=110.0, open_ms=1_700_003_600_000)]
    fn = _reg(price_candles=series).get("tradetk.price_change_pct")
    # actual is +20.0%; claiming +5.0% is off by 15pp, well outside tolerance.
    assert fn({"symbol": "BTC", "tolerance_pp": 1.0}, 5.0) is False


def test_price_change_pct_fewer_than_two_candles_fails_closed() -> None:
    fn = _reg(price_candles=[_candle(100.0)]).get("tradetk.price_change_pct")
    assert fn({"symbol": "BTC"}, 0.0) is False


def test_price_change_pct_unreachable_provider_fails_closed() -> None:
    fn = _reg(boom=True).get("tradetk.price_change_pct")
    assert fn({"symbol": "BTC"}, 0.0) is False


# ── funding ──────────────────────────────────────────────────────────


def test_funding_within_tolerance_verifies() -> None:
    points = [_funding(0.0001, 1_700_000_000_000), _funding(0.0005, 1_700_003_600_000)]
    fn = _reg(funding_points=points).get("tradetk.funding")
    # latest by time_ms is the second point, rate 0.0005.
    assert fn({"symbol": "BTC", "tolerance": 0.0001}, 0.0005) is True


def test_funding_outside_tolerance_fails() -> None:
    points = [_funding(0.0001, 1_700_000_000_000), _funding(0.0005, 1_700_003_600_000)]
    fn = _reg(funding_points=points).get("tradetk.funding")
    assert fn({"symbol": "BTC", "tolerance": 0.0001}, 0.0020) is False


def test_funding_empty_points_fails_closed() -> None:
    fn = _reg(funding_points=[]).get("tradetk.funding")
    assert fn({"symbol": "BTC"}, 0.0001) is False


def test_funding_unreachable_provider_fails_closed() -> None:
    fn = _reg(boom=True).get("tradetk.funding")
    assert fn({"symbol": "BTC"}, 0.0001) is False
