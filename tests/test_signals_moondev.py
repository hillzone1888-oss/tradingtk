"""Moon Dev provider: pure parsers, tier clamping, auth boundary, and IO via
httpx.MockTransport (no network). Also asserts the security invariant that the
API key travels in a header, never a query param."""

from __future__ import annotations

from datetime import timezone

import httpx
import pytest

from tradetk.enums import Capability
from tradetk.signals.moondev import (
    MoonDevAuthError,
    MoonDevError,
    MoonDevProvider,
    _epoch_to_dt,
    parse_profitable_traders,
    parse_top_markets,
    parse_top_traders,
    parse_whales,
)

# ── sample payloads (shapes from the spec's field lists) ───────────

WHALES = [
    {
        "ts": 1784675040,
        "wallet": "0xabc",
        "market_title": "BTC above $70k on 2026-08-01",
        "market_slug": "btc-70k-2026-08-01",
        "event_slug": "btc-monthly",
        "outcome": "Yes",
        "side": "buy",
        "price": 0.42,
        "size": 5000,
        "usd_amount": 2100.0,
        "tx_hash": "0xdeadbeef",
    }
]

TOP_TRADERS = {"data": [
    {"wallet": "0xabc", "trade_count": 12, "total_volume": 50000.0,
     "biggest_trade": 9000.0, "markets_traded": 4, "last_trade_ts": 1784675040},
]}

TOP_MARKETS = [
    {"market_title": "BTC 70k", "market_slug": "btc-70k", "whale_trades": 8,
     "whale_volume": 40000.0, "unique_whales": 5, "biggest_trade": 12000.0},
]

PROFITABLE = [
    {"wallet": "0xabc", "pnl_7d": 1234.5, "volume_7d": 88000.0,
     "trades_7d": 30, "redeems_7d": 2, "source": "poly"},
]

HEALTH = {"status": "ok", "uptime_minutes": 123.5, "queue_depth": 0,
          "wallets_checked": 900, "profitable_traders": 25, "seen_wallets": 5000}


# ── pure parsers ───────────────────────────────────────────────────


def test_parse_whales_fields_and_time() -> None:
    trades = parse_whales(WHALES)
    assert len(trades) == 1
    t = trades[0]
    assert t.wallet == "0xabc"
    assert t.price == pytest.approx(0.42)
    assert t.usd_amount == pytest.approx(2100.0)
    assert t.when.tzinfo is timezone.utc
    assert t.when.year == 2026


def test_parse_accepts_bare_list_and_wrapped_dict() -> None:
    # top-traders payload is wrapped in {"data": [...]}, whales is a bare list.
    assert len(parse_top_traders(TOP_TRADERS)) == 1
    assert len(parse_top_markets(TOP_MARKETS)) == 1
    assert len(parse_profitable_traders(PROFITABLE)) == 1


def test_parse_unexpected_shape_raises() -> None:
    with pytest.raises(MoonDevError, match="unexpected payload shape"):
        parse_whales(42)


def test_price_out_of_prob_range_rejected() -> None:
    bad = [{**WHALES[0], "price": 1.5}]  # implied prob must be 0..1
    with pytest.raises(Exception):
        parse_whales(bad)


def test_nan_amount_rejected() -> None:
    bad = [{**WHALES[0], "usd_amount": float("nan")}]
    with pytest.raises(Exception):
        parse_whales(bad)


def test_unknown_field_is_ignored_not_fatal() -> None:
    # External evolving API: a new field must be forward-compatible.
    trades = parse_whales([{**WHALES[0], "brand_new_field": "surprise"}])
    assert trades[0].wallet == "0xabc"


def test_epoch_seconds_vs_millis() -> None:
    sec = _epoch_to_dt(1784675040)
    ms = _epoch_to_dt(1784675040000)
    assert sec == ms  # both resolve to the same instant


# ── tier clamping ──────────────────────────────────────────────────


def test_standard_tier_clamps_whale_limit(caplog) -> None:
    p = MoonDevProvider(api_key="k", tier="standard")
    with caplog.at_level("WARNING"):
        assert p._clamp(5000, "whales") == 250  # standard cap
    assert "clamping" in caplog.text


def test_qe_tier_allows_large_limit() -> None:
    p = MoonDevProvider(api_key="k", tier="qe")
    assert p._clamp(5000, "whales") == 5000


def test_unknown_tier_rejected() -> None:
    with pytest.raises(ValueError, match="unknown tier"):
        MoonDevProvider(api_key="k", tier="platinum")


# ── capability honesty ─────────────────────────────────────────────


def test_capabilities_only_advertise_implemented() -> None:
    caps = MoonDevProvider(api_key="k").capabilities()
    assert caps == {Capability.POLY_WHALES}
    # HL-derived signals are NOT implemented here, so must NOT be advertised.
    assert Capability.LIQUIDATIONS not in caps
    assert Capability.HLP_SENTIMENT not in caps


# ── auth boundary + IO with a mocked transport ─────────────────────


def _provider_capturing(status: int, payload, *, api_key: str | None):
    """Build a provider whose transport records the requests it sees."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return MoonDevProvider(api_key=api_key, client=client), seen


def test_authed_endpoint_without_key_raises_before_io() -> None:
    p, seen = _provider_capturing(200, WHALES, api_key=None)
    with p:
        with pytest.raises(MoonDevAuthError, match="MOONDEV_API_KEY is required"):
            p.poly_whales(limit=10)
    assert seen == []  # never left the process without a key


def test_public_health_needs_no_key() -> None:
    p, seen = _provider_capturing(200, HEALTH, api_key=None)
    with p:
        health = p.poly_health()
    assert health.status == "ok"
    assert len(seen) == 1
    assert "X-API-Key" not in seen[0].headers  # public call, no key sent


def test_key_travels_in_header_never_query() -> None:
    p, seen = _provider_capturing(200, WHALES, api_key="super-secret")
    with p:
        p.poly_whales(min_usd=1000, limit=10)
    req = seen[0]
    assert req.headers["X-API-Key"] == "super-secret"
    # The key must never leak into the URL/query string (would land in logs).
    assert "super-secret" not in str(req.url)
    assert "api_key" not in req.url.params


def test_401_raises_auth_error_and_is_terminal() -> None:
    # A 401 must surface as MoonDevAuthError, not be retried as a transient.
    p, seen = _provider_capturing(401, {"error": "Unauthorized"}, api_key="wrong")
    with p:
        with pytest.raises(MoonDevAuthError):
            p.poly_whales(limit=10)
    assert len(seen) == 1  # not retried


def test_whales_roundtrip_and_clamped_param() -> None:
    p, seen = _provider_capturing(200, WHALES, api_key="k")
    with p:
        trades = p.poly_whales(limit=99999)  # over the standard cap
    assert trades[0].wallet == "0xabc"
    # limit was clamped to the standard whale cap before the request.
    assert seen[0].url.params["limit"] == "250"
