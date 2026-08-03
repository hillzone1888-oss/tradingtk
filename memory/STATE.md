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

- Shadow log: see `uv run python -m tradetk.cli.shadow --stats`.
- Nothing has resolved in volume yet. Any calibration number below a few hundred
  resolved contracts is a placeholder, not a finding.

## Open questions for the human

- Whether to pay for a liquidations feed, or leave `liquidation_skew` dormant.
- Polymarket US KYC reportedly runs through the iOS app only — unconfirmed.

---

*Last updated: 2026-08-03 — initial seed, by hand.*
