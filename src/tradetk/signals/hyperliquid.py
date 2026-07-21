"""Native Hyperliquid data provider — thin, typed client over the info API.

Read-only. No orders are ever placed here; this is the default + fallback signal
source. Response shapes were locked against live calls to
``https://api.hyperliquid.xyz/info`` (POST):

* ``allMids``          -> ``{coin_name: mid_str}`` (perp mids keyed by name)
* ``l2Book``           -> ``{coin, time(ms), levels: [bids, asks]}``; level =
  ``{px, sz, n}``; bids descending, asks ascending
* ``candleSnapshot``   -> ``[{t, T, s, i, o, h, l, c, v, n}, ...]``
* ``fundingHistory``   -> ``[{coin, fundingRate, premium, time(ms)}, ...]``
* ``metaAndAssetCtxs`` -> ``[{universe: [...]}, [ctx, ...]]`` (parallel arrays)

Parsing is split into module-level pure functions (``parse_*``) so it is unit
tested with recorded payloads and no network.
"""

from __future__ import annotations

from typing import Any

import httpx

from tradetk.enums import Capability
from tradetk.signals.base import (
    Candle,
    DataValidationError,
    FundingPoint,
    Level,
    OrderBook,
    PriceSnapshot,
    ProviderHTTPError,
    RealizedVol,
    realized_vol_from_closes,
    utcnow,
)
from tradetk.signals.resilience import CircuitBreaker, RetryPolicy, call_resilient

INFO_URL = "https://api.hyperliquid.xyz/info"

# Native info endpoints use UPPERCASE coin names (BTC), unlike the Moon Dev
# legacy tick endpoints which require lowercase — do not conflate them.


def _norm_symbol(symbol: str) -> str:
    return symbol.strip().upper()


# ── Pure parsers (no IO) ───────────────────────────────────────────


def parse_mid(symbol: str, mids: dict[str, Any]) -> PriceSnapshot:
    sym = _norm_symbol(symbol)
    if sym not in mids:
        raise DataValidationError(f"no mid price for {sym!r} in allMids response")
    return PriceSnapshot(symbol=sym, mid=float(mids[sym]), retrieved_at=utcnow())


def parse_orderbook(symbol: str, raw: dict[str, Any]) -> OrderBook:
    if "levels" not in raw or len(raw["levels"]) != 2:
        raise DataValidationError("l2Book response missing bid/ask levels")
    bids_raw, asks_raw = raw["levels"]

    def _levels(rows: list[dict[str, Any]]) -> list[Level]:
        return [Level(px=float(r["px"]), sz=float(r["sz"]), n=int(r["n"])) for r in rows]

    bids = _levels(bids_raw)
    asks = _levels(asks_raw)
    # Sanity: best bid must be below best ask (crossed book => bad data).
    if bids and asks and bids[0].px >= asks[0].px:
        raise DataValidationError(
            f"crossed/locked book for {symbol}: bid {bids[0].px} >= ask {asks[0].px}"
        )
    return OrderBook(
        symbol=_norm_symbol(symbol),
        venue_time_ms=int(raw["time"]),
        retrieved_at=utcnow(),
        bids=bids,
        asks=asks,
    )


def parse_candles(rows: list[dict[str, Any]]) -> list[Candle]:
    out: list[Candle] = []
    for r in rows:
        out.append(
            Candle(
                symbol=_norm_symbol(str(r["s"])),
                interval=str(r["i"]),
                open_ms=int(r["t"]),
                close_ms=int(r["T"]),
                o=float(r["o"]),
                h=float(r["h"]),
                l=float(r["l"]),
                c=float(r["c"]),
                v=float(r["v"]),
                trades=int(r["n"]),
            )
        )
    return out


def parse_funding_history(rows: list[dict[str, Any]]) -> list[FundingPoint]:
    return [
        FundingPoint(
            symbol=_norm_symbol(str(r["coin"])),
            time_ms=int(r["time"]),
            rate=float(r["fundingRate"]),
            premium=float(r["premium"]) if r.get("premium") is not None else None,
        )
        for r in rows
    ]


def parse_current_funding(symbol: str, raw: list[Any]) -> FundingPoint:
    """Extract current funding for `symbol` from a metaAndAssetCtxs response."""
    sym = _norm_symbol(symbol)
    meta, ctxs = raw[0], raw[1]
    universe = meta["universe"]
    idx = next((i for i, u in enumerate(universe) if u["name"] == sym), None)
    if idx is None:
        raise DataValidationError(f"{sym!r} not found in perp universe")
    ctx = ctxs[idx]
    prem = ctx.get("premium")
    return FundingPoint(
        symbol=sym,
        time_ms=int(utcnow().timestamp() * 1000),
        rate=float(ctx["funding"]),
        premium=float(prem) if prem is not None else None,
    )


# ── Provider ───────────────────────────────────────────────────────


class HyperliquidProvider:
    """Native Hyperliquid signal provider (implements the DataProvider protocol)."""

    name = "hyperliquid"

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        info_url: str = INFO_URL,
        timeout_s: float = 15.0,
        retry: RetryPolicy | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._info_url = info_url
        # `truststore` is injected by the CLI entrypoint so httpx trusts the OS
        # cert store (this machine has a corporate/MITM root CA).
        self._client = client or httpx.Client(timeout=timeout_s)
        self._owns_client = client is None
        self._retry = retry or RetryPolicy()
        self._breaker = breaker or CircuitBreaker()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "HyperliquidProvider":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def capabilities(self) -> set[Capability]:
        return {
            Capability.SPOT_PRICE,
            Capability.PERP_PRICE,
            Capability.CANDLES,
            Capability.ORDERBOOK,
            Capability.FUNDING,
            Capability.REALIZED_VOL,
        }

    # -- IO core -----------------------------------------------------

    def _post(self, body: dict[str, Any]) -> Any:
        def _do() -> Any:
            resp = self._client.post(self._info_url, json=body)
            resp.raise_for_status()
            return resp.json()

        try:
            return call_resilient(
                _do,
                retry=self._retry,
                breaker=self._breaker,
                retry_on=(httpx.TransportError, httpx.HTTPStatusError),
            )
        except httpx.HTTPError as exc:
            raise ProviderHTTPError(f"hyperliquid request failed: {exc!r}") from exc

    # -- Protocol methods --------------------------------------------

    def mid_price(self, symbol: str) -> PriceSnapshot:
        return parse_mid(symbol, self._post({"type": "allMids"}))

    def orderbook(self, symbol: str) -> OrderBook:
        raw = self._post({"type": "l2Book", "coin": _norm_symbol(symbol)})
        return parse_orderbook(symbol, raw)

    def candles(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[Candle]:
        raw = self._post(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": _norm_symbol(symbol),
                    "interval": interval,
                    "startTime": start_ms,
                    "endTime": end_ms,
                },
            }
        )
        return parse_candles(raw)

    def funding_history(self, symbol: str, start_ms: int, end_ms: int) -> list[FundingPoint]:
        raw = self._post(
            {"type": "fundingHistory", "coin": _norm_symbol(symbol),
             "startTime": start_ms, "endTime": end_ms}
        )
        return parse_funding_history(raw)

    def current_funding(self, symbol: str) -> FundingPoint:
        return parse_current_funding(symbol, self._post({"type": "metaAndAssetCtxs"}))

    def realized_vol(self, symbol: str, lookback_days: int, interval: str = "1h") -> RealizedVol:
        from tradetk.signals.base import INTERVAL_SECONDS

        if interval not in INTERVAL_SECONDS:
            raise ValueError(f"unknown interval {interval!r}")
        now_ms = int(utcnow().timestamp() * 1000)
        start_ms = now_ms - lookback_days * 86_400_000
        candles = self.candles(symbol, interval, start_ms, now_ms)
        rv = realized_vol_from_closes([c.c for c in candles], interval, lookback_days)
        # `realized_vol_from_closes` leaves symbol blank; stamp it here.
        return rv.model_copy(update={"symbol": _norm_symbol(symbol)})
