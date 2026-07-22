"""DataProvider protocol, typed market-data models, and capability gating.

Everything a strategy consumes from the signal layer flows through the models
here. Parsing (raw JSON -> model) lives in each provider module as *pure*
functions so it can be unit-tested without a network. Models reject NaN/inf on
construction; staleness is checked explicitly at decision time via
:func:`assert_fresh`.

The `DataProvider` protocol is deliberately venue/provider-agnostic: a strategy
declares the capabilities it needs and never learns which provider serves them.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Annotated, Protocol, runtime_checkable

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from tradetk.enums import Capability

# ── Errors ─────────────────────────────────────────────────────────


class ProviderError(Exception):
    """Base class for all data-provider failures."""


class ProviderHTTPError(ProviderError):
    """A transport/HTTP failure that survived retries (or a circuit-open)."""


class DataValidationError(ProviderError):
    """A response parsed structurally but failed a sanity check (NaN, ordering)."""


class StaleDataError(ProviderError):
    """Data older than the configured staleness threshold at decision time."""


class CapabilityError(ProviderError):
    """A required capability is not supplied by the configured provider(s)."""


# ── Finite-float guard (reject NaN / inf everywhere) ───────────────


def _finite(v: float) -> float:
    if not math.isfinite(v):
        raise ValueError("value must be finite (got NaN or inf)")
    return v


Finite = Annotated[float, AfterValidator(_finite)]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ── Market-data models ─────────────────────────────────────────────


class PriceSnapshot(_Model):
    symbol: str
    mid: Finite = Field(gt=0)
    retrieved_at: datetime


class Level(_Model):
    px: Finite = Field(gt=0)
    sz: Finite = Field(ge=0)
    n: int = Field(ge=0, description="Number of resting orders at this level.")


class OrderBook(_Model):
    symbol: str
    venue_time_ms: int
    retrieved_at: datetime
    bids: list[Level]  # sorted best (highest px) first
    asks: list[Level]  # sorted best (lowest px) first

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].px if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].px if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.bids and self.asks:
            return (self.bids[0].px + self.asks[0].px) / 2.0
        return None

    @property
    def spread(self) -> float | None:
        if self.bids and self.asks:
            return self.asks[0].px - self.bids[0].px
        return None


class Candle(_Model):
    symbol: str
    interval: str
    open_ms: int
    close_ms: int
    o: Finite = Field(gt=0)
    h: Finite = Field(gt=0)
    l: Finite = Field(gt=0)  # noqa: E741 - OHLC "low"; name mirrors the venue field
    c: Finite = Field(gt=0)
    v: Finite = Field(ge=0)
    trades: int = Field(ge=0)


class FundingPoint(_Model):
    symbol: str
    time_ms: int
    rate: Finite
    premium: Finite | None = None


class RealizedVol(_Model):
    """Close-to-close realized volatility over a lookback window.

    `sigma_period` is the std-dev of per-interval log returns; `sigma_annual`
    scales it to a year. The translation layer rescales to a claim's specific
    time-to-resolution — it must NOT assume any particular horizon here.
    """

    symbol: str
    interval: str
    lookback_days: int
    n_samples: int
    sigma_period: Finite = Field(ge=0)
    sigma_annual: Finite = Field(ge=0)


# ── Interval helpers + realized-vol math (pure, testable) ──────────

INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "8h": 28800, "12h": 43200,
    "1d": 86400, "3d": 259200, "1w": 604800,
}
_SECONDS_PER_YEAR = 365.0 * 24.0 * 3600.0


def realized_vol_from_closes(
    closes: list[float], interval: str, lookback_days: int
) -> RealizedVol:
    """Compute close-to-close realized vol from a list of closing prices.

    Requires >= 2 closes. Uses sample std-dev (ddof=1) of log returns.
    """
    if interval not in INTERVAL_SECONDS:
        raise ValueError(f"unknown interval {interval!r}")
    if len(closes) < 2:
        raise DataValidationError(
            f"need >= 2 closes to compute realized vol, got {len(closes)}"
        )
    if any((not math.isfinite(c)) or c <= 0 for c in closes):
        raise DataValidationError("closes must be finite and positive")

    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1) if n > 1 else 0.0
    sigma_period = math.sqrt(var)
    periods_per_year = _SECONDS_PER_YEAR / INTERVAL_SECONDS[interval]
    sigma_annual = sigma_period * math.sqrt(periods_per_year)
    return RealizedVol(
        symbol="",  # filled in by the provider
        interval=interval,
        lookback_days=lookback_days,
        n_samples=n,
        sigma_period=sigma_period,
        sigma_annual=sigma_annual,
    )


# ── Staleness ──────────────────────────────────────────────────────


def utcnow() -> datetime:
    """UTC now — one seam so tests can monkeypatch a fixed clock."""
    return datetime.now(timezone.utc)


def assert_fresh(retrieved_at: datetime, max_age_seconds: float, *, now: datetime | None = None) -> None:
    """Raise :class:`StaleDataError` if `retrieved_at` is older than the limit."""
    ref = now or utcnow()
    if retrieved_at.tzinfo is None:
        retrieved_at = retrieved_at.replace(tzinfo=timezone.utc)
    age = (ref - retrieved_at).total_seconds()
    if age > max_age_seconds:
        raise StaleDataError(
            f"data is {age:.1f}s old, exceeds staleness limit of {max_age_seconds:.1f}s"
        )


# ── Provider protocol + capability gating ──────────────────────────


@runtime_checkable
class DataProvider(Protocol):
    """Read-only market-data source. Never touches order flow.

    Implementations must be honest about `capabilities()`: a capability listed
    here is a promise the corresponding method returns real data, never zeros or
    stale placeholders.
    """

    name: str

    def capabilities(self) -> set[Capability]: ...

    def mid_price(self, symbol: str) -> PriceSnapshot: ...

    def orderbook(self, symbol: str) -> OrderBook: ...

    def candles(
        self, symbol: str, interval: str, start_ms: int, end_ms: int
    ) -> list[Candle]: ...

    def funding_history(
        self, symbol: str, start_ms: int, end_ms: int
    ) -> list[FundingPoint]: ...

    def current_funding(self, symbol: str) -> FundingPoint: ...

    def realized_vol(
        self, symbol: str, lookback_days: int, interval: str = "1h"
    ) -> RealizedVol: ...


def require_capabilities(provider: DataProvider, needed: set[Capability]) -> None:
    """Fail loudly if `provider` cannot supply every capability in `needed`.

    Called at startup once a strategy declares `required_capabilities()`. Never
    silently degrade — a missing signal halts, it does not substitute zeros.
    """
    missing = needed - provider.capabilities()
    if missing:
        names = ", ".join(sorted(c.value for c in missing))
        raise CapabilityError(
            f"provider {provider.name!r} cannot supply required capabilities: {names}"
        )
