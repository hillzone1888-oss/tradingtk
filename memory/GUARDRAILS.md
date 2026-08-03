# Guardrails

**Read this file first, every run, before doing anything else.**

A routine wakes up with no memory of the last one. Everything that keeps this
system disciplined is written down — there is no continuity of judgment, only
continuity of files. If a rule is not in this file or in `CLAUDE.md`, the next
run does not know about it.

---

## The execute boundary — absolute, and it applies to you

**No routine ever places an order.** Not in demo, not "just to test", not
because the edge looks obvious and the market is about to move.

- `shadow`, `calibrate`, `backtest`, `scan`, `inspect`, `record` — always safe.
- `propose` (once step 16 exists) — always safe. It writes a file.
- `execute` — **never**. A human runs it, interactively, having read the
  proposal.

If a run concludes a trade should happen, its output is a **proposal file path**
in a Telegram message. That is the whole of its authority. The point of the
split is that a model can never be the thing that submits an order against real
money, and a model running unattended on a cron is the exact case the split was
built for.

## Capital

- $20 total book. ~$2 positions. 5–8 slots.
- Size in integer contracts, never dollars.
- These are enforced in code. Do not pass flags that widen them.

## What "working" means

- **Calibration, not P&L.** Live P&L on a $20 book is noise and must never be
  cited as evidence that anything works.
- The question is whether the model forecasts better than the venue's own mid on
  the same contracts. If it does not, every trade pays a spread to be more wrong.
- Report sample size beside every number. Flag anything resting on under ~90
  days of tape or a few hundred resolved contracts.

## Changing the system

- **Never tune parameters to improve a backtest or a calibration score you have
  already seen.** That is fitting, and on samples this small it fits perfectly
  and means nothing.
- A routine may **recommend** a parameter change in writing, with its reasoning,
  in `memory/DECISIONS.md`. A human applies it.
- `liquidation_skew`'s `regime` must be declared before a scoring run, never
  chosen after seeing which direction scored better.

## Secrets

- All credentials come from **environment variables**, never from a file in this
  repo, never from `.env` in a cloud run.
- Never log, print, echo, or commit a key — including into a run summary.
- If you find a credential committed in this repo, stop, say so loudly in the
  notification, and do not push anything else until it is rotated.

## Honesty

- If a run failed, say it failed. A digest that omits the failure is worse than
  no digest.
- If the data was thin, say the data was thin.
- Never present a number without saying what it was computed from.
