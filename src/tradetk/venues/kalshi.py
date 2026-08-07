"""Kalshi adapter — read-only market data, verified live against both environments.

**No order endpoint is implemented here, by design.** The operating rules permit
order submission from the `execute` command module alone, so this adapter cannot
place a trade even if called incorrectly.

Field names were captured from live responses, not from documentation. The API
returns a ``*_dollars`` / ``*_fp`` naming scheme and sends **every numeric value
as a decimal string** — the widely-documented integer-cent fields (``yes_bid``,
``volume``) are absent, and code written against them silently reads ``None``.

Environments (verified):

============  =========================================  ==================
environment   base URL                                   market data
============  =========================================  ==================
demo          ``https://demo-api.kalshi.co``              public, very thin
prod          ``https://api.elections.kalshi.com``        public, real depth
============  =========================================  ==================

Market data is public on both; ``/portfolio/*`` returns 401 without credentials.
Demo books are nearly empty (8 two-sided markets in 400 sampled, best volume 2),
so demo proves plumbing while realistic depth only exists in production.
"""

from __future__ import annotations

import base64
import datetime as dt
import logging
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from tradetk.signals.resilience import CircuitBreaker, RetryPolicy, call_resilient
from tradetk.venues.base import (
    BinaryBook,
    BookLevel,
    FeeSchedule,
    VenueAuthError,
    VenueDataError,
    VenueError,
    VenueMarket,
    VenueMinimums,
)

log = logging.getLogger("tradetk.venues.kalshi")

BASE_URLS = {
    "demo": "https://demo-api.kalshi.co",
    "prod": "https://api.elections.kalshi.com",
}
API_PREFIX = "/trade-api/v2"

# Kalshi's published floors. The API does not expose these, so they are declared
# here and surfaced by `minimums()` for the startup viability check rather than
# being scattered through the code as literals.
_MIN_ORDER_CONTRACTS = 1
_PRICE_TICK = Decimal("0.01")


def _dec(value: object) -> Decimal | None:
    """Parse a venue decimal string. Returns None for absent/garbage values.

    Never raises: a single unparseable field must not lose the whole market.
    """
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        log.debug("unparseable decimal %r", value)
        return None


def _dt(value: object) -> dt.datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        log.debug("unparseable timestamp %r", value)
        return None


# ── pure parsers (no IO, unit-testable) ────────────────────────────


def parse_market(raw: dict[str, Any]) -> VenueMarket:
    """Map a raw Kalshi market to the canonical model, using the live field names."""
    ticker = raw.get("ticker")
    if not ticker:
        raise VenueDataError("market payload has no ticker")
    return VenueMarket(
        ticker=ticker,
        series_ticker=raw.get("series_ticker"),
        event_ticker=raw.get("event_ticker"),
        title=raw.get("title") or "",
        status=raw.get("status") or "unknown",
        close_time=_dt(raw.get("close_time")),
        expiration_time=_dt(raw.get("expiration_time")),
        strike_type=raw.get("strike_type"),
        floor_strike=_dec(raw.get("floor_strike")),
        cap_strike=_dec(raw.get("cap_strike")),
        rules_primary=raw.get("rules_primary"),
        yes_bid=_dec(raw.get("yes_bid_dollars")),
        yes_ask=_dec(raw.get("yes_ask_dollars")),
        volume=_dec(raw.get("volume_fp")),
        liquidity=_dec(raw.get("liquidity_dollars")),
        result=(raw.get("result") or None),
    )


def parse_orderbook(ticker: str, raw: dict[str, Any], *, retrieved_at: dt.datetime) -> BinaryBook:
    """Convert Kalshi's dual-bid book into the canonical YES-denominated view.

    Kalshi publishes ``{"orderbook_fp": {"yes_dollars": [[px, sz]],
    "no_dollars": [[px, sz]]}}`` — **both sides are bids**. A resting NO bid at
    0.96 is the same order as a YES ask at 0.04, so the ask side is derived as
    ``1 - no_bid``. Getting this backwards prices every trade at the wrong side
    of the spread.
    """
    book = raw.get("orderbook_fp") or raw.get("orderbook") or {}
    if not isinstance(book, dict):
        raise VenueDataError(f"unexpected orderbook payload for {ticker}")

    def levels(key: str) -> list[tuple[Decimal, Decimal]]:
        out: list[tuple[Decimal, Decimal]] = []
        for entry in book.get(key) or []:
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                continue
            price, size = _dec(entry[0]), _dec(entry[1])
            if price is None or size is None or size <= 0:
                continue
            out.append((price, size))
        return out

    # Resting YES bids: best is the highest price.
    yes_bids = [
        BookLevel(price=p, size=s)
        for p, s in sorted(levels("yes_dollars"), key=lambda x: x[0], reverse=True)
    ]
    # Resting NO bids become YES asks at (1 - price); best is the lowest.
    yes_asks = [
        BookLevel(price=Decimal(1) - p, size=s)
        for p, s in sorted(levels("no_dollars"), key=lambda x: x[0], reverse=True)
    ]

    result = BinaryBook(
        ticker=ticker, retrieved_at=retrieved_at, yes_bids=yes_bids, yes_asks=yes_asks
    )
    if result.is_crossed():
        # Real and transient during fast markets; surface it rather than trading through it.
        log.warning(
            "crossed book on %s: bid %s > ask %s", ticker, result.best_yes_bid, result.best_yes_ask
        )
    return result


def parse_fee_schedule(raw: dict[str, Any]) -> FeeSchedule:
    """Read per-series fee parameters straight from the venue.

    The multiplier is never hardcoded — a schedule change must show up as data.
    """
    ticker = raw.get("ticker")
    if not ticker:
        raise VenueDataError("series payload has no ticker")
    fee_type = str(raw.get("fee_type") or "unknown")
    multiplier = _dec(raw.get("fee_multiplier"))
    return FeeSchedule(
        series_ticker=ticker,
        fee_type=fee_type,
        fee_multiplier=multiplier if multiplier is not None else Decimal(0),
        maker_fees_charged="maker" in fee_type,
    )


# ── request signing (RSA-PSS) ──────────────────────────────────────


class KalshiSigner:
    """Signs requests with RSA-PSS over ``timestamp + METHOD + path``.

    The private key is loaded from a file path and never inlined in config or
    logged. It is a *trading* credential with no withdrawal authority.
    """

    def __init__(self, key_id: str, private_key_path: str | Path) -> None:
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise VenueError("the `cryptography` package is required to sign requests") from exc

        path = Path(private_key_path)
        if not path.exists():
            raise VenueAuthError(f"private key file not found: {path}")
        self._key_id = key_id
        self._padding = padding
        self._hashes = hashes
        with path.open("rb") as handle:
            self._key = serialization.load_pem_private_key(handle.read(), password=None)

    def headers(self, method: str, path: str, *, timestamp_ms: int | None = None) -> dict[str, str]:
        ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
        message = f"{ts}{method.upper()}{path}".encode()
        signature = self._key.sign(
            message,
            self._padding.PSS(
                mgf=self._padding.MGF1(self._hashes.SHA256()),
                salt_length=self._padding.PSS.DIGEST_LENGTH,
            ),
            self._hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }


# ── adapter ────────────────────────────────────────────────────────


class KalshiVenue:
    """Read-only Kalshi client.

    `environment` selects demo or prod. Credentials are optional: every method
    on this class uses public market data, so the adapter is fully usable — and
    testable — before any account exists.
    """

    name = "kalshi"

    def __init__(
        self,
        environment: str = "demo",
        *,
        key_id: str | None = None,
        private_key_path: str | Path | None = None,
        client: httpx.Client | None = None,
        timeout_s: float = 20.0,
        retry: RetryPolicy | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        if environment not in BASE_URLS:
            raise ValueError(f"unknown environment {environment!r}; expected demo or prod")
        self.environment = environment
        self._base = BASE_URLS[environment]
        self._client = client or httpx.Client(timeout=timeout_s, follow_redirects=True)
        self._owns_client = client is None
        self._retry = retry or RetryPolicy()
        self._breaker = breaker or CircuitBreaker()
        self._signer = (
            KalshiSigner(key_id, private_key_path) if key_id and private_key_path else None
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "KalshiVenue":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def authenticated(self) -> bool:
        return self._signer is not None

    # -- IO ----------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None, *, auth: bool = False) -> Any:
        full = f"{API_PREFIX}{path}"
        url = f"{self._base}{full}"
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {"Accept": "application/json"}
        if auth:
            if self._signer is None:
                raise VenueAuthError(f"{path} requires credentials; none configured")
            headers.update(self._signer.headers("GET", full))

        def _do() -> Any:
            resp = self._client.get(url, params=clean, headers=headers)
            if resp.status_code in (401, 403):
                raise VenueAuthError(f"kalshi auth failed ({resp.status_code}) for {path}")
            resp.raise_for_status()
            return resp.json()

        try:
            return call_resilient(
                _do, retry=self._retry, breaker=self._breaker,
                retry_on=(httpx.TransportError, httpx.HTTPStatusError),
            )
        except VenueAuthError:
            raise
        except httpx.HTTPError as exc:
            raise VenueError(f"kalshi request failed: {exc!r}") from exc

    # -- read-only surface -------------------------------------------

    def markets(
        self,
        *,
        series_ticker: str | None = None,
        status: str = "open",
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[VenueMarket]:
        payload = self._get(
            "/markets",
            {"series_ticker": series_ticker, "status": status,
             "limit": min(limit, 1000), "cursor": cursor},
        )
        out: list[VenueMarket] = []
        for raw in payload.get("markets") or []:
            try:
                out.append(parse_market(raw))
            except VenueDataError as exc:  # one bad row must not lose the page
                log.warning("skipping unparseable market: %s", exc)
        return out

    def market(self, ticker: str) -> VenueMarket:
        payload = self._get(f"/markets/{ticker}")
        raw = payload.get("market") or payload
        return parse_market(raw)

    def orderbook(self, ticker: str, *, depth: int = 10) -> BinaryBook:
        payload = self._get(f"/markets/{ticker}/orderbook", {"depth": depth})
        return parse_orderbook(
            ticker, payload, retrieved_at=dt.datetime.now(dt.timezone.utc)
        )

    def series(self, series_ticker: str) -> dict[str, Any]:
        payload = self._get(f"/series/{series_ticker}")
        return payload.get("series") or payload

    def fee_schedule(self, series_ticker: str) -> FeeSchedule:
        return parse_fee_schedule(self.series(series_ticker))

    def exchange_status(self) -> dict[str, Any]:
        return self._get("/exchange/status")

    def minimums(self) -> VenueMinimums:
        """Venue floors relevant to a very small book.

        Kalshi does not publish these through the API, so they are stated here
        and reported at startup. A $2 position is viable: the binding constraint
        is the per-position ceiling after integer quantisation, not a venue
        minimum. Withdrawal minimums are the real hazard on a $20 balance and
        must be confirmed against the account's terms before funding.
        """
        return VenueMinimums(
            min_order_contracts=_MIN_ORDER_CONTRACTS,
            price_tick=_PRICE_TICK,
            min_price=Decimal("0.01"),
            max_price=Decimal("0.99"),
            min_deposit_dollars=None,
            min_withdrawal_dollars=None,
            per_order_min_fee_dollars=None,
            notes=(
                "Order minimum is 1 contract on a 1-cent grid, so a ~$2 position is "
                "feasible. Deposit/withdrawal minimums are NOT exposed by the API — "
                "confirm them against account terms before funding, since a $20 "
                "balance can become effectively unwithdrawable."
            ),
        )
