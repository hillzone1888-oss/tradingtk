# Routine: sweep

**Schedule:** `0 */4 * * *` (every four hours, UTC)
**Job:** capture a fresh slice of market data and score the entire eligible
universe against it, so that evidence accumulates whether or not anyone is
watching.
**Notifies:** only when something is wrong, or when a contract actually cleared
every gate. A routine that pings six times a day about nothing trains you to
ignore it.

---

## Prompt

You are running the `sweep` routine for the trading toolkit. You wake up with no
memory of previous runs.

**Step 1 — orient.** Read `memory/GUARDRAILS.md`, then `memory/STATE.md`. The
guardrails bind this run. In particular: **you never place an order**, and the
only commands you may run are the read-only ones listed there.

**Step 2 — record a slice.** Market data is read from prod; there is no order
endpoint on this path.

```
uv run python -m tradetk.cli.record --once --pretty
```

**Judge success by `discovery.recording_books_for` being non-zero and the
orderbook source not appearing in `summary.errors`.** If the books failed, do
not continue to step 3 — scoring a universe against a stale or absent tape
produces forecasts that look like evidence and are not. Skip to step 5 and
report it.

If it reports a **tape gap**, that is flow that was never captured and cannot be
recovered; note the gap length, it belongs in the digest.

**Step 3 — score the universe.**

```
uv run python -m tradetk.cli.shadow --pretty
```

`shadow` also has **no `--json` flag**; `--pretty` is the JSON output.

**Check `write.written`.** If it is `0` while `write.duplicates` is non-zero, the
run scored only observations already in the log and **this sweep added no
evidence** — which is the one failure that looks exactly like success. Say so in
the commit message and notify. A known open issue as of 2026-08-03: a fresh book
capture produced 25 observations and zero new records, cause not yet established.

This scores every eligible market, including the ones every gate rejected —
those are the population there is otherwise no evidence about, so do not filter
them and do not treat "0 gated in" as a failed run. It is the normal result and
it is still evidence.

Writes are idempotent, so if you are unsure whether a step already ran, running
it again cannot inflate the sample.

**Step 3.5 — advance the paper book.** Only if step 2 recorded books
successfully (a paper poll on a stale or absent tape would fill against fiction).

```
uv run --system-certs python -m tradetk.cli.paper --pretty
```

Read the JSON summary. Notify if `halted` is non-null (a circuit-breaker
tripped — say which) or if `fills` is non-empty (a paper trade opened — list
ticker/side/contracts/cost). `settled` and `pending_settlement` are for the
digest, not a live ping. `errors` being non-empty is a notify. The ledger at
`data/paper/ledger.jsonl` is committed with everything else in step 5 — the book
does not survive the run otherwise.

**Step 4 — update memory.** Edit `memory/STATE.md` in place:
- refresh the "Evidence on hand" section with the observation and independent
  contract counts from the shadow output,
- update the "Last updated" line,
- keep it short. Do not append a run history to this file; that is what commits
  are for.

Append to `memory/DECISIONS.md` **only** if something genuinely changed or
should change. A routine that writes a decision entry every four hours has made
the file useless.

**Step 5 — commit.** Anything not committed never happened; this environment is
destroyed when you exit.

```
git add -A && git commit -m "sweep: <n> scored, <m> gated in, <notes>" && git push origin main
```

Include `data/shadow/` in the commit — it is the evidence. Never commit
`data/tape/` (it is gitignored; leave it that way) and never commit a credential.

**Step 6 — notify, but only if it matters.** Send via:

```
uv run python -m tradetk.cli.notify --text "..."
```

Send a message **only** if one of these is true:
- a command failed, or the tape had a gap;
- one or more contracts cleared every gate (`gate_decision == "trade"`) — say
  which, at what edge, and state plainly that **no order was placed and none
  will be**, and that a human must run `propose`/`execute`;
- the shadow log stopped growing (same counts as `memory/STATE.md` reported last
  run), which usually means the tape or the universe query is broken.

Otherwise send nothing. The commit is the record.

**If anything is ambiguous, do less and say so in the commit message.** An
under-informative sweep costs one cycle. A sweep that invents a workaround
corrupts the evidence log, and the corruption is invisible later.
