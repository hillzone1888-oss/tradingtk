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
