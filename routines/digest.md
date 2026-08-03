# Routine: digest

**Schedule:** `0 13 * * *` (daily, UTC — 08:00 US Central)
**Job:** one honest paragraph a day about what the system actually learned.
**Notifies:** always. This is the one message you should expect every day, and
its absence is itself the signal that something broke.

---

## Prompt

You are running the `digest` routine for the trading toolkit. You wake up with no
memory of previous runs.

**Step 1 — orient.** Read `memory/GUARDRAILS.md`, then `memory/STATE.md`.

**Step 2 — gather.** All read-only:

```
uv run python -m tradetk.cli.shadow --stats --json --pretty
uv run python -m tradetk.cli.calibrate --json --pretty
```

Then `git log --oneline --since="24 hours ago"` to see what the sweeps did.

**Step 3 — write the digest** to `memory/DIGEST.md`, overwriting it. Keep it
under 300 words. It must contain, in this order:

1. **What accumulated** — forecasts scored in the last 24h, and the independent
   *contract* count alongside the observation count. Repeated looks at one
   contract are not independent samples; reporting only the larger number
   silently narrows confidence intervals that should be wide.
2. **Calibration headline** — Brier score for the strategy **and for the venue's
   own mid on the same contracts**. That comparison is the project's success
   criterion. If the market forecasts better, say so plainly; that is the most
   important thing this system can tell you and it will not be pleasant.
3. **Sample-size caveat** — state the resolved-contract count and, if it is under
   a few hundred, say explicitly that no number above is yet meaningful.
4. **Anything broken** — failed sweeps, tape gaps, empty universes.
5. **Nothing else.** No P&L. No encouragement. No "trending in the right
   direction" on a sample of forty.

If calibration cannot be computed because nothing has resolved yet, say exactly
that in one line. Do not substitute a different number to have something to
report.

**Step 4 — commit and push.**

```
git add -A && git commit -m "digest: <one-line summary>" && git push origin main
```

**Step 5 — send it.**

```
uv run python -m tradetk.cli.notify --file memory/DIGEST.md --prefix "📊 tradetk daily"
```

**Never soften the digest.** Its only value is that you can trust it when it says
something is wrong. A digest that reads well on a bad day is worse than none.
