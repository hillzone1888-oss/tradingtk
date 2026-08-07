# Spec — Step 16: the `propose` command

*Design doc. Date: 2026-08-06. Build step 16 of 19.*

## What this is

The read-only half of the two-command execution boundary the README has promised
since step 1:

> `propose` — scans, estimates, gates, and writes `proposals/<ts>.json` with the
> full trace. **Never contacts the venue order endpoint.** Safe to run constantly.
> `execute --proposal <file>` — the **only** path that submits an order.

`propose` runs the full live pipeline — fresh signals, live books, strategy,
vault overlay, edge gate, sizing, risk gate, halts — and writes **one JSON file
per admitted trade** containing the complete decision trace. A human reads the
file and, at step 17, hands it to `execute`, which re-validates and asks for a
typed confirmation. One file = one order = one confirmation: the handshake is
deliberately too simple to get wrong.

The assistant's standing rule stays as written in `CLAUDE.md`: asked to place a
trade, it produces a proposal and hands over the file path. `propose` is what it
runs to do that.

## Decisions locked during brainstorming

- **One file per trade** (`proposals/<utc-ts>-<ticker>.json`), not a batch file
  and not best-only. A run summary on stdout lists what was written and every
  skip reason.
- **Risk state comes from the live-ledger seam**: `propose` projects
  `data/live/ledger.jsonl` — the same event-sourced format and the same
  `state/ledger.py` code the paper book uses, at a different path. The file does
  not exist yet, so today the projection is an empty book (all slots free, no
  halt latched). Step 17's `execute` appends real `fill`/`settle` events there,
  and `propose`'s risk math becomes right about real capital with zero changes.
  The paper ledger is never consulted: a simulated position must not eat a real
  slot.
- **The overlay is ON.** The vault overlay exists to shape what the toolkit
  proposes; `propose` is the command it was built for. Blocked underlyings
  assess nothing, biases forbid sides, risk dials shrink sizing, catalysts raise
  the gate — exactly the narrowing the backtest already composes.

## The extraction: one shared assessment loop

`propose` is the **third** consumer of the per-side assessment loop
(size each side → gate each side → keep the better passing side), after
`BacktestEngine._best_assessment` (overlay on) and paper's `choose_side`
(overlay off, duplicated at step 15 with a cross-check test because two
consumers didn't justify refactoring the oracle-guarded engine). Three consumers
is the threshold: this step extracts the loop once, as a deliberate,
oracle-guarded refactor.

- New: `translation/assessment.py` with a single pure function (sketch — the
  plan pins exact signatures):

  ```python
  def assess_candidate(claim, estimate, book, when, capital_in_use, *,
                       gate_limits, sizing_limits, fee_model,
                       overlay=None) -> CandidateOutcome
  ```

  `CandidateOutcome` carries the chosen `EdgeAssessment | None`, the binding
  cap, and the **ordered tuple of skip-reason strings** the loop produced
  (`overlay_blocked`, `overlay_side_forbidden`, `unsizeable_*`, `gate_*`).
  Returning reasons instead of incrementing a counter keeps the function pure;
  the engine folds them into `self._skipped` in order, which preserves its
  counters exactly.
- The engine's `_best_assessment` becomes a thin wrapper: call the shared
  function with `overlay=self.overlay`, fold the skips. **The backtest suite is
  the oracle**: same trades, same skip counts, zero test edits.
- Paper's `choose_side` becomes a call with `overlay=None`. The step-15
  cross-check test (`choose_side` ≡ engine) now guards the extraction seam
  instead of a duplicate, and the two parked "choose_side duplication" minors
  from step 15 dissolve.
- `translation/` already imports nothing from `backtest/` or `cli/` — the
  extraction keeps it that way (leaf-ward move, no new cycles).

## The run, in phases

`src/tradetk/cli/propose.py`, mirroring paper's shape:

1. **Load state.** Project the live ledger (missing file ⇒ empty book) →
   `RiskState`, `realized_today`, drawdown, latch.
2. **Scan live.** Eligible markets and books via the same read-only prod path
   `record` uses (`venues/books.py`); claims from the step-6 parser; fresh
   Hyperliquid candles covering the vol lookback. Read-only everywhere, by
   construction: nothing in the import graph can place an order. (The scan
   precedes the halt gate because staleness is measured on these candles —
   the same order paper uses, where data is fetched before the phases run.)
3. **Halt gate, once.** Build `BookHealth` (staleness from phase 2's candle
   freshness) and call `screen_halts`. Tripped ⇒ **write no proposals**, emit
   the summary with `halted` and which breaker, exit 0 — a halt is a designed
   outcome, not an error. There is no settle phase: `propose` holds no
   positions to settle; the live ledger is `execute`'s to write.
4. **Evaluate.** Per candidate, in the pipeline's canonical order:
   `strategy.estimate` → shared `assess_candidate` (overlay loaded from config
   exactly as the backtest CLI loads it; an unavailable overlay warns loudly and
   proceeds unmodified, same posture as backtest/paper) → `screen_new_entry` →
   `screen_cost`. Candidates are ranked by net edge and capped at free slots;
   each admitted candidate updates the working `RiskState`/capital so later
   candidates see it. Per-candidate failures skip that candidate, never the run
   (paper's posture, kept).
5. **Write.** One file per admitted trade plus a stdout JSON summary: files
   written, every skip reason with counts, the halt check, and the overlay
   status. The "why not" trace is as much the product as the "why" — a run that
   proposes nothing must say exactly which gates ate the universe.

## The proposal file

`proposals/<utc-ts>-<ticker>.json`, `schema_version: 1`. Contents:

- **Identity**: schema_version, created_at (UTC), toolkit git describe (best
  effort), strategy name + parameters.
- **The claim**: ticker, underlying, resolution_time, strike fields — the typed
  claim, serialized.
- **The decision**: side, contracts, limit-implied price, cost and fee
  breakdown (all money as strings, Decimal end to end), net edge pp, gross
  edge pp, binding cap, probability estimate with its inputs (vol, hours to
  resolution).
- **The evidence at decision time**: top-of-book (best bid/ask/depth both
  sides), book retrieved_at, candle freshness. What `execute` re-validates
  against.
- **The context**: risk-state snapshot (slots used, capital deployed), halt
  check result, overlay verdict for this underlying (or "no overlay"),
  **config fingerprint** — a sha256 over the canonical JSON of the config
  sections that shaped the decision (capital, edge_gate, liquidity, horizon,
  risk, orders, venue, fees, strategy), so step 17's `execute` can refuse a
  proposal minted under a different configuration.

What the file deliberately does **not** contain: expiry rules, drift
tolerances, or any "still valid?" logic — those are `execute`'s re-validation
policy (step 17). The proposal carries facts; `execute` owns judgment about
staleness.

`proposals/` is **gitignored**: proposal files are transient local artifacts
consumed by `execute`, not evidence. (If a propose routine is ever added —
`routines/README.md` defers it on purpose — cloud runs would need the file
committed or notified; that decision belongs to the step that builds the
routine.)

## Config is the source of truth

Unlike `backtest` (an experiment tool whose CLI flags sweep parameters),
`propose` reads every limit from `config/config.yaml` via the `from_config`
constructors that already exist (`GateLimits.from_config`,
`RiskLimits.from_config`, `HaltLimits.from_config`, sizing from
`config.capital`). Flags cover only I/O and selection: `--config`,
`--registry`, `--ledger`, `--proposals-dir` (default from
`config.paths.proposals_dir`), `--strategy`, `--vol-lookback-days`,
`--pretty`. A path that can lead to real money must not be steerable by
ad-hoc flags — and the config fingerprint in each file makes the
configuration that minted it auditable.

## Error handling

- Provider/book failure on one candidate → skip that candidate, count it,
  continue.
- Venue/provider wholly unreachable → summary with `errors`, no proposals,
  exit 2 (unlike a halt, this is a failure).
- Overlay config broken → warn loudly (WARNING log), proceed unmodified.
- Missing live ledger → empty book, not an error (it's the pre-step-17 normal).
- Two runs in the same second for the same ticker would collide on filename:
  the writer refuses to overwrite an existing file and errors that run instead
  (never silently replace a proposal a human may be reading).

## Testing

- **Extraction**: the untouched backtest suite is the oracle (same trades, same
  skip counters); paper's existing cross-check test keeps guarding the seam.
  No new tests needed to prove identity — that is the point of the oracle.
- **Propose**: fixture-driven (fake venue + provider, real gate stack — the
  step-15 `paper_env` pattern): a clean run writes N files with valid schema;
  a halted run writes none and says why; slot-capping (more passing candidates
  than free slots ⇒ only free-slot-count files, ranked by net edge); overlay
  narrowing (a blocked underlying yields no file; a bias forbids a side); the
  no-overwrite refusal; money-as-strings round-trip of a written file.

## Invariants to hold at review

1. No order endpoint anywhere in `cli/propose.py`'s import graph; venue and
   provider access read-only.
2. The extraction is behaviour-identical: backtest suite passes untouched, and
   the engine's skip counters are byte-for-byte the same names and counts.
3. `propose` reads limits only from config (no limit-bearing CLI flags).
4. Proposals are facts-only: no venue mutation, no execute-side judgment baked
   in.
5. The live ledger is read-only to `propose` (only `execute` will ever append).
6. A halted run writes zero proposal files.
7. Money is Decimal end to end, serialized as strings in the file.
