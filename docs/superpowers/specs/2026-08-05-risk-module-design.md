# Risk module (build step 14) — design

**Date:** 2026-08-05
**Status:** approved, ready for planning
**Build step:** 14 of 19

## Problem

The book-level risk logic already works, but it has no home. Three portfolio
checks live inline inside `BacktestEngine.run`:

- a slot cap (`len(open_positions) >= max_positions` → `no_free_slot`),
- a per-underlying concentration cap (`same_underlying >= max_slots_per_underlying`
  → `underlying_concentration_limit`),
- a capital cap (`capital_in_use + cost > total_capital` → `insufficient_capital`).

Nothing else can reuse them. The README's execution boundary says `execute`
"re-validates against the live book + **risk state**" — a shared, book-level
thing that does not yet exist. The `risk/` package is scaffolded but empty.

Step 14 gives that logic a real home so the same decision the backtest already
makes can later be re-run, unchanged, by the paper executor (step 15) and the
`propose`/`execute` path (steps 16–17).

## Scope

**In scope — a behaviour-identical extraction.** Move the three existing checks
into a pure `risk/` core that the backtest consumes. Success is defined by the
backtest's own test suite passing unchanged: same trades, same skip counts. The
backtest is the oracle.

**Out of scope — the declared halts.** `RiskConfig` already declares
`max_daily_loss_dollars`, `max_total_drawdown_dollars`, and
`data_staleness_halt_seconds`, none of them wired. They are **not** built here.
They are a named extension point (see "The halt seam"), wired at step 15 in the
live executor where a halt-state that persists across routine wake-ups actually
has meaning. Wiring a drawdown or daily-loss halt into the *backtest* would put
a threshold in the replay path that changes the headline result — the exact knob
CLAUDE.md forbids tuning — so the backtest stays a clean, halt-free oracle.

## Why "extract + share", not a stateful ledger

The chosen shape is a **functional core, caller-owned state**: `risk/` holds no
memory and never mutates. Each consumer keeps its own book — the backtest keeps
its in-memory `open_positions`; the future executor keeps a persisted file — and
both derive a lightweight `RiskState` snapshot to hand the pure decision
functions.

The rejected alternative was a stateful `RiskLedger` that owns the positions and
their transitions. It buys "identical bookkeeping by construction" at the cost of
a second copy of the position book **now**: the backtest already stores rich
`_OpenPosition` objects it needs for settlement (resolution time, PnL, strike),
which are not risk's concern, so the ledger would either absorb backtest-only
fields (scope creep) or run a parallel book the backtest must keep in sync — the
exact class of two-things-out-of-step bug this project has already been bitten
by. The functional core is still fully verified against the backtest oracle
without that hazard, and it defers persistence — the genuinely executor-coupled
part — to a thin shell added when there is an executor to shape it.

## Module shape (`src/tradetk/risk/`)

Three small files, one responsibility each. No I/O, no state, no exceptions in
the normal path.

### `risk/limits.py` — `RiskLimits`

A frozen dataclass carrying the portfolio caps:

- `max_positions: int` — concurrent open slots
- `max_slots_per_underlying: int` — slots any one underlying may hold
- `total_capital: Decimal` — hard ceiling on deployed capital across the book

Constructors:

- direct construction (used by the CLI, which preserves its
  `--max-positions` / `--max-per-underlying` / `--total-capital` override flags),
- `RiskLimits.from_config(config) -> RiskLimits` reading
  `config.capital.{max_positions, max_slots_per_underlying, total_capital}`,
  mirroring the existing `SizingLimits.from_config`.

`total_capital` is **sourced from the same `config.capital.total_capital`** that
`SizingLimits` reads. It is not moved out of `SizingLimits` — the sizer itself
uses it (`sizing.py`: a position is capped against remaining capital). One config
field feeds both dataclasses, so they cannot drift.

The config validators already enforce `max_slots_per_underlying <= max_positions`
and `position_target <= per_position_ceiling <= total_capital`, so `RiskLimits`
trusts validated input rather than re-checking.

### `risk/state.py` — `RiskState`

An immutable snapshot of the open book *from the risk point of view only*:

- `open: tuple[OpenRisk, ...]`, where `OpenRisk` carries `(ticker, underlying,
  capital_at_risk)`.

Helpers:

- `slots_used -> int`
- `slots_for(underlying) -> int`
- `capital_deployed -> Decimal`

`RiskState` knows nothing about settlement, PnL, resolution time, or strikes.
Those stay in whatever the caller uses to run its book. Each consumer builds a
`RiskState` from its own storage: the backtest projects its `open_positions`
dict; a future executor projects its persisted file.

### `risk/gate.py` — the decision

The pure decision surface, mirroring the backtest's two existing checkpoints
exactly:

```python
@dataclass(frozen=True)
class RiskDecision:
    admitted: bool
    reason: str | None   # None when admitted

def screen_new_entry(underlying, state: RiskState, limits: RiskLimits) -> RiskDecision:
    # pre-sizing: "no_free_slot" | "underlying_concentration_limit" | admit

def screen_cost(capital_at_risk, state: RiskState, limits: RiskLimits) -> RiskDecision:
    # post-sizing: "insufficient_capital" | admit
```

Two functions, not one, deliberately. The backtest screens slots and
concentration **before** sizing — so a full book does not burn sizing work, and
the reason is recorded distinctly — and screens capital **after** sizing, because
the cost is not known until the position is sized. Collapsing the two would
change which skip reason surfaces for a candidate that fails more than one check.
The reasons are open strings, not a closed enum, so the halt seam can add reasons
without breaking any consumer.

Boundary operators match the engine today and are pinned by test: `>=` for the
slot and concentration caps, `>` for the capital cap.

## Backtest integration

`BacktestEngine`:

- The constructor replaces the loose `max_positions` / `max_slots_per_underlying`
  kwargs, and its read of `sizing_limits.total_capital` for the cap, with one
  `risk_limits: RiskLimits`.
- At the two existing checkpoints, the engine builds a `RiskState` from its
  `open_positions` — `(ticker, claim.underlying, cost)` per open position — and
  calls `screen_new_entry` / `screen_cost`, mapping `RiskDecision.reason`
  directly onto the **same** `self._skipped[...]` counter names used today
  (`no_free_slot`, `underlying_concentration_limit`, `insufficient_capital`).
- The engine keeps `open_positions`, settlement, PnL, and equity untouched — the
  risk core owns none of that. Building a `RiskState` each iteration is O(open),
  bounded by `max_positions` (≈ 8), and negligible.
- The summary JSON keeps identical `max_positions` / `max_slots_per_underlying`
  keys and values, now read from `risk_limits`.

`cli/backtest.py`:

- Builds a `RiskLimits` from its existing `--max-positions`, `--max-per-underlying`,
  and `--total-capital` arguments. The flags and their behaviour are preserved.
  `from_config` exists for the future propose/execute path that reads a `Config`.

## Composition with the vault overlay

Orthogonal, and already composes — no new interaction to design. The vault
overlay narrows a *single trade* (side and size) inside `_best_assessment`, which
runs *between* the two risk checkpoints. `screen_cost` then runs on the
post-overlay `capital_at_risk`, so an overlay-shrunk position is correctly
cheaper against the book cap. The overlay shrinks the trade; risk counts the
book. Recorded here so it is understood as intentional, not overlooked.

## Error handling

Pure functions with no I/O have no runtime failure surface. Invalid limits are
rejected at config load by existing Pydantic validators. A negative
`capital_at_risk` is a programming error, not a runtime condition, and is guarded
by an assertion rather than a swallow. No fail-open machinery belongs here —
nothing external can fail.

## Testing

- `tests/test_risk_gate.py` — pure-core unit tests with known answers: full book
  → `no_free_slot`; an underlying at its cap → `underlying_concentration_limit`;
  cost greater than remaining capital → `insufficient_capital`; empty book →
  admit; and the boundary operators pinned exactly (`>=` for slots and
  concentration, `>` for capital).
- `RiskLimits.from_config` and `RiskState` construction/helpers tested directly.
- The existing backtest suite is the behaviour-identity oracle and must pass
  unchanged. One focused assertion pins that the engine's skip-counter names are
  preserved after the extraction.

## The halt seam (designed, not built)

Recorded so step 15 inherits a contract rather than a surprise:

- `RiskConfig.max_daily_loss_dollars` (halt new entries for the day) and
  `max_total_drawdown_dollars` (halt permanently until manual reset) are the
  declared halts. They are wired in the live executor at step 15, where the
  halt-state persists across stateless routine wake-ups.
- Shaped to drop in without reshaping: `RiskState` is the carrier — at step 15 it
  gains realized-PnL / day-boundary / halt-flag fields — and `gate.py` gains a
  `screen_halts(state, limits)` returning `daily_loss_halt` / `drawdown_halt`.
  Because `RiskDecision.reason` is an open string, adding these reasons breaks no
  consumer.
- `data_staleness_halt_seconds` is a live-only concern (signal-data age has no
  meaning in a tape replay) and is flagged for the executor.

None of this is built in step 14. It is a named seam, not code.

## Files

- Add: `src/tradetk/risk/limits.py`, `src/tradetk/risk/state.py`,
  `src/tradetk/risk/gate.py` (replace the scaffold `__init__.py` with real
  exports).
- Modify: `src/tradetk/backtest/engine.py`, `src/tradetk/cli/backtest.py`.
- Test: `tests/test_risk_gate.py` (new); existing `tests/test_backtest*.py`
  stand as the identity oracle.

## Success criteria

- The three checks live in `risk/` as pure, independently tested functions.
- The full existing test suite passes unchanged — same trades, same skip counts.
- `ruff check src tests scripts` passes.
- No probability, sizing, settlement, or venue code changes behaviour.
- The halt seam is documented; no halt logic is built.
