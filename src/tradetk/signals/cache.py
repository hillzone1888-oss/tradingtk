"""Local candle cache backed by parquet.

One file per ``(symbol, interval)``. Writes merge with existing rows and dedup on
``open_ms`` (keeping the latest fetch), so repeated pulls of overlapping windows
converge to a clean, gap-tolerant series. This is a cache, not the tape — the
recorder (step 5) owns the immutable raw-response history.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from tradetk.signals.base import Candle

_COLUMNS = ["symbol", "interval", "open_ms", "close_ms", "o", "h", "l", "c", "v", "trades"]


class CandleCache:
    def __init__(self, cache_dir: str | Path) -> None:
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str, interval: str) -> Path:
        safe = symbol.upper().replace("/", "_")
        return self._dir / f"candles_{safe}_{interval}.parquet"

    def put(self, candles: list[Candle]) -> None:
        """Merge `candles` into the cache, deduping on open_ms (last wins)."""
        if not candles:
            return
        symbol = candles[0].symbol
        interval = candles[0].interval
        if any(c.symbol != symbol or c.interval != interval for c in candles):
            raise ValueError("put() requires a single (symbol, interval) batch")

        new = pd.DataFrame([c.model_dump() for c in candles], columns=_COLUMNS)
        path = self._path(symbol, interval)
        if path.exists():
            existing = pd.read_parquet(path)
            new = pd.concat([existing, new], ignore_index=True)
        new = (
            new.drop_duplicates(subset="open_ms", keep="last")
            .sort_values("open_ms")
            .reset_index(drop=True)
        )
        new.to_parquet(path, index=False)

    def get(self, symbol: str, interval: str) -> list[Candle]:
        """Return cached candles for the pair, oldest first (empty if none)."""
        path = self._path(symbol, interval)
        if not path.exists():
            return []
        df = pd.read_parquet(path).sort_values("open_ms")
        return [Candle(**row) for row in df.to_dict(orient="records")]
