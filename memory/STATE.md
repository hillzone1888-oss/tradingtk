# State

What the next run needs to know. **Keep this short.** It is read on every wake-up
and every line costs context. Long-form history belongs in `DECISIONS.md`;
per-run detail belongs in the commit message.

Update the fields below at the end of every run. If nothing changed, say so —
"unchanged since <date>" is information, a stale file is not.

---

## Build position

- Build step **13 of 19** complete. Remaining: 14 risk, 15 paper executor,
  16 `propose`, 17 `execute`, 18 Polymarket US, 19 production flags.
- **`propose` does not exist yet.** Until step 16 lands, no routine can produce a
  proposal file, and the honest output of a sweep is scored forecasts only.

## Venue and environment

- Kalshi, **demo** environment. Market data is read from **prod** (demo has no
  strike fields and no book depth); execution targets demo. Neither path has an
  order endpoint wired.
- Hyperliquid is the only read-only signal source. Nothing is ever sent to it.

## Strategies

- `baseline_vol` — the only strategy. Runnable.
- A second strategy layering a forced-liquidation skew onto the baseline was
  built at build step 13, then **removed 2026-08-04** as permanently
  unrunnable after dropping the paywalled third-party liquidations feed it
  depended on: no provider ever advertised the liquidations capability it
  needed. Hyperliquid has no usable native liquidation feed (verified
  2026-08-03: no info type, no WS channel, no flag on the public trades feed;
  the HLP liquidator vaults see only backstop liquidations — 0 in 7 days). Do
  not re-investigate this.

## Evidence on hand

- Shadow log: 161 records, 52 distinct contracts, `baseline_vol` only, spanning
  the 2026-07-22 tape and 2026-08-03. See `uv run python -m tradetk.cli.shadow --stats`.
- Nothing has resolved in volume yet. Any calibration number below a few hundred
  resolved contracts is a placeholder, not a finding.

## FIXED 2026-08-03 — the sweep was writing zero records

`record` polled **books before metadata**. The shadow evaluator resolves a
claim strictly as-of the book's timestamp, so metadata written 4 seconds later
was invisible, and every book in a poll was skipped as `no_parseable_claim`.

A daemon run hid it — poll N resolved against poll N-1's metadata, so only the
first poll was lost. A `--once` run, which is exactly what the scheduled sweep
does, lost **everything**: 25 books captured, 25 skipped, `written: 0`, exit 0.
A silent, total loss of evidence that looked like a clean run.

Fix: metadata is polled first, so the terms genuinely predate the book and the
as-of guard is satisfied honestly rather than relaxed. Pinned by
`tests/test_cli_record.py::test_metadata_is_recorded_before_books` — **if that
test ever fails, scheduled sweeps are accumulating nothing.**

Verified after the fix: `written: 50`, observations scored 111 → 161, distinct
contracts 25 → 52. The residual `no_parseable_claim: 50` is historical (July's
first poll plus today's pre-fix poll) and is not recoverable.

## Vault overlay

- Landed 2026-08-04. Off by default (`vault_overlay.enabled: false` in
  `config.yaml`); every existing output is byte-identical with it off.
  Approved Second-Brain stances/catalysts, via `vault-post`, can only narrow
  what the pipeline proposes — restrict the side, shrink the size, or demand
  more edge — never permit a trade the pipeline would otherwise refuse.
  `shadow` and `backtest` both wire it; `shadow` records the verdict but never
  filters on it. `record` now captures a vault snapshot each poll when
  enabled, so backtests can ask "what did my stances say then" instead of
  reading views written after the fact. A snapshot failure is logged and
  swallowed — the market tape must never be lost to a dead vault.

## Open questions for the human

- Polymarket US KYC reportedly runs through the iOS app only — unconfirmed.

---

*Last updated: 2026-08-05.*
