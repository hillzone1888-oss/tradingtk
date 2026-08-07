"""Tests for Kalshi venue adapter."""

from tradetk.venues.kalshi import parse_market


def test_parse_market_maps_settled_result():
    m = parse_market({"ticker": "T1", "status": "finalized", "result": "yes", "title": "x"})
    assert m.result == "yes"


def test_parse_market_result_is_none_when_open():
    m = parse_market({"ticker": "T1", "status": "open", "title": "x"})
    assert m.result is None
