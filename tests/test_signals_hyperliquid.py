"""Hyperliquid provider: pure parsers + end-to-end IO via httpx.MockTransport."""

from __future__ import annotations

import httpx
import pytest

from tradetk.signals.base import DataValidationError
from tradetk.signals.hyperliquid import (
    HyperliquidProvider,
    parse_candles,
    parse_current_funding,
    parse_funding_history,
    parse_mid,
    parse_orderbook,
)


# ── pure parsers ───────────────────────────────────────────────────


def test_parse_orderbook_orders_and_derivatives(hl_payloads) -> None:
    ob = parse_orderbook("btc", hl_payloads["l2Book"])
    assert ob.symbol == "BTC"
    assert ob.best_bid == 66246.0
    assert ob.best_ask == 66247.0
    assert ob.mid == 66246.5
    assert ob.spread == 1.0
    assert ob.venue_time_ms == 1784675040902


def test_parse_orderbook_rejects_crossed_book() -> None:
    crossed = {"coin": "BTC", "time": 1, "levels": [
        [{"px": "100", "sz": "1", "n": 1}], [{"px": "99", "sz": "1", "n": 1}]]}
    with pytest.raises(DataValidationError, match="crossed"):
        parse_orderbook("BTC", crossed)


def test_parse_candles(hl_payloads) -> None:
    candles = parse_candles(hl_payloads["candleSnapshot"])
    assert len(candles) == 2
    assert candles[0].c == 66189.0
    assert candles[0].interval == "1h"
    assert candles[0].trades == 19926


def test_parse_mid_missing_symbol_raises(hl_payloads) -> None:
    assert parse_mid("BTC", hl_payloads["allMids"]).mid == 66210.5
    with pytest.raises(DataValidationError, match="no mid price"):
        parse_mid("DOGE", hl_payloads["allMids"])


def test_parse_funding(hl_payloads) -> None:
    fh = parse_funding_history(hl_payloads["fundingHistory"])
    assert fh[0].rate == pytest.approx(0.0000019598)
    cur = parse_current_funding("BTC", hl_payloads["metaAndAssetCtxs"])
    assert cur.rate == pytest.approx(0.0000035984)


def test_parse_current_funding_unknown_symbol(hl_payloads) -> None:
    with pytest.raises(DataValidationError, match="universe"):
        parse_current_funding("SOL", hl_payloads["metaAndAssetCtxs"])


# ── end-to-end IO with a mocked transport (no network) ─────────────


def _provider(hl_payloads) -> HyperliquidProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        return httpx.Response(200, json=hl_payloads[body["type"]])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HyperliquidProvider(client=client)


def test_provider_methods_roundtrip(hl_payloads) -> None:
    with _provider(hl_payloads) as p:
        assert p.mid_price("BTC").mid == 66210.5
        assert p.orderbook("BTC").best_ask == 66247.0
        assert len(p.candles("BTC", "1h", 0, 1)) == 2
        assert p.current_funding("BTC").rate == pytest.approx(0.0000035984)
        rv = p.realized_vol("BTC", lookback_days=1, interval="1h")
        assert rv.symbol == "BTC"
        assert rv.n_samples == 1  # 2 candles -> 1 return


def test_provider_capabilities(hl_payloads) -> None:
    from tradetk.enums import Capability

    with _provider(hl_payloads) as p:
        caps = p.capabilities()
        assert caps == {
            Capability.SPOT_PRICE,
            Capability.PERP_PRICE,
            Capability.CANDLES,
            Capability.ORDERBOOK,
            Capability.FUNDING,
            Capability.REALIZED_VOL,
        }
