# Decisions

Append-only. Newest at the top. One entry per thing that changed, or per thing a
run **recommends** changing and a human has not yet acted on.

A routine may write here freely. A routine may not act on a recommendation it
wrote itself — the whole value of this file is that a change is visible as a
decision with a date and a reason attached, rather than a parameter that quietly
became different.

Format:

```
## YYYY-MM-DD — <short title>   [applied | recommended | rejected]
**What:** the change, precisely enough to reverse it.
**Why:** the reasoning, and what evidence it rests on (with sample size).
**Risk if wrong:** what this breaks or biases.
```

---

## 2026-08-03 — Alpaca paper account retained as a future second venue   [applied]

**What:** an Alpaca **paper** account is kept and its credentials belong in the
cloud environment (`ALPACA_API_KEY_ID`, `ALPACA_API_SECRET`,
`ALPACA_BASE_URL=https://paper-api.alpaca.markets/v2`). No code reads them yet
and no routine uses them.

**Why:** the operator wants a paper-money venue on hand for strategy work.
Decision reaffirmed after the constraint below was raised.

**The constraint, recorded so nobody rediscovers it the hard way:** Alpaca
**cannot** paper-trade what this toolkit trades. tradetk prices Kalshi binary
event contracts ("BTC above $100k at 3pm ET", resolving YES/NO). Alpaca trades
equities and crypto spot. There is no Alpaca instrument corresponding to a Kalshi
contract, so an Alpaca account cannot simulate any current tradetk strategy —
not approximately, not as a proxy. The venue-correct paper paths are Kalshi's
**demo** environment (already the default) and build **step 15**, the paper
executor that fills against real recorded books using the live cost code.

**Where Alpaca does earn its place:** as the broker for a *separate* equities
agent — its own claim type, its own strategy, its own build steps. That is real
work and is not scoped yet.

**Risk if wrong:** none to the current system, since nothing reads these
credentials. The risk is only of confusion later — someone seeing Alpaca keys in
the environment and assuming tradetk results were paper-traded through them.
This entry exists to prevent exactly that.

**Outstanding:** only the key **ID** has been supplied; the matching secret is
needed before anything can connect.

## 2026-08-03 — routine harness adopted   [applied]

**What:** the toolkit is now driven by scheduled Claude Code routines
(`routines/`), with `memory/` as the state that survives between them. Nothing
about the trading logic changed; the gates, sizing and cost model are the same
deterministic code they were.

**Why:** evidence accumulates at the rate the universe moves, not the rate a
human remembers to run a command. A scheduled sweep turns "I ran shadow twice
last week" into a continuous record.

**Risk if wrong:** an unattended agent is a new failure surface. It is mitigated
by the execute boundary (no routine can place an order) and by the guardrails
file being read on every wake-up — not by trusting the agent to remember.
