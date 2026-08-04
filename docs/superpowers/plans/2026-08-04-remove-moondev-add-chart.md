# Remove Moon Dev + add keyless `chart` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the paywalled Moon Dev integration entirely and add a keyless `chart` CLI that renders an underlying's price against a Kalshi contract's implied odds.

**Architecture:** Two independent pieces. (1) A pure *removal*: delete `moondev.py`, the now-orphaned `liquidation_skew` strategy, and the dead capability enums; simplify `record` to a books-only recorder. (2) An *addition*: `cli/chart.py` reads the underlying's OHLC from the existing keyless `HyperliquidProvider.candles()` and the contract's implied-probability history from the existing `TapeReplay`, and renders both stacked to a PNG via matplotlib (Agg backend).

**Tech Stack:** Python 3.12, `uv`, pydantic v2, httpx, matplotlib (new), pytest.

## Global Constraints

- **Python `>=3.12`, managed by `uv`.** Run everything via `uv run …`. `uv sync` after any dependency change; `uv` needs `--native-tls` on this box (corporate/MITM root CA).
- **Runtime HTTP must use the OS trust store.** CLI entrypoints call `truststore.inject_into_ssl()` before any live call (copy the pattern already in `record.py`).
- **Never touch the execute/order path.** This change adds only read-only capability. No code here may contact a venue order endpoint.
- **No credential, `data/tape/`, or `.env` is ever committed.**
- **Line length 100, ruff-clean** (`uv run ruff check .`).
- **The full suite must be green before every commit** (`uv run pytest -q`). Removal tasks are "green" only when no test references a deleted symbol — update or delete those tests in the same task, never `skip` them.
- **Every commit message ends with the trailer** (per repo git rules):
  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01FLkAHjVY1jJn1kMtpTVc3X
  ```

---

## File Structure

**Deleted**
- `src/tradetk/signals/moondev.py`
- `src/tradetk/signals/liquidations.py`
- `src/tradetk/strategy/liquidation_skew.py`
- `tests/test_signals_moondev.py`
- `tests/test_liquidation_skew.py`

**Created**
- `src/tradetk/cli/chart.py` — the `chart` command: pure helpers + orchestration + rendering.
- `tests/test_cli_chart.py` — unit tests for the pure helpers, a render smoke test, and a monkeypatched end-to-end test.

**Modified**
- `src/tradetk/enums.py` — drop `ProviderName.moondev` and the six Moon Dev-only capabilities.
- `src/tradetk/config/schema.py` — drop `moondev_enabled` / `moondev_tier`.
- `src/tradetk/cli/record.py` — remove all Moon Dev signal machinery; books become the sole source.
- `src/tradetk/strategy/__init__.py` — drop the `liquidation_skew` import + `__all__` entry.
- `scripts/validate_provider.py` — reduce to a Hyperliquid-only reachability check.
- `config/config.example.yaml` — drop moondev provider lines, the `moondev:` capabilities row, and the `liquidation_skew` strategy comment block.
- `pyproject.toml` — add `matplotlib`.
- `tests/test_strategy.py`, `tests/test_signals_recorder.py` — remove references to deleted symbols (as needed to stay green).
- Docs/memory: `CLAUDE.md`, `README.md`, `routines/sweep.md`, `routines/weekly-review.md`, `routines/README.md`, `memory/STATE.md`, `memory/GUARDRAILS.md`, `.env.example`.

---

## Task 1: Delete `liquidation_skew` and its liquidations signal

**Files:**
- Delete: `src/tradetk/strategy/liquidation_skew.py`, `src/tradetk/signals/liquidations.py`, `tests/test_liquidation_skew.py`
- Modify: `src/tradetk/strategy/__init__.py`, `tests/test_strategy.py`

**Interfaces:**
- Consumes: nothing.
- Produces: after this task, `available_strategies()` returns exactly `{"baseline_vol"}`; the name `LiquidationSkewStrategy` no longer exists anywhere.

- [ ] **Step 1: See what references the doomed symbols**

Run: `git grep -n -E "liquidation_skew|LiquidationSkew|signals\.liquidations|LiquidationProfile|LiquidationEvent|build_liquidation_profile"`
Expected: hits in the three files to delete, `strategy/__init__.py`, and possibly `tests/test_strategy.py`.

- [ ] **Step 2: Delete the three files**

```bash
git rm src/tradetk/strategy/liquidation_skew.py src/tradetk/signals/liquidations.py tests/test_liquidation_skew.py
```

- [ ] **Step 3: Remove the registration from `strategy/__init__.py`**

Delete this import line:
```python
from tradetk.strategy.liquidation_skew import LiquidationSkewStrategy  # noqa: E402,F401
```
and remove `"LiquidationSkewStrategy",` from the `__all__` list.

- [ ] **Step 4: Purge references in `tests/test_strategy.py`**

Remove every test function and assertion that imports or names `LiquidationSkewStrategy` / `liquidation_skew` (from Step 1's hits). Do not `skip` them — delete them. Leave the `baseline_vol` tests untouched.

- [ ] **Step 5: Confirm no references remain**

Run: `git grep -n -E "liquidation_skew|LiquidationSkew|signals\.liquidations|LiquidationProfile|LiquidationEvent"`
Expected: no output.

- [ ] **Step 6: Run the affected tests, then the full suite**

Run: `uv run pytest tests/test_strategy.py -q && uv run pytest -q`
Expected: PASS (green). `available_strategies()` now yields only `baseline_vol`.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "Remove liquidation_skew: no liquidations provider remains after dropping Moon Dev"
```

---

## Task 2: Delete the Moon Dev provider and its enums/config

**Files:**
- Delete: `src/tradetk/signals/moondev.py`, `tests/test_signals_moondev.py`
- Modify: `src/tradetk/enums.py`, `src/tradetk/config/schema.py`, `config/config.example.yaml`, `scripts/validate_provider.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ProviderName` has only `hyperliquid` and `polymarket_us`; `Capability` has only the six Hyperliquid-native members (`SPOT_PRICE`, `PERP_PRICE`, `CANDLES`, `ORDERBOOK`, `FUNDING`, `REALIZED_VOL`); `ProviderConfig` has no `moondev_*` fields.

- [ ] **Step 1: Delete the provider and its test**

```bash
git rm src/tradetk/signals/moondev.py tests/test_signals_moondev.py
```

- [ ] **Step 2: Trim `src/tradetk/enums.py`**

In `class ProviderName`, delete the line:
```python
    moondev = "moondev"  # opt-in; paid tier exceeds a $20 book
```
In `class Capability`, delete the entire "Moon Dev-only signals" block (these six members and their comment):
```python
    # ── Moon Dev-only signals (no native equivalent) ──
    LIQUIDATIONS = "liquidations"
    HLP_SENTIMENT = "hlp_sentiment"
    POSITION_SNAPSHOTS = "position_snapshots"
    SMART_MONEY = "smart_money"
    ORDER_FLOW = "order_flow"
    POLY_WHALES = "poly_whales"  # Polymarket GLOBAL flow — external signal only
```
Keep the six native capabilities above them.

- [ ] **Step 3: Trim `src/tradetk/config/schema.py`**

In `class ProviderConfig`, delete these two fields:
```python
    moondev_enabled: bool = False
    moondev_tier: str = Field(default="standard", pattern="^(standard|qe)$")
```
Leave `primary` and `capabilities`.

- [ ] **Step 4: Fix `config/config.example.yaml`**

Replace the whole `provider:` block with:
```yaml
provider:
  primary: hyperliquid             # native HL is default + fallback (read-only)
  capabilities:
    hyperliquid: [spot_price, perp_price, candles, orderbook, funding, realized_vol]
```
In the `strategy:` block, set the name comment to `# baseline_vol` only and delete the entire commented `liquidation_skew` params block (the `# params:` lines through `# max_horizon_hours`).

- [ ] **Step 5: Reduce `scripts/validate_provider.py` to Hyperliquid-only**

Rewrite it so it no longer imports or checks Moon Dev. Replace the file body with:
```python
"""Report Hyperliquid reachability. Non-zero exit if unreachable, so it can gate a run."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import truststore

from tradetk.signals.hyperliquid import HyperliquidProvider


def _check_hyperliquid(symbol: str) -> dict[str, Any]:
    try:
        with HyperliquidProvider() as hl:
            snap = hl.mid_price(symbol)
        return {"reachable": True, "symbol": snap.symbol, "mid": snap.mid}
    except Exception as exc:  # noqa: BLE001 - report, don't crash the check
        return {"reachable": False, "error": f"{type(exc).__name__}: {exc}"}


def run(symbol: str) -> dict[str, Any]:
    truststore.inject_into_ssl()  # OS trust store for the corporate/MITM CA
    hl = _check_hyperliquid(symbol)
    return {"symbol": symbol, "providers": {"hyperliquid": hl}, "ok": hl["reachable"]}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Check Hyperliquid reachability.")
    ap.add_argument("--symbol", default="BTC")
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args(argv)
    result = run(args.symbol)
    print(json.dumps(result, indent=2 if args.pretty else None))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

- [ ] **Step 6: Confirm no Moon Dev references remain in code/config**

Run: `git grep -n -iE "moondev|MOONDEV_API_KEY|poly_whales|PolyWhale|HLP_SENTIMENT|SMART_MONEY|ORDER_FLOW|POSITION_SNAPSHOTS" -- src config scripts`
Expected: no output. (Docs/memory are handled in Task 4.)

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS. If a test in `tests/test_signals_recorder.py` or elsewhere imported `MoonDevProvider` or a deleted `Capability`, remove that reference now (do not skip) and re-run.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "Remove Moon Dev provider, enums, and config"
```

---

## Task 3: Simplify `record` to a books-only recorder

**Files:**
- Modify: `src/tradetk/cli/record.py`, `routines/sweep.md`
- Test: `tests/test_cli_record.py` (verify it still passes; it imports only `next_interval`, `poll_all`, `TapeSource`, `TapeWriter`, all of which remain)

**Interfaces:**
- Consumes: `build_book_sources(venue, *, max_hours, depth, max_markets)` and `poll_all` (unchanged).
- Produces: `record` has no `--books`, `--no-signals`, `--source`, or `--tier` flags and no `MOONDEV_API_KEY` gate. Kalshi books are recorded unconditionally. The recorder command is `uv run python -m tradetk.cli.record --once --pretty`.

- [ ] **Step 1: Remove the Moon Dev imports and module tables from `record.py`**

Delete the import:
```python
from tradetk.signals.moondev import TIER_CAPS, MoonDevProvider
```
Delete the `MOONDEV_SOURCES` dict and the entire `build_moondev_sources(...)` function.

- [ ] **Step 2: Remove the Moon Dev / signal flags in `main()`**

Delete these argparse lines: `--tier`, `--source`, `--books`, and `--no-signals`. Keep `--market-data-env`, `--book-depth`, `--book-max-hours`, `--book-max-markets`, `--once`, `--daemon`, `--interval`, `--no-adaptive`, `--min-interval`, `--tape-dir`, `--pretty`.

- [ ] **Step 3: Delete the `MOONDEV_API_KEY` gate and signal-source assembly**

Remove this block entirely:
```python
    want_signals = not args.no_signals
    key = os.environ.get("MOONDEV_API_KEY")
    if want_signals and not key:
        print(json.dumps({"ok": False,
                          "error": "MOONDEV_API_KEY is not set; pass --no-signals to record "
                                   "books only."}, indent=indent))
        return 2
```
In the `with ExitStack() as stack:` body, remove the `if want_signals:` branch (the `MoonDevProvider` context + `build_moondev_sources` call). Replace the `if args.books:` conditional with an unconditional books build so the block reads:
```python
    with ExitStack() as stack:
        sources: list[TapeSource] = []
        # Read-only market data. The adapter has no order endpoint, so this
        # cannot touch execution regardless of environment.
        venue = stack.enter_context(KalshiVenue(args.market_data_env))
        book_sources, discovery = build_book_sources(
            venue, max_hours=args.book_max_hours, depth=args.book_depth,
            max_markets=args.book_max_markets,
        )
        discovery["market_data_environment"] = args.market_data_env
        sources += book_sources

        if not sources:
            print(json.dumps({"ok": False, "error": "no eligible markets to record",
                              "discovery": discovery}, indent=indent))
            return 2
```
Leave the `--once` poll path and the `--daemon` loop below it unchanged. If `os` is now unused, remove its import; keep it if still referenced.

- [ ] **Step 4: Update the module docstring**

The docstring's Moon Dev example lines (`--daemon --interval 900` whale-log note, etc.) should be reduced to books-only usage:
```python
    uv run python -m tradetk.cli.record --once --pretty
    uv run python -m tradetk.cli.record --daemon --interval 300
```
Remove the "Moon Dev whale log reaches back ~4 hours" paragraph.

- [ ] **Step 5: Run the record tests, then the full suite**

Run: `uv run pytest tests/test_cli_record.py -q && uv run pytest -q`
Expected: PASS. `test_metadata_is_recorded_before_books` still passes (it calls `build_book_sources` directly, which is unchanged).

- [ ] **Step 6: Smoke-test keyless behaviour locally**

Run: `MOONDEV_API_KEY= uv run python -m tradetk.cli.record --once --pretty` (or on PowerShell: `$env:MOONDEV_API_KEY=""; uv run python -m tradetk.cli.record --once --pretty`)
Expected: exits 0 with `"ok": true` and a `discovery.recording_books_for` count — no key gate. (Network permitting; if Kalshi is unreachable the point is only that no key error appears.)

- [ ] **Step 7: Fix the sweep routine command**

In `routines/sweep.md`, change the record command to:
```
uv run python -m tradetk.cli.record --once --pretty
```
Delete the entire "Two flags here are load-bearing" section about `--no-signals` and `--json`. Keep the "judge success by `discovery.recording_books_for`" guidance.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "record: books-only recorder; drop Moon Dev signal flags and key gate"
```

---

## Task 4: Update docs and memory for the removal

**Files:**
- Modify: `CLAUDE.md`, `README.md`, `.env.example`, `routines/weekly-review.md`, `routines/README.md`, `memory/STATE.md`, `memory/GUARDRAILS.md`

**Interfaces:** none (documentation only).

- [ ] **Step 1: Find every remaining Moon Dev / liquidation reference in docs**

Run: `git grep -n -iE "moondev|moon dev|MOONDEV_API_KEY|liquidation_skew|liquidations" -- CLAUDE.md README.md .env.example routines memory`
Expected: a list to work through.

- [ ] **Step 2: `CLAUDE.md` — data venue is Hyperliquid-only**

In the "Venues" section, change the data-venue line to state the data venue is **Hyperliquid only** (read-only, no orders). Remove "Moon Dev" from it. Leave the execute-boundary and Kalshi lines intact.

- [ ] **Step 3: `.env.example`**

Delete the `MOONDEV_API_KEY=` line (and any comment describing it).

- [ ] **Step 4: `README.md`**

Remove Moon Dev from any provider/setup list; state Hyperliquid is the sole read-only signal source. Remove `liquidation_skew` from any strategy list, leaving `baseline_vol`.

- [ ] **Step 5: `routines/weekly-review.md` and `routines/README.md`**

Delete the `liquidation_skew` "not runnable / do not report a score" guidance (Step 4 of weekly-review) — the strategy no longer exists. Remove any Moon Dev mention.

- [ ] **Step 6: `memory/GUARDRAILS.md`**

Remove any Moon Dev read-only-provider clause if present (the security posture is now: Hyperliquid is the only read-only signal source; nothing is ever sent to it).

- [ ] **Step 7: `memory/STATE.md` — reflect the new reality**

Replace the "Strategies" section with: `baseline_vol` is the only strategy; note `liquidation_skew` (step 13) was **removed 2026-08-04** as permanently unrunnable after dropping the paywalled Moon Dev liquidations feed. In "Venue and environment", change the signal-source line to Hyperliquid-only. Delete both "FIXED 2026-08-03 — sweep failed" and the `--no-signals` narrative (obsolete: there is no key gate anymore). Update the "Last updated" line to `2026-08-04`.

- [ ] **Step 8: Confirm the docs are clean**

Run: `git grep -n -iE "moondev|moon dev|MOONDEV_API_KEY|liquidation_skew" -- CLAUDE.md README.md .env.example routines memory`
Expected: no output (a passing mention in a dated historical note in `memory/DECISIONS.md` is acceptable and out of scope here).

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "Docs+memory: Hyperliquid-only data venue; note liquidation_skew removed"
```

---

## Task 5: Add matplotlib and the `implied_prob_series` helper

**Files:**
- Modify: `pyproject.toml`
- Create: `src/tradetk/cli/chart.py`
- Test: `tests/test_cli_chart.py`

**Interfaces:**
- Consumes: `BookObservation` (from `tradetk.backtest.replay`) with `.ticker: str`, `.observed_at: datetime`, `.book: BinaryBook`; `BinaryBook.mid -> Decimal | None`.
- Produces: `implied_prob_series(observations: Iterable[BookObservation], ticker: str) -> list[tuple[datetime, float]]` — time-ordered `(observed_at, yes_mid_as_float)` for the given ticker, skipping snapshots whose `book.mid` is `None` (one-sided books).

- [ ] **Step 1: Add matplotlib to `pyproject.toml`**

In `[project].dependencies`, add:
```toml
    "matplotlib>=3.8",
```

- [ ] **Step 2: Sync**

Run: `uv sync --native-tls`
Expected: matplotlib resolves and installs (pure wheels; no compiler needed).

- [ ] **Step 3: Write the failing test**

Create `tests/test_cli_chart.py`:
```python
"""Unit tests for the `chart` CLI helpers (no network, no venue)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tradetk.backtest.replay import BookObservation
from tradetk.cli.chart import implied_prob_series
from tradetk.venues.base import BinaryBook, BookLevel


def _obs(ticker: str, minute: int, bid: str | None, ask: str | None) -> BookObservation:
    when = datetime(2026, 8, 4, 12, minute, tzinfo=timezone.utc)
    book = BinaryBook(
        ticker=ticker,
        retrieved_at=when,
        yes_bids=[BookLevel(price=Decimal(bid), size=Decimal("10"))] if bid else [],
        yes_asks=[BookLevel(price=Decimal(ask), size=Decimal("10"))] if ask else [],
    )
    return BookObservation(ticker=ticker, observed_at=when, book=book)


def test_implied_prob_series_filters_ticker_and_computes_mid() -> None:
    obs = [
        _obs("KXBTCD-A", 0, "0.40", "0.42"),   # mid 0.41
        _obs("KXETHD-Z", 1, "0.10", "0.12"),   # other ticker, excluded
        _obs("KXBTCD-A", 2, "0.44", "0.46"),   # mid 0.45
    ]
    series = implied_prob_series(obs, "KXBTCD-A")
    assert [round(p, 2) for _, p in series] == [0.41, 0.45]
    assert [dt.minute for dt, _ in series] == [0, 2]


def test_implied_prob_series_skips_one_sided_books() -> None:
    obs = [
        _obs("KXBTCD-A", 0, "0.40", None),     # no ask -> mid None -> skipped
        _obs("KXBTCD-A", 1, "0.44", "0.46"),   # mid 0.45
    ]
    series = implied_prob_series(obs, "KXBTCD-A")
    assert [round(p, 2) for _, p in series] == [0.45]
```

- [ ] **Step 4: Run it to confirm it fails**

Run: `uv run pytest tests/test_cli_chart.py -q`
Expected: FAIL — `ImportError: cannot import name 'implied_prob_series' from 'tradetk.cli.chart'` (module/function does not exist yet).

- [ ] **Step 5: Create `chart.py` with the helper**

Create `src/tradetk/cli/chart.py`:
```python
"""``chart`` — view an underlying's price against a Kalshi contract's implied odds.

Read-only and keyless. The underlying OHLC comes from Hyperliquid's public
``candleSnapshot``; the contract's implied-probability history is reconstructed
from book snapshots this project recorded itself (the same tape the shadow
evaluator reads). Renders a two-panel PNG so both the assistant and the operator
can actually look at the price action while designing a strategy.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from tradetk.backtest.replay import BookObservation


def implied_prob_series(
    observations: Iterable[BookObservation], ticker: str
) -> list[tuple[datetime, float]]:
    """Time-ordered ``(observed_at, yes_mid)`` for ``ticker``.

    ``yes_mid`` is the book's informational midpoint — implied probability. A
    one-sided book (no bid or no ask) has no mid and is skipped rather than
    guessed; a chart that invented a price on a half-empty book would mislead.
    """
    out: list[tuple[datetime, float]] = []
    for obs in observations:
        if obs.ticker != ticker:
            continue
        mid = obs.book.mid
        if mid is None:
            continue
        out.append((obs.observed_at, float(mid)))
    out.sort(key=lambda row: row[0])
    return out
```

- [ ] **Step 6: Run the tests to confirm they pass**

Run: `uv run pytest tests/test_cli_chart.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "chart: add matplotlib dep and implied_prob_series helper"
```

---

## Task 6: Candle-to-series and time-span helpers

**Files:**
- Modify: `src/tradetk/cli/chart.py`, `tests/test_cli_chart.py`

**Interfaces:**
- Consumes: `Candle` (from `tradetk.signals.base`) with `.open_ms: int`, `.o/.h/.l/.c: float`.
- Produces:
  - `candles_to_ohlc(candles: Iterable[Candle]) -> tuple[list[datetime], list[float], list[float], list[float], list[float]]` — `(times, opens, highs, lows, closes)`, time-ordered, `times` are tz-aware UTC.
  - `series_span(series: list[tuple[datetime, float]]) -> tuple[datetime, datetime]` — first and last timestamps; raises `ValueError` on an empty series.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli_chart.py`:
```python
from tradetk.cli.chart import candles_to_ohlc, series_span
from tradetk.signals.base import Candle


def _candle(open_ms: int, o: float, h: float, l: float, c: float) -> Candle:
    return Candle(
        symbol="BTC", interval="1h", open_ms=open_ms, close_ms=open_ms + 3_600_000,
        o=o, h=h, l=l, c=c, v=1.0, trades=1,
    )


def test_candles_to_ohlc_orders_and_converts() -> None:
    times, o, h, l, c = candles_to_ohlc([
        _candle(1_000_000, 100, 110, 95, 105),
        _candle(4_600_000, 105, 120, 104, 118),
    ])
    assert [t.tzinfo is not None for t in times] == [True, True]
    assert times[0] < times[1]
    assert o == [100.0, 105.0]
    assert h == [110.0, 120.0]
    assert l == [95.0, 104.0]
    assert c == [105.0, 118.0]


def test_series_span_returns_first_and_last() -> None:
    s = [
        (datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc), 0.4),
        (datetime(2026, 8, 4, 13, 0, tzinfo=timezone.utc), 0.5),
    ]
    start, end = series_span(s)
    assert start.hour == 12 and end.hour == 13
```

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/test_cli_chart.py -q`
Expected: FAIL — `cannot import name 'candles_to_ohlc'`.

- [ ] **Step 3: Implement the helpers**

Add to `src/tradetk/cli/chart.py` (add `from datetime import datetime, timezone` to the imports, and `from tradetk.signals.base import Candle`):
```python
def candles_to_ohlc(
    candles: Iterable[Candle],
) -> tuple[list[datetime], list[float], list[float], list[float], list[float]]:
    """Split candles into parallel, time-ordered plotting arrays (UTC)."""
    rows = sorted(candles, key=lambda k: k.open_ms)
    times = [datetime.fromtimestamp(k.open_ms / 1000.0, tz=timezone.utc) for k in rows]
    return (
        times,
        [float(k.o) for k in rows],
        [float(k.h) for k in rows],
        [float(k.l) for k in rows],
        [float(k.c) for k in rows],
    )


def series_span(series: list[tuple[datetime, float]]) -> tuple[datetime, datetime]:
    """First and last timestamp of a non-empty ``(time, value)`` series."""
    if not series:
        raise ValueError("cannot take the span of an empty series")
    times = [row[0] for row in series]
    return min(times), max(times)
```

- [ ] **Step 4: Run to confirm pass**

Run: `uv run pytest tests/test_cli_chart.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chart: candle-to-series and span helpers"
```

---

## Task 7: The renderer — two panels to a PNG

**Files:**
- Modify: `src/tradetk/cli/chart.py`, `tests/test_cli_chart.py`

**Interfaces:**
- Consumes: the outputs of `implied_prob_series` and `candles_to_ohlc`.
- Produces: `render_chart(*, ticker: str, symbol: str, prob_series: list[tuple[datetime, float]], ohlc: tuple[list[datetime], list[float], list[float], list[float], list[float]], out_path: str, strike: float | None = None) -> str` — writes a PNG to `out_path` (creating parent dirs) and returns `out_path`. Top panel: underlying close line with a high–low shaded band and an optional strike line. Bottom panel: implied probability over time, y-axis fixed to `[0, 1]`. Shared x-axis. Uses the non-interactive `Agg` backend.

- [ ] **Step 1: Write the failing smoke test**

Append to `tests/test_cli_chart.py`:
```python
from tradetk.cli.chart import render_chart


def test_render_chart_writes_a_nonempty_png(tmp_path) -> None:
    t0 = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 8, 4, 13, 0, tzinfo=timezone.utc)
    prob = [(t0, 0.41), (t1, 0.45)]
    ohlc = ([t0, t1], [100.0, 105.0], [110.0, 120.0], [95.0, 104.0], [105.0, 118.0])
    out = tmp_path / "chart.png"
    result = render_chart(
        ticker="KXBTCD-A", symbol="BTC", prob_series=prob, ohlc=ohlc,
        out_path=str(out), strike=112.0,
    )
    assert result == str(out)
    assert out.exists()
    assert out.stat().st_size > 1000  # a real PNG, not an empty file
```

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/test_cli_chart.py::test_render_chart_writes_a_nonempty_png -q`
Expected: FAIL — `cannot import name 'render_chart'`.

- [ ] **Step 3: Implement `render_chart`**

At the very top of `src/tradetk/cli/chart.py` (before importing `pyplot`), pin the Agg backend, and add the function. The backend line must run before `matplotlib.pyplot` is imported:
```python
import os

import matplotlib

matplotlib.use("Agg")  # headless: never try to open a window
import matplotlib.pyplot as plt  # noqa: E402 - must follow matplotlib.use


def render_chart(
    *,
    ticker: str,
    symbol: str,
    prob_series: list[tuple[datetime, float]],
    ohlc: tuple[list[datetime], list[float], list[float], list[float], list[float]],
    out_path: str,
    strike: float | None = None,
) -> str:
    """Render underlying price (top) vs. implied odds (bottom) to a PNG."""
    times, _opens, highs, lows, closes = ohlc
    fig, (ax_price, ax_prob) = plt.subplots(
        2, 1, figsize=(11, 7), sharex=True, height_ratios=[2, 1]
    )

    # Top: underlying close with a high–low band.
    if times:
        ax_price.plot(times, closes, color="#1f77b4", linewidth=1.3, label=f"{symbol} close")
        ax_price.fill_between(times, lows, highs, color="#1f77b4", alpha=0.15, label="high–low")
    if strike is not None:
        ax_price.axhline(strike, color="#d62728", linestyle="--", linewidth=1.0, label="strike")
    ax_price.set_ylabel(f"{symbol} price")
    ax_price.set_title(f"{ticker}  —  {symbol} price vs. contract implied odds")
    ax_price.legend(loc="upper left", fontsize=8)
    ax_price.grid(True, alpha=0.25)

    # Bottom: implied probability, fixed 0..1.
    if prob_series:
        p_times = [row[0] for row in prob_series]
        p_vals = [row[1] for row in prob_series]
        ax_prob.plot(p_times, p_vals, color="#2ca02c", linewidth=1.3, marker=".", markersize=4)
    ax_prob.set_ylim(0.0, 1.0)
    ax_prob.set_ylabel("implied P(yes)")
    ax_prob.set_xlabel("time (UTC)")
    ax_prob.grid(True, alpha=0.25)

    fig.autofmt_xdate()
    fig.tight_layout()
    parent = os.path.dirname(out_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path
```

- [ ] **Step 4: Run to confirm pass**

Run: `uv run pytest tests/test_cli_chart.py -q`
Expected: PASS (all chart tests).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chart: two-panel PNG renderer (price vs. implied odds)"
```

---

## Task 8: Wire `main()` — load tape, fetch candles, render, report

**Files:**
- Modify: `src/tradetk/cli/chart.py`, `tests/test_cli_chart.py`

**Interfaces:**
- Consumes: `TapeReplay.from_tape(tape_dir)` → `.observations()`, `.tickers`, `.span`, `.claim_as_of(ticker, when, registry)`; `UnderlyingRegistry.from_yaml(path)`; `HyperliquidProvider().candles(symbol, interval, start_ms, end_ms)`; the three helpers above.
- Produces: `main(argv: list[str]) -> int` and a module `__main__` guard, so `python -m tradetk.cli.chart` runs. Emits one JSON object on stdout: `{"ok": bool, "out": str, "ticker": str, "symbol": str, "prob_points": int, "candles": int, "span": [iso, iso]}` (or `{"ok": false, "error": ...}`). To keep it testable without a network, `main` resolves the candle provider through a module-level `_provider_factory()` that tests monkeypatch.

- [ ] **Step 1: Write the failing end-to-end test (monkeypatched, no network)**

Append to `tests/test_cli_chart.py`:
```python
import json

from tradetk.cli import chart as chart_mod


class _FakeProvider:
    def __enter__(self): return self
    def __exit__(self, *exc): return None
    def candles(self, symbol, interval, start_ms, end_ms):
        return [_candle(start_ms, 100, 110, 95, 105), _candle(start_ms + 3_600_000, 105, 120, 104, 118)]


def test_main_renders_and_reports(tmp_path, monkeypatch, capsys) -> None:
    ticker = "KXBTCD-A"
    obs = [_obs(ticker, 0, "0.40", "0.42"), _obs(ticker, 30, "0.44", "0.46")]

    class _Replay:
        @classmethod
        def from_tape(cls, tape_dir): return cls()
        def observations(self): return iter(obs)
        @property
        def tickers(self): return {ticker}
        @property
        def span(self):
            return obs[0].observed_at, obs[-1].observed_at
        def claim_as_of(self, tk, when, registry):
            class _C: underlying = "BTC"; threshold = Decimal("112")
            return _C()

    monkeypatch.setattr(chart_mod, "TapeReplay", _Replay)
    monkeypatch.setattr(chart_mod, "UnderlyingRegistry",
                        type("R", (), {"from_yaml": staticmethod(lambda p: object())}))
    monkeypatch.setattr(chart_mod, "_provider_factory", lambda: _FakeProvider())

    out = tmp_path / "out.png"
    rc = chart_mod.main(["--ticker", ticker, "--out", str(out), "--tape-dir", "unused",
                         "--registry", "unused"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["symbol"] == "BTC"
    assert payload["prob_points"] == 2
    assert payload["candles"] == 2
    assert out.exists() and out.stat().st_size > 1000


def test_main_errors_when_ticker_absent_from_tape(tmp_path, monkeypatch, capsys) -> None:
    class _Empty:
        @classmethod
        def from_tape(cls, tape_dir): return cls()
        def observations(self): return iter([])
        @property
        def tickers(self): return set()
        @property
        def span(self):
            raise IndexError
        def claim_as_of(self, tk, when, registry): return None

    monkeypatch.setattr(chart_mod, "TapeReplay", _Empty)
    monkeypatch.setattr(chart_mod, "UnderlyingRegistry",
                        type("R", (), {"from_yaml": staticmethod(lambda p: object())}))
    rc = chart_mod.main(["--ticker", "KXBTCD-A", "--out", str(tmp_path / "x.png"),
                         "--tape-dir", "unused", "--registry", "unused", "--symbol", "BTC"])
    assert rc == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False
```

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/test_cli_chart.py -q`
Expected: FAIL — `module 'tradetk.cli.chart' has no attribute 'main'` (and `TapeReplay`/`UnderlyingRegistry`/`_provider_factory` not defined).

- [ ] **Step 3: Implement `main` and its collaborators**

Add the imports and functions to `src/tradetk/cli/chart.py`:
```python
import argparse
import json
import sys
from datetime import timezone as _tz

import truststore

from tradetk.backtest.replay import ReplayError, TapeReplay
from tradetk.signals.hyperliquid import HyperliquidProvider
from tradetk.translation.claims import UnderlyingRegistry


def _provider_factory() -> HyperliquidProvider:
    """Indirection so tests can substitute a fake candle provider."""
    return HyperliquidProvider()


def _default_out(ticker: str) -> str:
    stamp = datetime.now(_tz.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"data/charts/{ticker}-{stamp}.png"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Chart an underlying vs. a contract's implied odds.")
    ap.add_argument("--ticker", required=True, help="Kalshi contract ticker to chart.")
    ap.add_argument("--interval", default="1h", help="Hyperliquid candle interval (default 1h).")
    ap.add_argument("--tape-dir", default="data/tape")
    ap.add_argument("--registry", default="config/underlyings.yaml")
    ap.add_argument("--symbol", default=None,
                    help="Underlying symbol override; inferred from the tape's claim if omitted.")
    ap.add_argument("--out", default=None, help="PNG path (default data/charts/<ticker>-<ts>.png).")
    args = ap.parse_args(argv)

    truststore.inject_into_ssl()
    out_path = args.out or _default_out(args.ticker)

    try:
        replay = TapeReplay.from_tape(args.tape_dir)
    except ReplayError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2

    prob = implied_prob_series(replay.observations(), args.ticker)
    if not prob:
        print(json.dumps({"ok": False, "error":
                          f"no book observations for {args.ticker!r} on the tape at "
                          f"{args.tape_dir}; run `record` for it first"}))
        return 2

    start, end = series_span(prob)

    # Underlying symbol + strike, inferred from the contract's claim unless overridden.
    symbol = args.symbol
    strike: float | None = None
    claim = replay.claim_as_of(args.ticker, end, UnderlyingRegistry.from_yaml(args.registry))
    if claim is not None:
        symbol = symbol or getattr(claim, "underlying", None)
        thr = getattr(claim, "threshold", None)
        strike = float(thr) if thr is not None else None
    if not symbol:
        print(json.dumps({"ok": False, "error":
                          f"could not infer underlying for {args.ticker!r}; pass --symbol"}))
        return 2

    start_ms = int(start.timestamp() * 1000) - 3_600_000  # small left pad
    end_ms = int(end.timestamp() * 1000)
    with _provider_factory() as provider:
        candles = provider.candles(symbol, args.interval, start_ms, end_ms)
    ohlc = candles_to_ohlc(candles)

    render_chart(ticker=args.ticker, symbol=symbol, prob_series=prob, ohlc=ohlc,
                 out_path=out_path, strike=strike)

    print(json.dumps({
        "ok": True, "out": out_path, "ticker": args.ticker, "symbol": symbol,
        "prob_points": len(prob), "candles": len(candles),
        "span": [start.isoformat(), end.isoformat()],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```
Note: `_provider_factory` returns a real provider used as a context manager (`with … as provider`), so the fake in the test implements `__enter__`/`__exit__`.

- [ ] **Step 4: Run the full chart test file**

Run: `uv run pytest tests/test_cli_chart.py -q`
Expected: PASS (all helper, render, and main tests).

- [ ] **Step 5: Ruff + full suite**

Run: `uv run ruff check src/tradetk/cli/chart.py tests/test_cli_chart.py && uv run pytest -q`
Expected: clean + green.

- [ ] **Step 6: Real smoke test against the committed tape (network permitting)**

Run: `uv run python -m tradetk.cli.chart --ticker <a ticker present in data/tape> --out data/charts/smoke.png`
(Find a present ticker with `uv run python -m tradetk.cli.shadow --stats --pretty` or by inspecting the tape.)
Expected: `"ok": true` JSON and a PNG at `data/charts/smoke.png`. If Hyperliquid is unreachable behind the corporate CA, the JSON error will name the network cause — that is acceptable for this step; the monkeypatched tests are the correctness gate.

- [ ] **Step 7: Ensure `data/charts/` is not committed as evidence**

Confirm `.gitignore` ignores `data/*` except the shadow log (it already does: `data/*` then `!data/shadow/`). `data/charts/` is therefore ignored. Do **not** add a negation for it.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "chart: wire main (tape + Hyperliquid candles -> PNG report)"
```

---

## Self-Review

**Spec coverage:**
- Remove `moondev.py` + test → Task 2. ✓
- Strip Moon Dev from `record.py`, books-only, sweep command fixed → Task 3. ✓
- Remove Moon Dev enums/config/schema, simplify `validate_provider.py` → Task 2. ✓
- Delete `liquidation_skew` + `signals/liquidations.py` + tests, `__init__` update → Task 1. ✓
- Docs/memory (`CLAUDE.md`, `README.md`, routines, `STATE.md`, `GUARDRAILS.md`, `.env.example`) → Task 4. ✓
- `chart` command: Hyperliquid candles (top) + tape implied-prob (bottom), matplotlib Agg PNG, JSON report, `--symbol`/`--interval`, default out path, error when ticker absent → Tasks 5–8. ✓
- matplotlib dependency → Task 5. ✓
- Pure helpers unit-tested + render smoke test + no network in tests → Tasks 5–8. ✓
- `data/charts/` not committed → Task 8 Step 7. ✓

**Placeholder scan:** No TBD/TODO; every code step carries real code. Deletion steps specify exact lines/blocks and a grep to prove completion.

**Type consistency:** `implied_prob_series(observations, ticker) -> list[tuple[datetime, float]]`, `candles_to_ohlc(...) -> (times, o, h, l, c)`, `series_span(series) -> (start, end)`, and `render_chart(*, ticker, symbol, prob_series, ohlc, out_path, strike=None) -> str` are used with matching names/signatures in `main` (Task 8) and in the tests. `BinaryBook.mid`, `Candle.open_ms/o/h/l/c`, `BookObservation.ticker/observed_at/book`, `TapeReplay.from_tape/observations/span/claim_as_of`, `UnderlyingRegistry.from_yaml`, and `HyperliquidProvider.candles` all match the source read during planning.
