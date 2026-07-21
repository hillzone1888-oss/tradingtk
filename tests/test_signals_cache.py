"""Candle cache: parquet round-trip and dedup-on-merge."""

from __future__ import annotations

from tradetk.signals.base import Candle
from tradetk.signals.cache import CandleCache


def _candle(open_ms: int, close: float) -> Candle:
    return Candle(symbol="BTC", interval="1h", open_ms=open_ms, close_ms=open_ms + 3600_000,
                  o=close, h=close, l=close, c=close, v=1.0, trades=1)


def test_roundtrip(tmp_path) -> None:
    cache = CandleCache(tmp_path)
    cache.put([_candle(0, 100.0), _candle(3600_000, 101.0)])
    got = cache.get("BTC", "1h")
    assert [c.open_ms for c in got] == [0, 3600_000]
    assert got[1].c == 101.0


def test_merge_dedups_on_open_ms_last_wins(tmp_path) -> None:
    cache = CandleCache(tmp_path)
    cache.put([_candle(0, 100.0), _candle(3600_000, 101.0)])
    # Re-fetch overlapping window with a corrected close for open_ms=0.
    cache.put([_candle(0, 999.0), _candle(7200_000, 102.0)])
    got = cache.get("BTC", "1h")
    assert [c.open_ms for c in got] == [0, 3600_000, 7200_000]
    assert got[0].c == 999.0  # last write wins


def test_get_missing_returns_empty(tmp_path) -> None:
    assert CandleCache(tmp_path).get("ETH", "1d") == []
