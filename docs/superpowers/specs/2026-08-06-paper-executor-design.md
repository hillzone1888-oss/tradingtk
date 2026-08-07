# Spec — Step 15: the paper executor

*Design doc. Date: 2026-08-06. Build step 15 of 19.*

## What this is

The first **live-forward** loop in the toolkit. Everything before it either
replays the project's own recorded tape (`backtest`) or scores the whole
universe without holding a position (`shadow`). The paper executor runs the
*exact live decision path* forward in real time — pipeline, risk gate, **halts**,
staleness, persistence, settlement — but fills every trade in **simulation**. No
order endpoint is contacted, from anywhere in this module's import graph.

It exists to do two jobs, in this order of importance:

1. **De-risk the live loop before real money.** It is the dress rehearsal for
   step 17 (`execute`). It proves the forward loop runs, that the risk **halts**
   fire when they should, that a stale feed is caught, that a persistent book
   survives across polls and settles correctly. These are *mechanics*, and they
   are the point.
2. **Accrue a live-but-fake track record** — so that when real execution lands,
   it is not the first time the whole path has run end to end.

### What success is — and is not

Paper **P&L is not evidence and must never be cited as such.** `CLAUDE.md` is
blunt: live P&L on a $20 book is noise; whether the model *works* is answered
from calibration and shadow, never the balance. The paper executor inherits that
rule wholesale. Its job is to exercise the loop, not to produce a number anyone
trusts.

This matters for every future step too, so it is written here once: on a $20
book the practice that actually compounds into profit is **discipline** — honest
evidence, no overfitting, capital preservation, a rehearsed loop before real
money — not cleverness tuned against a backtest you have already seen. Any future
step that reaches for a flattering number instead of an honest one is optimizing
the wrong quantity. The design below is built to make the disciplined path the
only path.

## Non-goals (deliberately not built here)

- **No high-fidelity fill model.** A paper fill walks the *live* book at decision
  time and assumes it fills — optimistic versus a real resting maker order, which
  may fill late or not at all. That is accepted and documented; real fill
  dynamics (queue position, adverse selection) are a **step 17** concern, where
  queue and real money exist. The fill records its **assumed price** precisely so
  step 17 can measure real-vs-assumed slippage against it.
- **No halts in the backtest.** The halt functions live in `risk/` and are pure,
  so the backtest *can* adopt them later — but as a deliberate, oracle-guarded
  change with its own before/after, because halts are a capital-preservation
  overlay whose cost/benefit must be *measured*, not baked in silently. Step 15
  wires halts into the paper executor only.
- **No engine refactor.** The paper orchestrator calls the *already-shared*
  pieces (risk gate, translation, sizing, fees, book-walk) itself. It does not
  refactor the oracle-guarded `BacktestEngine` to extract a common loop. Only the
  short loop *wiring* is duplicated; the decision-*determining* logic is shared,
  so paper and backtest cannot silently disagree on a decision. If the
  duplication ever bites, a later step extracts the shared loop on purpose.
- **No unrealized mark-to-market.** Drawdown is measured on *realized* equity
  (see below). Paper positions are ~$2 and total exposure is already capped at
  `total_capital` by the capital screen, so realized-only drawdown is sufficient
  here; unrealized MTM drawdown is a step-17 refinement.
- **No chart/pattern/TA signal logic.** That is *entry* logic and belongs to the
  strategy layer, earned through calibration — a separate future strategy step,
  not the halt seam and not this step.

## The per-poll lifecycle

The paper executor runs one poll per invocation, inside the existing `sweep`
routine, **after** `record` and `shadow` and **before** the commit. The order of
the five phases is load-bearing.

1. **Load.** Fold `data/paper/ledger.jsonl` into the current state: the open book
   (as a `RiskState`), `realized_today`, the drawdown high-water-mark, and the
   drawdown latch. The ledger is the single source of truth; the book is a
   projection, never a separately-mutated file.

2. **Settle first.** For each open paper position, read `venue.market(ticker)`
   (read-only). If it is settled and carries a `result`, compute realized P&L
   with the *same* contract-payout math the backtest uses, append a `settle`
   event, and drop it from the book. Settlements run **even when the book is
   halted** — a halt stops new *risk*; it never freezes an existing position from
   resolving. A position past its resolution time for which the venue still shows
   no result is reported `pending_settlement` and **never force-settled**.

3. **Halt gate.** Build a `BookHealth` from post-settlement state
   (`realized_today` including anything just settled, drawdown, and `data_age` —
   the age of the freshest **underlying signal** feeding this poll's estimates,
   i.e. the newest Hyperliquid candle close, *not* the Kalshi book, which is
   fetched live and fresh by construction) and call `screen_halts` **once**. If it refuses, append one `halt` event recording the reason and its
   inputs, and **open nothing** this poll. Settlement already happened in phase 2,
   which is correct: a loss that just settled can trip the daily-loss halt in the
   same poll.

4. **Evaluate entries.** Pull live eligible markets and books (the same read path
   `record`/`shadow` use). Skip any candidate whose ticker is already an open
   position (reason `already_open`). For the rest, run the already-shared pieces
   **in the backtest's exact order**: `strategy.estimate` → edge gate → sizing →
   `screen_new_entry` → `screen_cost`, judged against the freshly-folded
   `RiskState`. For each admitted candidate, walk the **live** book with
   `walk_to_buy_yes`/`walk_to_buy_no` to get the assumed fill price and
   contracts, and append a `fill` event.

5. **Commit.** The `sweep` routine commits and pushes the ledger. Anything not
   committed never happened — a cloud run works in a clone destroyed on exit.

## Data model — the ledger

`data/paper/ledger.jsonl`, **committed** (like `data/shadow/`, and for the same
reason: ephemeral cloud runs would otherwise lose the only copy of the book).
One JSON object per line, append-only, never mutated. Line-oriented so git diffs
are clean and history is auditable — the same discipline as the append-only
`DECISIONS.md`.

Event types and their idempotency keys:

- **`fill`** — `{type, ts, ticker, underlying, side, contracts, assumed_price,
  fee, cost, resolution_time}`. `cost` is capital-at-risk (`contracts *
  assumed_price + fee`). Idempotency key `(ticker, "fill", ts)`; combined with
  the `already_open` skip, a ticker cannot be double-entered.
- **`settle`** — `{type, ts, ticker, result, side, contracts, proceeds,
  realized_pnl, resolution_time}`. `proceeds` = `contracts` if the held side won
  else `0`; `realized_pnl` = `proceeds - cost`. Settlement itself is free (Kalshi
  charges on trades, not resolution), so no fee here. Idempotency key `(ticker,
  "settle")` — a ticker settles exactly once.
- **`halt`** — `{type, ts, reason, realized_today, drawdown, data_age_seconds}`.
  Audit only; does not change the book. Idempotency key `(ts, "halt")`.
- **`reset`** — `{type, ts, note}`. Clears the drawdown latch. Appended by a human
  (or a small `paper reset-drawdown` subcommand). Idempotency key `(ts, "reset")`.

### Projection semantics (the fold)

`starting_capital` = `config.capital.total_capital` ($20).

- **Open book**: a ticker with a `fill` and no later `settle` is open; its
  capital-at-risk is its fill `cost`. Assembled into a `RiskState` of `OpenRisk`.
- **`realized_today`**: sum of `realized_pnl` over `settle` events whose `ts`
  falls in the current UTC day. (UTC to match `shadow`'s `date=` partitioning.)
- **Equity & drawdown** (realized-only): `equity(t)` = `starting_capital +`
  cumulative `realized_pnl` up to `t`. `peak_equity` = max `equity` ever reached;
  `current_equity` = `equity(now)`; `drawdown` = `peak_equity - current_equity`
  (≥ 0).
- **`drawdown_latched`**: fold events in order holding a boolean — set `True` when
  `drawdown ≥ max_total_drawdown_dollars`, set `False` on a `reset` event. The
  final value is the latch state.

The fold is O(events); on a $20 book with a few short-dated slots that is a
handful of events per day. Trivial.

## The halt seam — now wired

Step 14 left `risk/gate.py` using open-string reasons precisely so this step
could add halts "without breaking any consumer." The halts are a **pure parallel**
to step 14's `RiskLimits`/`RiskState`, kept in `risk/` so the module stays a leaf
(no imports from backtest, cli, overlay, venues, or translation).

New in `src/tradetk/risk/halts.py`:

```python
@dataclass(frozen=True)
class HaltLimits:
    max_daily_loss_dollars: Decimal
    max_total_drawdown_dollars: Decimal
    data_staleness_halt_seconds: Decimal

    @classmethod
    def from_config(cls, config) -> "HaltLimits": ...   # reads config.risk.*

@dataclass(frozen=True)
class BookHealth:
    realized_today: Decimal      # negative when losing
    drawdown: Decimal            # >= 0
    data_age_seconds: Decimal    # now - freshest underlying signal (candle) ts
    drawdown_latched: bool

def screen_halts(health: BookHealth, limits: HaltLimits) -> RiskDecision:
    # severity order; a recorded-reason choice, trivially changed
    if health.drawdown_latched or health.drawdown >= limits.max_total_drawdown_dollars:
        return RiskDecision(False, "drawdown_halt")
    if -health.realized_today >= limits.max_daily_loss_dollars:
        return RiskDecision(False, "daily_loss_halt")
    if health.data_age_seconds > limits.data_staleness_halt_seconds:
        return RiskDecision(False, "stale_data_halt")
    return RiskDecision(admitted=True, reason=None)
```

`screen_halts` reuses step 14's `RiskDecision`. Halt lifetimes:

| Halt | Trips when | Clears |
|------|-----------|--------|
| `daily_loss_halt` | `-realized_today ≥ max_daily_loss_dollars` | auto, next UTC day |
| `drawdown_halt` | `drawdown ≥ max_total_drawdown_dollars` | **manual `reset` only** (latched) |
| `stale_data_halt` | `data_age_seconds > data_staleness_halt_seconds` | auto, when fresh data arrives |

## Components

- **`src/tradetk/state/ledger.py`** — read/append the JSONL log and the pure
  projection functions (open book → `RiskState`, `realized_today`, drawdown +
  latch). Pure fold split cleanly from file I/O.
- **`src/tradetk/risk/halts.py`** — `HaltLimits`, `BookHealth`, `screen_halts`
  (above).
- **`src/tradetk/cli/paper.py`** — the orchestrator running the five-phase
  lifecycle. Reuses translation, the risk gate, fees, and the book-walk; copies
  no decision logic from the engine. Read-only venue and provider access only.
- **`VenueMarket.result`** (+ one mapping line in `parse_market`) — the settled
  outcome, read-only. The only change outside the new files; touches no order
  path.
- **`routines/sweep.md`** — a `paper` phase inserted between `shadow` and the
  commit, sharing the fresh data the sweep already pulls every four hours.
- **Notify** — reuse `src/tradetk/notify`: notify on a filled paper entry (a real
  event) and on a halt (something is wrong), consistent with sweep's "only when it
  matters" rule. No new notify machinery.

## Error handling

- A provider or book-fetch failure on a single candidate skips **that candidate**,
  never the poll — the same posture as the engine's per-symbol guard.
- A stale feed is the `stale_data_halt` **designed path**, not an error.
- A settle-read failure leaves the position open and reports `pending_settlement`;
  it never force-settles or drops a position.
- Re-running a poll is safe: the fold is idempotent, and `already_open` plus the
  per-ticker settle key mean a retry converges to the same book.

## Testing

The backtest suite remains the behavioural oracle for the *decision* path; these
tests cover the *new* surface:

- **`screen_halts` known-answer tests** — each reason, each boundary
  (`≥` vs `>`), the latch set-and-hold, and `reset` clearing it — mirroring the
  step-14 risk-gate tests.
- **Ledger projection tests** — fill/settle fold to the right open book and
  realized P&L; drawdown high-water-mark and latch; an **idempotency test**
  (folding twice, or re-appending a keyed event, yields the same state).
- **Settlement test** — open → venue shows `result` → correct `realized_pnl`
  lands in the ledger; a resolved-by-time position with no venue result stays
  `pending_settlement`.
- **End-to-end poll test** — a fake venue + provider drive one full five-phase
  lifecycle, including a poll that halts (opens nothing) and a poll that settles
  then fills.

## Invariants to hold at review

1. No order endpoint anywhere in `cli/paper.py`'s import graph; venue and
   provider access is read-only.
2. `risk/` stays a leaf — `halts.py` imports only stdlib and within-package.
3. Paper's per-candidate decision sequence is the backtest's exact order, calling
   the shared pieces (no copied decision logic).
4. The ledger is append-only and the book is only ever a projection of it.
5. Settlement runs regardless of halt state; entries do not.
6. `VenueMarket.result` is the only change outside new files, and it touches no
   order path.
7. Paper P&L is never presented as evidence; calibration + shadow remain the
   success criteria.
