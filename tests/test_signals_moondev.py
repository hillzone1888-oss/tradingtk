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
    parse_daily,
    parse_profitable_traders,
    parse_top_markets,
    parse_top_traders,
    parse_whales,
)

# ── sample payloads (shapes from the spec's field lists) ───────────

# Shapes below are the LIVE response shapes captured 2026-07-22, including the
# envelope wrappers ("trades", "rollup", ...) that the spec's prose omits.

WHALE_ROW = {
    "ts": 1784737175,
    "wallet": "0x1465B79bfF7992Bc703e1AaFB3683b1089647072",
    "pseudonym": "Well-To-Do-Code",
    "market_title": "Will Ethereum dip to $1,500 by December 31, 2026?",
    "market_slug": "will-ethereum-dip-to-1500-by-december-31-2026-865-787",
    "event_slug": "what-price-will-ethereum-hit-before-2027",
    "outcome": "No",
    "side": "BUY",
    "price": 0.6399872968670747,
    "size": 33062.71,
    "usd_amount": 21159.714399999997,
    "tx_hash": "0x2bca2c9a5a92d5166f99b6a2694fe8fa723307f51ee09d10034c8a5b973776f7",
}
WHALES = {"count": 1, "limit": 1, "full_access": False, "filters": {}, "trades": [WHALE_ROW]}

TOP_TRADERS = {"count": 1, "traders": [
    {"wallet": "0xabc", "pseudonym": "Handle", "trade_count": 12,
     "total_volume": 50000.0, "biggest_trade": 9000.0, "markets_traded": 4,
     "last_trade_ts": 1784675040},
]}

TOP_MARKETS = {"count": 1, "markets": [
    {"market_title": "BTC 70k", "market_slug": "btc-70k", "event_slug": "btc",
     "whale_trades": 8, "whale_volume": 40000.0, "unique_whales": 5,
     "biggest_trade": 12000.0, "last_trade_ts": 1784675040},
]}

DAILY = {"count": 1, "days": 30, "rollup": [
    {"day": "2026-07-22", "trade_count": 1343, "total_volume": 5340099.39,
     "biggest_trade": 222109.41, "smallest_trade": 1000.0, "avg_trade": 3976.24,
     "unique_whales": 618, "unique_markets": 429},
]}

PROFITABLE = {"total": 1, "full_list": False, "traders": [
    {"wallet": "0xabc", "pnl_7d": 1234.5, "volume_7d": 88000.0,
     "trades_7d": 30, "redeems_7d": 2, "source": "poly",
     "polymarket_link": "https://polymarket.com/0xabc"},
]}

HEALTH = {"status": "ok", "uptime_minutes": 123.5, "queue_depth": 0,
          "wallets_checked": 900, "profitable_traders": 25, "seen_wallets": 5000}


# ── pure parsers ───────────────────────────────────────────────────


def test_parse_whales_fields_and_time() -> None:
    trades = parse_whales(WHALES)
    assert len(trades) == 1
    t = trades[0]
    assert t.wallet.startswith("0x1465")
    assert t.pseudonym == "Well-To-Do-Code"
    assert t.price == pytest.approx(0.6399872968670747)
    assert t.side == "BUY"
    assert t.when.tzinfo is timezone.utc
    assert t.when.year == 2026


def test_parse_accepts_bare_list_and_every_live_wrapper_key() -> None:
    """Regression: "trades" and "rollup" wrappers were unknown to the parser,
    so poly_whales and daily — the two busiest endpoints — raised on every call."""
    assert len(parse_whales(WHALES)) == 1  # "trades"
    assert len(parse_daily(DAILY)) == 1  # "rollup"
    assert len(parse_top_traders(TOP_TRADERS)) == 1  # "traders"
    assert len(parse_top_markets(TOP_MARKETS)) == 1  # "markets"
    assert len(parse_profitable_traders(PROFITABLE)) == 1
    assert len(parse_whales([WHALE_ROW])) == 1  # bare list still works


def test_daily_rollup_uses_real_field_names() -> None:
    """The spec's prose says whale_trades/whale_volume; the API returns
    trade_count/total_volume. Wrong names must fail loudly, not yield None."""
    row = parse_daily(DAILY)[0]
    assert row.trade_count == 1343
    assert row.total_volume == pytest.approx(5340099.39)
    assert row.unique_markets == 429
    with pytest.raises(Exception):  # the spec-named shape is not silently accepted
        parse_daily([{"day": "2026-07-22", "whale_trades": 5, "whale_volume": 1.0}])


def test_unknown_single_list_key_falls_back_with_warning(caplog) -> None:
    payload = {"count": 1, "renamed_rows": [WHALE_ROW]}
    with caplog.at_level("WARNING"):
        assert len(parse_whales(payload)) == 1
    assert "unrecognised row-array key" in caplog.text


def test_ambiguous_multiple_lists_raises() -> None:
    with pytest.raises(MoonDevError, match="cannot locate row array"):
        parse_whales({"a": [WHALE_ROW], "b": [WHALE_ROW]})


def test_parse_unexpected_shape_raises() -> None:
    with pytest.raises(MoonDevError, match="unexpected payload shape"):
        parse_whales(42)


def test_price_out_of_prob_range_rejected() -> None:
    with pytest.raises(Exception):  # implied prob must be 0..1
        parse_whales([{**WHALE_ROW, "price": 1.5}])


def test_nan_amount_rejected() -> None:
    with pytest.raises(Exception):
        parse_whales([{**WHALE_ROW, "usd_amount": float("nan")}])


def test_unknown_field_is_ignored_not_fatal() -> None:
    # External evolving API: a new field must be forward-compatible.
    trades = parse_whales([{**WHALE_ROW, "brand_new_field": "surprise"}])
    assert trades[0].pseudonym == "Well-To-Do-Code"


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
    assert trades[0].pseudonym == "Well-To-Do-Code"
    # limit was clamped to the standard whale cap before the request.
    assert seen[0].url.params["limit"] == "250"


# ── tier reconciliation from the response envelope ─────────────────


def test_full_access_flag_is_recorded() -> None:
    p, _ = _provider_capturing(200, WHALES, api_key="k")  # envelope says False
    with p:
        assert p.observed_full_access is None  # nothing observed yet
        p.poly_whales(limit=5)
        assert p.observed_full_access is False


def test_tier_mismatch_warns(caplog) -> None:
    """Config claiming qe while the API reports full_access=false means our row
    caps are wrong and we would under-read the universe silently."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=WHALES)  # full_access: False

    client = httpx.Client(transport=httpx.MockTransport(handler))
    p = MoonDevProvider(api_key="k", tier="qe", client=client)
    with p, caplog.at_level("WARNING"):
        p.poly_whales(limit=5)
    assert "tier mismatch" in caplog.text


def test_no_warning_when_tier_matches(caplog) -> None:
    p, _ = _provider_capturing(200, WHALES, api_key="k")  # standard + False
    with p, caplog.at_level("WARNING"):
        p.poly_whales(limit=5)
    assert "tier mismatch" not in caplog.text
