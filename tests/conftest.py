"""Shared fixtures: recorded Hyperliquid payloads (locked against live calls)."""

from __future__ import annotations

import pytest

L2BOOK_BTC = {
    "coin": "BTC",
    "time": 1784675040902,
    "levels": [
        [  # bids, descending
            {"px": "66246.0", "sz": "8.21803", "n": 25},
            {"px": "66245.0", "sz": "1.10000", "n": 3},
        ],
        [  # asks, ascending
            {"px": "66247.0", "sz": "0.15852", "n": 6},
            {"px": "66248.0", "sz": "2.00000", "n": 4},
        ],
    ],
}

CANDLES_BTC = [
    {"t": 1784653200000, "T": 1784656799999, "s": "BTC", "i": "1h",
     "o": "66510.0", "c": "66189.0", "h": "66536.0", "l": "66100.0", "v": "2326.82222", "n": 19926},
    {"t": 1784656800000, "T": 1784660399999, "s": "BTC", "i": "1h",
     "o": "66189.0", "c": "66246.0", "h": "66300.0", "l": "66050.0", "v": "1500.0", "n": 12000},
]

FUNDING_HISTORY_BTC = [
    {"coin": "BTC", "fundingRate": "0.0000019598", "premium": "-0.0004843219", "time": 1784656800028},
]

META_AND_CTXS = [
    {"universe": [{"szDecimals": 5, "name": "BTC", "maxLeverage": 40, "marginTableId": 56}]},
    [{"funding": "0.0000035984", "openInterest": "36304.05", "prevDayPx": "65100.0",
      "markPx": "66247.0", "midPx": "66246.5", "oraclePx": "66278.5", "premium": "-0.0004616882"}],
]

ALL_MIDS = {"BTC": "66210.5", "ETH": "1917.55"}


@pytest.fixture
def hl_payloads() -> dict:
    return {
        "allMids": ALL_MIDS,
        "l2Book": L2BOOK_BTC,
        "candleSnapshot": CANDLES_BTC,
        "fundingHistory": FUNDING_HISTORY_BTC,
        "metaAndAssetCtxs": META_AND_CTXS,
    }
