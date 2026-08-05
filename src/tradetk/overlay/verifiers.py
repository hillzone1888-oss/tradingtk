"""Recomputing the technical evidence a stance cites.

vault-post defines the verifier contract and knows nothing about market data;
this module supplies the implementations, because tradetk is what owns the data.

Every verifier answers one question: does the live number reproduce the claimed
one, within tolerance? Anything it cannot check — an unreachable provider, an
unknown symbol, a malformed parameter — answers ``False``. Evidence that could
not be verified must not score, because "I could not check" and "I checked and
it holds" are not the same claim.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from vaultpost import VerifierRegistry

from tradetk.signals.hyperliquid import HyperliquidProvider

log = logging.getLogger("tradetk.overlay.verifiers")

DEFAULT_TOLERANCE_PCT = 2.0


def _within(actual: float, claimed: float, tolerance_pct: float) -> bool:
    if actual == 0:
        return abs(claimed) <= 1e-9
    return abs(actual - claimed) / abs(actual) * 100.0 <= tolerance_pct


def _guard(fn: Callable[..., bool]) -> Callable[[dict, float], bool]:
    """Any failure to check is a failure to verify."""

    def wrapped(params: dict, value: float) -> bool:
        try:
            return bool(fn(params, float(value)))
        except Exception as exc:  # noqa: BLE001 - unverifiable is not verified
            log.info("verifier could not check evidence: %s", exc)
            return False

    return wrapped


def build_registry(provider_factory: Any | None = None) -> VerifierRegistry:
    """Register every verifier tradetk can back with real data."""
    factory = provider_factory or HyperliquidProvider
    reg = VerifierRegistry()

    def spot(params: dict, value: float) -> bool:
        # HyperliquidProvider has no spot_price method; everywhere else in this
        # codebase current spot is the close of the most recently *closed*
        # candle (see CandleSeries.spot_at), so reproduce that convention here
        # with a short recent window.
        # Tolerance param: params["tolerance_pct"] — percent, default DEFAULT_TOLERANCE_PCT.
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=3)
        with factory() as p:
            candles = p.candles(
                params["symbol"], params.get("interval", "5m"),
                int(start.timestamp() * 1000), int(end.timestamp() * 1000),
            )
        if not candles:
            return False
        latest = sorted(candles, key=lambda k: k.open_ms)[-1]
        actual = float(latest.c)
        return _within(actual, value, params.get("tolerance_pct", DEFAULT_TOLERANCE_PCT))

    def realized_vol(params: dict, value: float) -> bool:
        # Tolerance param: params["tolerance_pct"] — percent, default 10.0.
        with factory() as p:
            rv = p.realized_vol(params["symbol"], int(params.get("lookback_days", 30)))
        return _within(
            float(rv.sigma_annual), value, params.get("tolerance_pct", 10.0)
        )

    def price_change_pct(params: dict, value: float) -> bool:
        # Tolerance param: params["tolerance_pp"] — percentage POINTS (not
        # percent-of-actual like the others), default 1.0.
        hours = float(params.get("hours", 24))
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        with factory() as p:
            candles = p.candles(
                params["symbol"], params.get("interval", "1h"),
                int(start.timestamp() * 1000), int(end.timestamp() * 1000),
            )
        if len(candles) < 2:
            return False
        rows = sorted(candles, key=lambda k: k.open_ms)
        first, last = float(rows[0].o), float(rows[-1].c)
        if first == 0:
            return False
        actual = (last - first) / first * 100.0
        return abs(actual - value) <= float(params.get("tolerance_pp", 1.0))

    def funding(params: dict, value: float) -> bool:
        # Tolerance param: params["tolerance"] — absolute funding-rate units
        # (not percent, not pp), default 0.0001.
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=float(params.get("hours", 8)))
        with factory() as p:
            points = p.funding_history(
                params["symbol"], int(start.timestamp() * 1000),
                int(end.timestamp() * 1000),
            )
        if not points:
            return False
        latest = sorted(points, key=lambda f: f.time_ms)[-1]
        return abs(float(latest.rate) - value) <= float(params.get("tolerance", 0.0001))

    reg.register("tradetk.spot", _guard(spot))
    reg.register("tradetk.realized_vol", _guard(realized_vol))
    reg.register("tradetk.price_change_pct", _guard(price_change_pct))
    reg.register("tradetk.funding", _guard(funding))
    return reg
