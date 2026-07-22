"""Kalshi adapter: live-captured payload shapes, the YES/NO book conversion,
book-walking fills, and the read-only boundary.

The conversion tests matter most. Kalshi publishes bids on both sides and no
asks; treating the raw feed as a normal book prices every trade on the wrong
side of the spread, and nothing downstream would notice.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import httpx
import pytest

from tradetk.venues.base import BinaryBook, BookLevel, Side, VenueAuthError, VenueDataError
from tradetk.venues.kalshi import (
    BASE_URLS,
    KalshiVenue,
    parse_fee_schedule,
    parse_market,
    parse_orderbook,
)

NOW = dt.datetime(2026, 7, 22, 16, 45, tzinfo=dt.timezone.utc)

# Captured live from KXBTC15M-26JUL221300-00 (production).
RAW_MARKET = {
    "ticker": "KXBTC15M-26JUL221300-00",
    "event_ticker": "KXBTC15M-26JUL221300",
    "title": "Will the price of Bitcoin be at or above $66,266.28 at 1:00 PM EDT?",
    "status": "active",
    "close_time": "2026-07-22T17:00:00Z",
    "expiration_time": "2026-07-22T17:00:00Z",
    "strike_type": "greater_or_equal",
    "floor_strike": 66266.28,
    "rules_primary": "If the price of Bitcoin is at or above 66266.28 ...",
    "yes_bid_dollars": "0.0310",
    "yes_ask_dollars": "0.0320",
    "volume_fp": "961941.77",
    "liquidity_dollars": "12345.6700",
}

RAW_BOOK = {
    "orderbook_fp": {
        "yes_dollars": [["0.0330", "321.00"], ["0.0340", "212.00"], ["0.0390", "2038.01"]],
        "no_dollars": [["0.9540", "879.00"], ["0.9560", "241.00"], ["0.9600", "7770.01"]],
    }
}


# ── market parsing ─────────────────────────────────────────────────


def test_parse_market_uses_live_dollar_field_names() -> None:
    """The documented `yes_bid`/`volume` fields do not exist; reading them yields
    None and silently drops every price."""
    m = parse_market(RAW_MARKET)
    assert m.yes_bid == Decimal("0.0310")
    assert m.yes_ask == Decimal("0.0320")
    assert m.volume == Decimal("961941.77")
    assert m.floor_strike == Decimal("66266.28")
    assert m.close_time == dt.datetime(2026, 7, 22, 17, 0, tzinfo=dt.timezone.utc)


def test_legacy_field_names_are_not_silently_accepted() -> None:
    legacy = {"ticker": "X", "title": "t", "status": "active",
              "yes_bid": 31, "volume": 100}  # the shape most docs describe
    m = parse_market(legacy)
    assert m.yes_bid is None and m.volume is None  # absent, not wrong


def test_market_without_ticker_raises() -> None:
    with pytest.raises(VenueDataError, match="no ticker"):
        parse_market({"title": "no ticker here"})


def test_machine_readable_strike_detection() -> None:
    assert parse_market(RAW_MARKET).has_machine_readable_strike is True
    custom = {**RAW_MARKET, "strike_type": "custom", "floor_strike": None}
    assert parse_market(custom).has_machine_readable_strike is False


def test_unparseable_number_does_not_lose_the_market() -> None:
    m = parse_market({**RAW_MARKET, "volume_fp": "n/a"})
    assert m.ticker == RAW_MARKET["ticker"]
    assert m.volume is None


# ── the YES/NO conversion ──────────────────────────────────────────


def test_no_bids_become_yes_asks() -> None:
    """A NO bid at 0.96 is a YES ask at 0.04 — the same resting order."""
    book = parse_orderbook("T", RAW_BOOK, retrieved_at=NOW)
    assert book.best_yes_ask == Decimal("0.0400")  # 1 - 0.9600
    assert book.best_no_bid == Decimal("0.9600")


def test_best_bid_is_the_highest_yes_bid() -> None:
    book = parse_orderbook("T", RAW_BOOK, retrieved_at=NOW)
    assert book.best_yes_bid == Decimal("0.0390")


def test_spread_and_mid() -> None:
    book = parse_orderbook("T", RAW_BOOK, retrieved_at=NOW)
    assert book.spread == Decimal("0.0010")
    assert book.mid == Decimal("0.03950")
    # Buying costs the ask, never the mid.
    assert book.best_yes_ask > book.mid


def test_ask_side_is_sorted_best_first() -> None:
    book = parse_orderbook("T", RAW_BOOK, retrieved_at=NOW)
    prices = [lv.price for lv in book.yes_asks]
    assert prices == sorted(prices)  # ascending: cheapest to buy first
    assert prices[0] == Decimal("0.0400")


def test_bid_side_is_sorted_best_first() -> None:
    book = parse_orderbook("T", RAW_BOOK, retrieved_at=NOW)
    prices = [lv.price for lv in book.yes_bids]
    assert prices == sorted(prices, reverse=True)  # descending: highest first


def test_empty_book_is_safe() -> None:
    book = parse_orderbook("T", {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}},
                           retrieved_at=NOW)
    assert book.best_yes_bid is None and book.best_yes_ask is None
    assert book.spread is None and book.mid is None
    assert book.walk_to_buy_yes(5) == (Decimal(0), Decimal(0))


def test_one_sided_book_reports_no_spread() -> None:
    """Common on demo: bids with nothing on the other side."""
    book = parse_orderbook("T", {"orderbook_fp": {"yes_dollars": [["0.24", "2"]],
                                                  "no_dollars": []}}, retrieved_at=NOW)
    assert book.best_yes_bid == Decimal("0.24")
    assert book.best_yes_ask is None
    assert book.spread is None


def test_zero_and_malformed_levels_are_dropped() -> None:
    raw = {"orderbook_fp": {"yes_dollars": [["0.30", "0"], ["bad"], ["0.20", "5"]],
                            "no_dollars": []}}
    book = parse_orderbook("T", raw, retrieved_at=NOW)
    assert [lv.price for lv in book.yes_bids] == [Decimal("0.20")]


def test_bad_payload_raises() -> None:
    with pytest.raises(VenueDataError):
        parse_orderbook("T", {"orderbook_fp": "not a dict"}, retrieved_at=NOW)


# ── book walking ───────────────────────────────────────────────────


def _book() -> BinaryBook:
    return BinaryBook(
        ticker="T", retrieved_at=NOW,
        yes_bids=[BookLevel(price="0.30", size="10"), BookLevel(price="0.29", size="50")],
        yes_asks=[BookLevel(price="0.32", size="4"), BookLevel(price="0.35", size="100")],
    )


def test_walk_fills_within_top_level() -> None:
    filled, cost = _book().walk_to_buy_yes(3)
    assert filled == Decimal(3)
    assert cost == Decimal("0.96")  # 3 * 0.32


def test_walk_crosses_levels_not_one_price() -> None:
    """Assuming a single price is how a backtest becomes fiction."""
    filled, cost = _book().walk_to_buy_yes(6)
    assert filled == Decimal(6)
    assert cost == Decimal("1.98")  # 4*0.32 + 2*0.35
    # Strictly worse than pretending the whole order filled at the top of book.
    assert cost > 6 * Decimal("0.32")


def test_walk_reports_partial_fill_honestly() -> None:
    thin = BinaryBook(ticker="T", retrieved_at=NOW,
                      yes_asks=[BookLevel(price="0.32", size="4")])
    filled, cost = thin.walk_to_buy_yes(100)
    assert filled == Decimal(4)  # not 100
    assert cost == Decimal("1.28")


def test_walk_to_sell_uses_bid_side() -> None:
    filled, proceeds = _book().walk_to_sell_yes(12)
    assert filled == Decimal(12)
    assert proceeds == Decimal("3.58")  # 10*0.30 + 2*0.29


def test_walk_zero_or_negative_is_noop() -> None:
    assert _book().walk_to_buy_yes(0) == (Decimal(0), Decimal(0))
    assert _book().walk_to_buy_yes(-5) == (Decimal(0), Decimal(0))


def test_depth_by_side() -> None:
    b = _book()
    assert b.depth(Side.yes) == Decimal("104")  # ask side
    assert b.depth(Side.no) == Decimal("60")  # bid side


def test_crossed_book_is_detected() -> None:
    crossed = BinaryBook(ticker="T", retrieved_at=NOW,
                         yes_bids=[BookLevel(price="0.50", size="1")],
                         yes_asks=[BookLevel(price="0.40", size="1")])
    assert crossed.is_crossed() is True
    assert _book().is_crossed() is False


# ── fee schedule ───────────────────────────────────────────────────


def test_fee_schedule_read_from_venue_not_hardcoded() -> None:
    fs = parse_fee_schedule({"ticker": "KXBTC15M", "fee_type": "quadratic", "fee_multiplier": 1})
    assert fs.fee_type == "quadratic"
    assert fs.fee_multiplier == Decimal(1)
    assert fs.maker_fees_charged is False


def test_maker_fee_variant_detected() -> None:
    fs = parse_fee_schedule({"ticker": "X", "fee_type": "quadratic_with_maker_fees",
                             "fee_multiplier": 1})
    assert fs.maker_fees_charged is True


# ── adapter wiring ─────────────────────────────────────────────────


def _venue(payload: dict, status: int = 200, env: str = "demo"):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return KalshiVenue(env, client=client), seen


def test_environments_hit_the_right_hosts() -> None:
    assert "demo-api.kalshi.co" in BASE_URLS["demo"]
    assert "api.elections.kalshi.com" in BASE_URLS["prod"]
    v, seen = _venue({"markets": [RAW_MARKET]})
    with v:
        v.markets(limit=1)
    assert seen[0].url.host == "demo-api.kalshi.co"
    assert "/trade-api/v2/markets" in str(seen[0].url)


def test_defaults_to_demo() -> None:
    v, _ = _venue({"markets": []})
    assert v.environment == "demo"


def test_unknown_environment_rejected() -> None:
    with pytest.raises(ValueError, match="unknown environment"):
        KalshiVenue("production")


def test_markets_roundtrip_and_skips_bad_rows() -> None:
    v, _ = _venue({"markets": [RAW_MARKET, {"title": "no ticker"}]})
    with v:
        out = v.markets(limit=10)
    assert len(out) == 1  # the unparseable row is skipped, not fatal
    assert out[0].ticker == RAW_MARKET["ticker"]


def test_orderbook_roundtrip() -> None:
    v, _ = _venue(RAW_BOOK)
    with v:
        book = v.orderbook("KXBTC15M-26JUL221300-00", depth=5)
    assert book.best_yes_ask == Decimal("0.0400")


def test_market_data_needs_no_credentials() -> None:
    v, seen = _venue({"markets": [RAW_MARKET]})
    with v:
        assert v.authenticated is False
        v.markets()
    assert "KALSHI-ACCESS-KEY" not in seen[0].headers


def test_401_surfaces_as_auth_error_and_is_not_retried() -> None:
    v, seen = _venue({"error": "unauthorized"}, status=401)
    with v:
        with pytest.raises(VenueAuthError):
            v.markets()
    assert len(seen) == 1


def test_minimums_permit_a_two_dollar_position() -> None:
    v, _ = _venue({})
    mins = v.minimums()
    assert mins.min_order_contracts == 1
    assert mins.price_tick == Decimal("0.01")
    # A $2 position must buy at least one contract at a plausible price.
    assert Decimal("2.00") / mins.max_price >= mins.min_order_contracts
    # Withdrawal minimums are the real hazard and must be flagged, not assumed.
    assert "withdraw" in mins.notes.lower()
    assert mins.min_withdrawal_dollars is None


# ── the execute boundary, enforced structurally ────────────────────


def test_adapter_exposes_no_order_placement() -> None:
    """Order submission may exist only in the `execute` command module."""
    forbidden = {"place_order", "create_order", "submit_order", "order", "buy", "sell", "cancel"}
    assert forbidden.isdisjoint(set(dir(KalshiVenue)))


def test_adapter_source_contains_no_order_endpoint() -> None:
    from pathlib import Path

    import tradetk.venues.kalshi as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    assert "/portfolio/orders" not in source
    assert ".post(" not in source  # no write verb anywhere in the adapter


# ── request signing ────────────────────────────────────────────────


def _write_test_key(tmp_path):
    """Generate a throwaway RSA key (never a real credential)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "test_key.pem"
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return path, key.public_key()


def test_signature_verifies_over_timestamp_method_path(tmp_path) -> None:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    from tradetk.venues.kalshi import KalshiSigner

    path, public = _write_test_key(tmp_path)
    signer = KalshiSigner("key-123", path)
    headers = signer.headers("GET", "/trade-api/v2/portfolio/balance", timestamp_ms=1700000000000)

    assert headers["KALSHI-ACCESS-KEY"] == "key-123"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"

    import base64

    public.verify(  # raises if the signature does not match
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        b"1700000000000GET/trade-api/v2/portfolio/balance",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_signature_changes_with_path(tmp_path) -> None:
    from tradetk.venues.kalshi import KalshiSigner

    path, _ = _write_test_key(tmp_path)
    s = KalshiSigner("k", path)
    a = s.headers("GET", "/a", timestamp_ms=1)["KALSHI-ACCESS-SIGNATURE"]
    b = s.headers("GET", "/b", timestamp_ms=1)["KALSHI-ACCESS-SIGNATURE"]
    assert a != b


def test_missing_key_file_raises_auth_error(tmp_path) -> None:
    from tradetk.venues.kalshi import KalshiSigner

    with pytest.raises(VenueAuthError, match="not found"):
        KalshiSigner("k", tmp_path / "nope.pem")


def test_authed_endpoint_without_credentials_refuses(tmp_path) -> None:
    v, seen = _venue({"balance": 0})
    with v:
        with pytest.raises(VenueAuthError, match="requires credentials"):
            v._get("/portfolio/balance", auth=True)
    assert seen == []  # never left the process
