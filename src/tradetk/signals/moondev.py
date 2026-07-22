"""Moon Dev data provider — thin typed client (read-only, never signs anything).

Verified live against ``https://api.moondev.com``:
* base URL is ``api.moondev.com`` (NOT ``www.moondev.com`` — that 404s the API)
* ``GET /api/poly/health`` and ``GET /health`` are public (no key)
* data endpoints require an ``X-API-Key`` header; without it they return
  ``401 {"error": "Unauthorized", "message": "Valid API key required"}``

Scope of this step (built from the endpoint catalog in the project spec):
* the **Polymarket flow** family (``/api/poly/*``) is implemented and typed.
* the HL-derived signal endpoints (liquidations, HLP sentiment, position
  snapshots, smart-money, order-flow) are **not** implemented here — their exact
  paths/shapes are not in the material provided, so they are deliberately **not
  advertised** in ``capabilities()``. A strategy requiring them will fail loudly
  via ``require_capabilities`` rather than get silent zeros.

⚠️ The Polymarket flow is **Polymarket GLOBAL** data (wallet-based, a separate
exchange from the US venue we trade). Treat it as an external sentiment signal,
never as a price available to us. See the mapping requirement in the spec.

The data models below are typed from the spec's documented field lists and are
marked for live re-verification once an API key is available; the public health
model was captured from a live call.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Any

import httpx
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from tradetk.enums import Capability
from tradetk.signals.base import ProviderError, ProviderHTTPError
from tradetk.signals.ratelimit import RateLimiter
from tradetk.signals.resilience import CircuitBreaker, RetryPolicy, call_resilient

log = logging.getLogger("tradetk.signals.moondev")

DEFAULT_BASE_URL = "https://api.moondev.com"

# Documented tier row caps (spec). We clamp requested limits to these and log
# the truncation — never silently drop rows.
TIER_CAPS: dict[str, dict[str, int]] = {
    "standard": {"whales": 250, "leaderboard": 50, "profitable_traders": 25},
    "qe": {"whales": 5000, "leaderboard": 5000, "profitable_traders": 5000},
}


class MoonDevError(ProviderError):
    """Base for Moon Dev provider failures."""


class MoonDevAuthError(MoonDevError):
    """Missing/invalid API key (HTTP 401/403)."""


# ── Finite guard (reused shape) ────────────────────────────────────


def _finite(v: float) -> float:
    import math

    if not math.isfinite(v):
        raise ValueError("value must be finite")
    return v


Finite = Annotated[float, AfterValidator(_finite)]


class _MDModel(BaseModel):
    # External, evolving API: ignore unknown fields (do NOT forbid) so a new
    # response field is forward-compatible instead of a crash. Floats are still
    # NaN/inf-guarded.
    model_config = ConfigDict(frozen=True, extra="ignore")


def _epoch_to_dt(ts: float) -> datetime:
    """Interpret an epoch timestamp that may be in seconds or milliseconds."""
    seconds = ts / 1000.0 if ts > 1e12 else float(ts)
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


# ── Typed models (poly family) ─────────────────────────────────────


class PolyHealth(_MDModel):
    """Public health payload — captured live, so this shape is verified."""

    status: str
    uptime_minutes: float | None = None
    queue_depth: int | None = None
    wallets_checked: int | None = None
    profitable_traders: int | None = None
    seen_wallets: int | None = None


class PolyWhaleTrade(_MDModel):
    """A single whale fill >= $1,000 (Polymarket GLOBAL). Fields per spec."""

    ts: int
    wallet: str
    market_title: str
    market_slug: str
    event_slug: str | None = None
    outcome: str
    side: str
    price: Finite = Field(ge=0, le=1, description="Global implied prob 0..1.")
    size: Finite = Field(ge=0)
    usd_amount: Finite = Field(gt=0)
    tx_hash: str | None = None

    @property
    def when(self) -> datetime:
        return _epoch_to_dt(self.ts)


class PolyTopTrader(_MDModel):
    wallet: str
    trade_count: int
    total_volume: Finite = Field(ge=0)
    biggest_trade: Finite = Field(ge=0)
    markets_traded: int
    last_trade_ts: int


class PolyTopMarket(_MDModel):
    market_title: str | None = None
    market_slug: str | None = None
    whale_trades: int
    whale_volume: Finite = Field(ge=0)
    unique_whales: int
    biggest_trade: Finite = Field(ge=0)


class PolyDailyRollup(_MDModel):
    day: str
    whale_trades: int | None = None
    whale_volume: Finite | None = None
    unique_whales: int | None = None


class ProfitableTrader(_MDModel):
    """Wallets with >= $300 7-day P&L (spec). Global data."""

    wallet: str
    pnl_7d: Finite
    volume_7d: Finite = Field(ge=0)
    trades_7d: int
    redeems_7d: int | None = None
    source: str | None = None


# ── Pure parsers (no IO) ───────────────────────────────────────────


def _as_rows(payload: Any) -> list[dict[str, Any]]:
    """Accept either a bare list or a dict wrapping a data array."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "results", "whales", "traders", "markets", "rows"):
            if isinstance(payload.get(key), list):
                return payload[key]
    raise MoonDevError(f"unexpected payload shape: {type(payload).__name__}")


def parse_whales(payload: Any) -> list[PolyWhaleTrade]:
    return [PolyWhaleTrade(**r) for r in _as_rows(payload)]


def parse_top_traders(payload: Any) -> list[PolyTopTrader]:
    return [PolyTopTrader(**r) for r in _as_rows(payload)]


def parse_top_markets(payload: Any) -> list[PolyTopMarket]:
    return [PolyTopMarket(**r) for r in _as_rows(payload)]


def parse_daily(payload: Any) -> list[PolyDailyRollup]:
    return [PolyDailyRollup(**r) for r in _as_rows(payload)]


def parse_profitable_traders(payload: Any) -> list[ProfitableTrader]:
    return [ProfitableTrader(**r) for r in _as_rows(payload)]


# ── Provider ───────────────────────────────────────────────────────


class MoonDevProvider:
    """Moon Dev signal provider. Auth via ``X-API-Key`` header (never as a query
    param, so the key never lands in a URL/log). Strictly read-only."""

    name = "moondev"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        tier: str = "standard",
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.Client | None = None,
        timeout_s: float = 15.0,
        retry: RetryPolicy | None = None,
        breaker: CircuitBreaker | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        if tier not in TIER_CAPS:
            raise ValueError(f"unknown tier {tier!r}; expected one of {sorted(TIER_CAPS)}")
        self._key = api_key
        self._tier = tier
        self._base = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout_s)
        self._owns_client = client is None
        self._retry = retry or RetryPolicy()
        self._breaker = breaker or CircuitBreaker()
        # 60 req/s sustained, 200 burst (documented).
        self._limiter = rate_limiter or RateLimiter(rate_per_s=60.0, burst=200.0)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "MoonDevProvider":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def capabilities(self) -> set[Capability]:
        # Honest: only the poly flow family is implemented here.
        return {Capability.POLY_WHALES}

    # -- tier clamping ----------------------------------------------

    def _clamp(self, requested: int | None, kind: str) -> int | None:
        if requested is None:
            return None
        cap = TIER_CAPS[self._tier][kind]
        if requested > cap:
            log.warning(
                "requested limit %d exceeds %s-tier cap %d for %s; clamping (rows dropped)",
                requested, self._tier, cap, kind,
            )
            return cap
        return requested

    # -- IO core -----------------------------------------------------

    def _headers(self, auth: bool) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if auth:
            if not self._key:
                raise MoonDevAuthError(
                    "MOONDEV_API_KEY is required for this endpoint but was not provided"
                )
            h["X-API-Key"] = self._key
        return h

    def _get(self, path: str, params: dict[str, Any] | None = None, *, auth: bool = True) -> Any:
        url = f"{self._base}{path}"
        clean = {k: v for k, v in (params or {}).items() if v is not None}

        def _do() -> Any:
            self._limiter.acquire()
            resp = self._client.get(url, params=clean, headers=self._headers(auth))
            if resp.status_code in (401, 403):
                # Do not include headers/key in the error.
                raise MoonDevAuthError(f"Moon Dev auth failed ({resp.status_code}) for {path}")
            resp.raise_for_status()
            return resp.json()

        try:
            return call_resilient(
                _do,
                retry=self._retry,
                breaker=self._breaker,
                # Auth errors are terminal — do not retry them.
                retry_on=(httpx.TransportError, httpx.HTTPStatusError),
            )
        except MoonDevAuthError:
            raise
        except httpx.HTTPError as exc:
            raise ProviderHTTPError(f"moondev request failed: {exc!r}") from exc

    # -- endpoints ---------------------------------------------------

    def poly_health(self) -> PolyHealth:
        return PolyHealth(**self._get("/api/poly/health", auth=False))

    def poly_whales(
        self,
        *,
        min_usd: float | None = None,
        days: int | None = None,
        wallet: str | None = None,
        market: str | None = None,
        side: str | None = None,
        limit: int | None = None,
    ) -> list[PolyWhaleTrade]:
        params = {
            "min_usd": min_usd, "days": days, "wallet": wallet,
            "market": market, "side": side, "limit": self._clamp(limit, "whales"),
        }
        return parse_whales(self._get("/api/poly/whales", params))

    def poly_top_traders(self, *, limit: int | None = None) -> list[PolyTopTrader]:
        return parse_top_traders(
            self._get("/api/poly/whales/top-traders", {"limit": self._clamp(limit, "leaderboard")})
        )

    def poly_top_markets(self, *, limit: int | None = None) -> list[PolyTopMarket]:
        return parse_top_markets(
            self._get("/api/poly/whales/top-markets", {"limit": self._clamp(limit, "leaderboard")})
        )

    def poly_whales_daily(self) -> list[PolyDailyRollup]:
        return parse_daily(self._get("/api/poly/whales/daily"))

    def profitable_traders(self, *, limit: int | None = None) -> list[ProfitableTrader]:
        return parse_profitable_traders(
            self._get(
                "/api/poly/profitable-traders",
                {"limit": self._clamp(limit, "profitable_traders")},
            )
        )
