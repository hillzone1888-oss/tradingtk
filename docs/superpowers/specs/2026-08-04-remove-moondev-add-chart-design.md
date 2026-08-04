# Remove Moon Dev; add a keyless `chart` command

**Date:** 2026-08-04
**Status:** proposed — awaiting user review
**Author:** pairing session

## Why

The Moon Dev API is paywalled with no free tier (site currently bundles the key
+ courses for $1,395 lifetime; the key alone is advertised at "$10,000 value").
The user is dropping it. Two consequences drove this design once the API surface
was actually inspected:

1. **Moon Dev exposes no price/OHLC/chart data at all** — only positions, whale
   flow, and liquidations. So "let me view chart data" cannot be served by Moon
   Dev regardless. Chart data has to come from elsewhere.
2. **Hyperliquid already provides keyless candles** (`candleSnapshot`, wired and
   tested in `HyperliquidProvider.candles()`). It is the existing read-only
   signal source and costs nothing. That is the underlying-price source.

So the work splits cleanly into a **removal** and an **addition**, with no new
paid dependency and — notably — no new HTTP data provider at all: both chart
sources already exist in the codebase.

## Non-goals

- No Binance/Coinbase integration. Hyperliquid candles cover the underlying.
- No change to the deterministic pipeline (probability → edge → sizing → gate),
  to the execute boundary, or to any venue/order path.
- "Use Moon Dev's website to craft strategies" is a *workflow*, not code. Strategy
  ideas come from his free content and get implemented as deterministic tradetk
  strategies. Rendering the JS roadmap (via the browser tool) happens when we
  build the next strategy, not here.

---

## Piece 1 — Remove Moon Dev entirely

### Delete
- `src/tradetk/signals/moondev.py`
- `tests/test_signals_moondev.py`

### Strip Moon Dev references from
- **`src/tradetk/cli/record.py`** — remove `build_moondev_sources`,
  `MOONDEV_SOURCES`, the `--source` / `--no-signals` / `--tier` flags, the
  `MOONDEV_API_KEY` gate, and the `MoonDevProvider` import. **Kalshi books become
  `record`'s only source.** Recording books becomes the default action (no
  `--no-signals` toggle to forget). This dissolves the entire keyless-cloud
  failure mode from the prior session.
- **`src/tradetk/enums.py`** — remove `ProviderName.moondev`; remove the six
  "Moon Dev-only" capabilities: `LIQUIDATIONS`, `HLP_SENTIMENT`,
  `POSITION_SNAPSHOTS`, `SMART_MONEY`, `ORDER_FLOW`, `POLY_WHALES`. Keep the six
  Hyperliquid-native ones (`SPOT_PRICE`, `PERP_PRICE`, `CANDLES`, `ORDERBOOK`,
  `FUNDING`, `REALIZED_VOL`).
- **`src/tradetk/config/schema.py`** — remove `moondev_enabled` and
  `moondev_tier` from `ProviderConfig`.
- **`scripts/validate_provider.py`** — reduce to a Hyperliquid-only reachability
  check. The "cross-validate two providers" premise and the empty `COMPARATORS`
  registry are removed.
- **Config/docs:** `.env.example` (drop `MOONDEV_API_KEY`),
  `config/config.example.yaml` (drop the moondev provider block and its
  capabilities map entries), `README.md`, `CLAUDE.md` (data venue is now
  Hyperliquid-only, read-only), `routines/sweep.md` (record command back to
  `uv run python -m tradetk.cli.record --once --books --pretty`; delete the
  `--no-signals` explanation), `routines/weekly-review.md`, `routines/README.md`,
  `memory/STATE.md`, `memory/GUARDRAILS.md`.

### Also remove `liquidation_skew` (decision — flagged for review)
Ripping out Moon Dev strands `liquidation_skew`: Moon Dev's liquidations feed was
its only possible data source, and Hyperliquid has no native liquidation feed
(verified prior session). A strategy that can never run, advertising a capability
no provider supplies, is exactly the dead/misleading code this project's honesty
rules exist to prevent.

- Delete `src/tradetk/strategy/liquidation_skew.py`
- Delete `src/tradetk/signals/liquidations.py` (pure models; served only this
  strategy)
- Delete `tests/test_liquidation_skew.py`
- Update `src/tradetk/strategy/__init__.py` (drop the import + `__all__` entry)
- Update `tests/test_strategy.py` (drop liquidation_skew assertions)

**Recoverable from git history** if a free liquidations feed ever appears.

> **REVIEW POINT:** if you'd rather keep `liquidation_skew` dormant instead of
> deleting it, that is the one-line alternative — leave the files and the
> `LIQUIDATIONS` capability in place. Default in this spec is delete.

### Kept (do not touch)
- `src/tradetk/strategy/guards.py` — `snapshot_guard` is shared with
  `baseline_vol`. Stays.
- `HyperliquidProvider` and its candles path — now load-bearing for the chart.
- `baseline_vol` — remains the only live strategy.
- The committed shadow log — untouched evidence.

---

## Piece 2 — `chart`: view underlying price vs. contract odds

A new read-only CLI that renders two stacked, time-aligned panels for one Kalshi
contract, so price action and the contract's implied odds can be read together.

### Command
```
uv run python -m tradetk.cli.chart --ticker <KALSHI_TICKER> [--interval 1h]
    [--tape-dir data/tape] [--out <path>] [--symbol <override>]
```
Outputs JSON on stdout: `{ok, out, ticker, symbol, prob_points, candles, span}`.
Writes a PNG (default `data/charts/<ticker>-<UTCstamp>.png`) and prints its path.

### Data flow (both sources already exist)
1. **Bottom panel — contract implied probability, from the tape.**
   `TapeReplay.from_tape(tape_dir)` → filter `.observations()` by `--ticker` →
   for each `BookObservation`, implied prob = yes-mid = midpoint of best
   `yes_bids` / `yes_asks` price. Series is `[(observed_at, prob)]`. If the tape
   has no observations for the ticker, exit non-zero with a clear "record this
   ticker first" message (the tape only holds what `record` captured).
2. **Underlying symbol** comes from `TapeReplay.claim_as_of(ticker, latest, registry)`
   → `claim.underlying` (principled, not a string-hack on the ticker). `--symbol`
   overrides if parsing fails.
3. **Top panel — underlying OHLC.** `HyperliquidProvider().candles(symbol,
   interval, start_ms, end_ms)` over the tape's span (with a small left pad).
   Rendered as candlesticks (or close line if candles are sparse).
4. **Render** both panels on a shared UTC x-axis with matplotlib; annotate the
   contract's strike on the top panel when the claim exposes one. Save PNG.

### How the result is consumed
The PNG is `Read` back by the assistant (images render) for analysis, and pushed
to the user via the side panel. No interactive/GUI backend — matplotlib `Agg`.

### Dependency
Add **matplotlib** (pure-wheel under `uv`; no MSVC toolchain needed on this box).
Use the `Agg` backend explicitly so it never tries to open a window.

### Isolation / structure
- `cli/chart.py` holds argument parsing, IO orchestration, and rendering.
- Pure, network-free helpers (unit-tested):
  - `implied_prob_series(observations, ticker) -> list[tuple[datetime, float]]`
    (best-bid/ask → yes-mid; skips one-sided/empty books rather than guessing).
  - `candles_to_series(candles) -> arrays` for plotting.
  - underlying resolution via the existing claim/registry path.
- Rendering gets a **smoke test**: given a tiny synthetic prob series + candle
  list, it writes a non-empty PNG. No network in any test.

---

## Testing

- Removal: the suite must stay green after symbols are deleted — every test that
  imported a removed symbol is updated or removed in the same change. No skips.
- Addition: pure helpers unit-tested with hand-built inputs; the renderer smoke-
  tested to a temp PNG. Hyperliquid and the tape are never hit in tests.
- Full `uv run pytest` green before any commit.

## Risks / open questions

- **matplotlib** is a non-trivial new dependency. Accepted: it is the pragmatic
  way to produce an image both the assistant and the user can actually see, and
  it installs from wheels here.
- **Sparse tape** — early on, a ticker may have very few book observations, so
  the bottom panel can be nearly flat/degenerate. The command states point counts
  in its JSON so a thin chart is obvious rather than misleading.
- **Underlying/interval mismatch** — if the tape span is short, coarse candle
  intervals give few points; `--interval` is exposed so this is tunable per look.

## Downstream doc updates (part of this change)
`memory/STATE.md` (strategies: `baseline_vol` only; data venue: Hyperliquid-only;
new `chart` capability), `CLAUDE.md` venues section, `routines/sweep.md` command,
`README.md`. The build-step ledger in `STATE.md` notes `liquidation_skew` (step
13) was removed as unrunnable after dropping Moon Dev.
