# Spec — Step 17: `execute` and `reconcile`

*Design doc. Date: 2026-08-07. Build step 17 of 19.*

## What this is

The other half of the two-command execution boundary — and the only path in
the project that will ever submit an order:

> `execute --proposal <file>` — the **only** path that submits an order.
> Re-validates against the live book + risk state, refuses if anything material
> moved, and requires interactive typed confirmation. Refuses to run
> non-interactively. **The human runs this, never the assistant.**

Plus `reconcile` — the safe command that keeps the live ledger truthful after
real trades exist: settles resolved positions from venue results and verifies
the fee model against real fills.

The standing rules bind absolutely: the assistant never runs `execute` (not
with flags, not in a script, not "just to test"); a routine's strongest output
remains a proposal file path. `reconcile` is safe to run constantly, like
`propose`.

## Decisions locked during brainstorming

- **Taker-capped limit.** The order is a limit priced at the fresh assessment's
  worst average price — it crosses the spread and fills against the book that
  was just re-validated, and can never pay more than the gated numbers. This is
  the only execution style the cost model (taker fees, spread, book-walk
  slippage) actually describes. It requires `orders.prefer_maker: false` and
  `orders.allow_crossing: true` in config — a deliberate, visible choice;
  `execute` refuses under `prefer_maker: true` with an explanation (maker fill
  probability and adverse selection are unmodeled — building that model is a
  future step, not a flag).
- **Full re-decision.** "Anything material moved" is decided by the pipeline,
  not by drift tolerances: `execute` re-runs the entire decision for this one
  ticker — fresh candles, fresh estimate, fresh book, halts, risk state from
  the live ledger — and refuses unless the **same side still clears every gate
  now**. The pipeline must approve the trade twice, with a human in between.
  Zero new judgment code: it reuses `assess_candidate` and the screens.
- **Safe `reconcile` command.** Fill events only ever from `execute`; settle
  events only from `reconcile`, which reads venue results (read-only), reuses
  paper's settle logic verbatim, and runs `reconcile_fill` over recorded fills
  to verify the fee model against reality — loudly flagging any mismatch.
  Non-interactive and safe by construction, so the sweep routine can adopt it
  later.

## Deliberate calls (approved)

- **No proposal-age knob.** The full re-decision makes staleness a solved
  problem — an old proposal either still clears every gate today or it
  refuses — and `created_at` is shown to the human. Fewer judgment knobs.
- **Contracts capped at the proposal.** The order size is the *fresh*
  assessment's contract count, capped at the proposal's — `execute` may size
  **down** on a thinned book, never up. The human picked up the file for a
  ceiling; it stays a ceiling.
- **Scaling position size later is a config decision, not a code path.**
  Position size comes only from config (`capital.position_target`,
  `capital.total_capital`). If performance warrants a bigger book, the human
  raises those values — one visible edit, which changes the config fingerprint
  so all previously-minted proposals refuse themselves. Per the honesty rules,
  "performance warrants" is answered from **calibration + shadow evidence,
  never the balance** — live P&L on a $20 book is noise. Nothing in `execute`
  ever sizes above its proposal.

## `execute` — the gate stack

Gates run in order; the first failure refuses with the exact reason and a
non-zero exit. Nothing is placed until every gate and the human have passed.

1. **Interactive TTY.** `sys.stdin.isatty()` must be true. No flag overrides
   this; a scheduled routine physically cannot run the command.
2. **Config gates.** `mode: live` with `live_trading_confirmed: true` (the
   schema validator already pairs them); venue environment **demo** unless
   `use_production: true`, and production additionally demands a second typed
   phrase naming the environment (`LIVE ORDERS ON PRODUCTION`); the taker
   flags as above.
3. **Proposal facts.** The file parses as `schema_version: 1`; the **config
   fingerprint matches** the running config (a proposal minted under different
   limits, venue, or fees refuses); the claim is not resolved; the ticker is
   not already open in the live ledger — and the ledger's `fill:{ticker}`
   idempotency key hard-stops a double-buy even if this check were bypassed.
4. **Halt gate.** Fresh `BookHealth` projected from the live ledger (staleness
   from the fresh candles pulled for the re-decision); any tripped breaker
   refuses.
5. **Full re-decision.** Fresh candles → fresh estimate → fresh book →
   `assess_candidate` (overlay loaded exactly as `propose` loads it) →
   `screen_new_entry` → `screen_cost`. Refuse unless the assessment passes
   **and** its side equals the proposal's side. Order contracts =
   `min(fresh contracts, proposal contracts)`; order price cap = the fresh
   assessment's worst average price.
6. **The human.** Print the t0 (proposal) and t1 (fresh) numbers side by side
   — side, contracts, price, cost, fee, net edge, and the deltas — then
   require the typed `orders.confirmation_phrase`, exactly, from the TTY.
   Anything else aborts.

## The order path

- **The order client lives inside `cli/execute.py`** — a private class in the
  command module, per the operating rule that no code outside the execute
  module may call the venue order endpoint. The venue adapter stays order-free
  by design and is still used for all reads.
- **Auth** reuses the existing RSA-PSS signer (`venues/kalshi.py`): key id
  from an environment variable, private key by file path, nothing logged —
  the signer's existing hygiene rules apply to order requests unchanged.
- **Lifecycle:** place the capped limit → poll order status until filled or
  `orders.limit_order_timeout_seconds` → cancel any remainder → report.
- **Record reality, not intent.** The ledger `fill` event carries the venue's
  actual filled contracts, actual average price, and actual fee. A partial
  fill records the partial; a zero fill records nothing and says so. The
  proposal's numbers were the *authorization*; the ledger holds the *facts*.
- **Fee-model reconciliation on the spot:** `reconcile_fill` (built at step 7
  to settle the cent-vs-centicent rounding ambiguity) runs against the real
  fill immediately, and a mismatch between modeled and actual fee is reported
  loudly (WARNING + summary field), never swallowed.

## `reconcile` — closing the loop

`python -m tradetk.cli.reconcile` (own module; no order endpoint in its import
graph):

1. Project the live ledger; for each open position, read `venue.market()`
   (read-only) and settle via the same `settle_position` paper uses; append
   `settle` events (idempotent `settle:{ticker}` key). Positions past
   resolution with no venue result yet are reported `pending_settlement`,
   never force-settled.
2. Re-run `reconcile_fill` over the ledger's fill events; report any fee-model
   drift.
3. Emit a JSON summary (settled, pending, fee_reconciliation, errors) —
   automatable by the sweep later; that wiring is a follow-up, not this step.

The live ledger becomes committed data now: `data/live/` gets the same
gitignore negation + keeper README as `data/paper/` — real fills must survive
a cloud clone's destruction.

## Error handling

- Any venue/API failure during placement or polling: report the order state as
  last known, never retry placement automatically, and if an order was placed
  but its status is unknown, say exactly that with the order id — the human
  resolves it in the venue UI. No automatic re-submission, ever.
- A cancel failure after timeout is reported with the order id (the order may
  still fill; `reconcile` and the ledger's idempotency keep the book truthful
  when it does — recording that late fill is a `reconcile` follow-up noted in
  the summary, not silently dropped).
- `reconcile` failures follow paper's posture: one bad read skips that
  position, never the run.

## Testing

Everything except the real HTTP order call is fake-driven:

- **Gate stack:** one test per refusal (non-TTY, mode, prod-without-flag,
  maker-config, fingerprint mismatch, resolved claim, already-open ticker,
  halt, side-flip, gate-fail on re-decision) — each asserting the exact
  refusal reason and that **no order call was attempted** (fake client records
  invocations).
- **Cap semantics:** fresh assessment larger than proposal → order uses
  proposal's contracts; smaller → fresh contracts.
- **Confirmation:** wrong phrase → abort, no order; exact phrase → order
  placed (fake).
- **Lifecycle:** fill / partial fill / timeout-cancel paths against a scripted
  fake order client; the ledger event carries the fake venue's actual numbers;
  `reconcile_fill` mismatch surfaces.
- **`reconcile`:** settle/pending/idempotency (reusing the paper settle test
  patterns) + fee-drift reporting.
- **The order client** gets a request-shape contract test (signed headers,
  path, body against Kalshi's documented order schema). The first live demo
  order is run by the human, by design.

## Invariants to hold at review

1. The order endpoint is called from exactly one module: `cli/execute.py`.
   `reconcile`, like every other command, has no order path in its import
   graph.
2. `execute` refuses non-interactively — no flag, env var, or config value
   can bypass the TTY check or the typed phrase.
3. Demo by default; production requires the config pair AND the second typed
   phrase.
4. No order is attempted until every gate has passed, and no gate is
   re-orderable around the human confirmation.
5. Order size never exceeds the proposal's contracts; order price never
   exceeds the fresh assessment's worst average price.
6. The ledger records venue-reported reality (fill events from `execute`
   only; settle events from `reconcile` only), and `reconcile_fill` runs on
   every real fill.
7. Credentials: key id from env, private key by file path, nothing logged or
   persisted — unchanged signer hygiene.
8. Money is Decimal end to end.
