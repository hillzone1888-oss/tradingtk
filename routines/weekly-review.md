# Routine: weekly-review

**Schedule:** `0 22 * * 0` (Sundays, UTC — 17:00 US Central)
**Job:** the slow question. Not "what happened this week" but "is this system
earning the right to keep running, and what should change."
**Notifies:** always.

---

## Prompt

You are running the `weekly-review` routine for the trading toolkit. You wake up
with no memory of previous runs.

**Step 1 — orient.** Read `memory/GUARDRAILS.md`, `memory/STATE.md`, and the last
few entries of `memory/DECISIONS.md` so you do not re-recommend something already
rejected.

**Step 2 — the full picture.** Read-only:

```
uv run python -m tradetk.cli.shadow --stats --pretty
uv run python -m tradetk.cli.calibrate --json --pretty
uv run python -m tradetk.cli.calibrate --html memory/reports/calibration-<YYYY-MM-DD>.html
```

Then `git log --oneline --since="7 days ago"`.

**Step 3 — read the decomposition, not just the score.** Brier is decomposed into
reliability / resolution / uncertainty because the fixes are opposite:
- poor **reliability** (miscalibration) can be remapped — the model has signal
  and reports it at the wrong confidence;
- poor **resolution** cannot — the model is not distinguishing outcomes at all,
  and no post-processing invents signal that was never there.

Say which one the week's data points at. If the sample cannot support that
distinction, say that instead.

**Step 4 — compare, honestly.**
- Strategy versus **the venue's own mid** on the same contracts. This is the
  criterion. Losing to the mid means every trade would pay a spread to be more
  wrong.
- Where `liquidation_skew` is concerned: it is not runnable (no liquidations
  provider). Do not report a score for it. Do not "estimate" one.
- Never pool measured-reference markets with fixed strikes; they are ~50/50 by
  construction.

**Step 5 — write the review** to `memory/reports/weekly-<YYYY-MM-DD>.md`:
what accumulated, the calibration picture with sample sizes, what broke, and an
explicit **"what I would change and why"** section.

**Step 6 — recommend, never apply.** Append any proposed change to
`memory/DECISIONS.md` marked `[recommended]`, with what, why, the evidence it
rests on, and the risk if wrong. Then stop.

You may **not** apply a parameter change yourself. Tuning a parameter to improve
a calibration score you have already looked at is fitting, and on these sample
sizes it fits perfectly and means nothing. This is the rule the whole project is
built to survive; a routine that broke it once, quietly, on a Sunday, would
invalidate every number that came after.

Also grade the **week's operations**, not the returns: did every sweep run, were
there tape gaps, did anything silently fail. That is the part you can actually
act on.

**Step 7 — commit and push.**

```
git add -A && git commit -m "weekly review <YYYY-MM-DD>: <headline>" && git push origin main
```

**Step 8 — send a short summary** (not the whole file):

```
uv run python -m tradetk.cli.notify --text "..." --prefix "🗓️ tradetk weekly"
```

Include: forecasts and resolved contracts this week, model vs mid, operational
faults, and the count of open `[recommended]` decisions awaiting a human. End
with the path to the full review in the repo.
