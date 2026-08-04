# State

What the next run needs to know. **Keep this short.** It is read on every wake-up
and every line costs context. Long-form history belongs in `DECISIONS.md`;
per-run detail belongs in the commit message.

Update the fields below at the end of every run. If nothing changed, say so —
"unchanged since <date>" is information, a stale file is not.

---

## FIXED 2026-08-03 — sweep failed in the keyless cloud environment

The first real cloud run of the sweep found two things, both now understood:

1. **`record` refused to start without `MOONDEV_API_KEY`.** The command in
   `routines/sweep.md` omitted `--no-signals`, and `record` exits 2 rather than
   record a subset of what was asked for. That gate is correct; the spec was
   wrong. Fixed — the sweep command now passes `--no-signals`, verified to exit
   0 and capture 25 books with no key set. (Local verification earlier missed
   this because the developer `.env` *has* the key.)
2. **`git push` returned 403** — the per-routine "allow unrestricted branch
   pushes" permission is not enabled. Web-UI setting, not fixable from code.
   Until it is on, no sweep can persist anything.

The routine behaved correctly throughout: it refused to score against an absent
tape, only *recommended* fixes rather than applying them, and never approached
the execute boundary.

## Build position

- Build step **13 of 19** complete. Remaining: 14 risk, 15 paper executor,
  16 `propose`, 17 `execute`, 18 Polymarket US, 19 production flags.
- **`propose` does not exist yet.** Until step 16 lands, no routine can produce a
  proposal file, and the honest output of a sweep is scored forecasts only.

## Venue and environment

- Kalshi, **demo** environment. Market data is read from **prod** (demo has no
  strike fields and no book depth); execution targets demo. Neither path has an
  order endpoint wired.
- Hyperliquid + Moon Dev are read-only signal sources. Nothing is ever sent to
  them.

## Strategies

- `baseline_vol` — the benchmark. Runnable.
- `liquidation_skew` — **not runnable.** It declares `Capability.LIQUIDATIONS`,
  which no provider advertises, so selecting it halts at startup by design.
  Hyperliquid has no usable native liquidation feed (verified 2026-08-03: no
  info type, no WS channel, no flag on the public trades feed; the HLP liquidator
  vaults see only backstop liquidations — 0 in 7 days). A paid third-party feed
  is the only live route. Do not re-investigate this.

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

## Open questions for the human

- Whether to pay for a liquidations feed, or leave `liquidation_skew` dormant.
- Polymarket US KYC reportedly runs through the iOS app only — unconfirmed.

---

*Last updated: 2026-08-03 — initial seed, by hand.*
