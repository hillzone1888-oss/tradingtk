# Execute + Reconcile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `execute` — the only order-submitting path, six gates deep, interactive-only — and `reconcile`, the safe command that settles the live ledger from venue truth and verifies the fee model against real fills.

**Architecture:** Task 1 makes the live ledger real (committed path + `cli/reconcile.py`, reusing paper's settle logic and `reconcile_fill`). Task 2 builds the private order client INSIDE `cli/execute.py` (signed via the existing `KalshiSigner`; the venue adapter stays order-free) with an isolated response-parse layer and request-shape contract tests. Task 3 adds the six-gate stack, the full re-decision (reusing `assess_candidate` + screens), the t0-vs-t1 display, the typed confirmation, the order lifecycle (place → poll → timeout-cancel → record reality), and the ledger append. Task 4 ticks the docs.

**Tech Stack:** Python 3.12 (`uv`), httpx, cryptography (RSA-PSS, already a dep), `Decimal` money, pytest, ruff (line-length 100).

## Global Constraints

- **Money is `Decimal`, never float.** Kalshi prices cross the wire as integer cents — convert at the boundary only.
- **All commands `uv run --system-certs`** (never `--native-tls`).
- **The order endpoint is called from exactly one module: `src/tradetk/cli/execute.py`.** No order method may be added to `venues/kalshi.py` or anywhere else. `cli/reconcile.py` must have no order path in its import graph.
- **`execute` refuses non-interactively.** `sys.stdin.isatty()` gate + typed phrase read from stdin; no flag/env/config bypass may exist.
- **Credentials:** key id from env var `KALSHI_API_KEY_ID`, private key path from env var `KALSHI_PRIVATE_KEY_PATH` (the file path is the secret's pointer — never inline key material). Nothing about credentials is ever logged, printed, or persisted — including in error paths and reprs.
- **No automatic order retries or re-submissions, ever.** Unknown state after a network error → report the order id and stop.
- **Record reality:** ledger fill events carry venue-reported contracts/price/fee, not the proposal's numbers.
- **ruff line-length 100** by eye (repo ruff doesn't enforce E501); don't change ruff config. Full suite green + ruff clean at every task end. One commit per task, message style `Execute: <summary>`.
- **The controller/assistant never runs `execute`'s live path** — tests use fakes; the first real demo order is the human's.

---

### Task 1: The live ledger becomes real — `data/live/` + `cli/reconcile.py`

**Files:**
- Create: `src/tradetk/cli/reconcile.py`
- Modify: `.gitignore` (negate `data/live/`, mirroring `data/paper/`)
- Create: `data/live/README.md` (keeper)
- Test: `tests/test_reconcile_cli.py`

**Interfaces:**
- Consumes: `read_ledger`, `append_events`, `settle_event`, `project` (state.ledger); `settle_position` (state.settle); `reconcile_fill`, `KalshiFeeModel`, `FeeRounding` (costs.fees); `KalshiVenue` (venues, read-only); `load_config`.
- Produces: `run_reconcile(*, config, ledger_path, venue, now) -> dict` (summary: `settled`, `pending_settlement`, `fee_reconciliation`, `errors`); `main(argv) -> int`.

- [ ] **Step 1: gitignore + keeper**

Mirror the `data/paper/` pattern in `.gitignore` exactly (`!data/live/`, `!data/live/**`); create `data/live/README.md`:
```
# live book lives here (ledger.jsonl); committed so real fills survive any clone.
# fill events are appended ONLY by `execute`; settle events ONLY by `reconcile`.
```
Verify: `git check-ignore data/live/ledger.jsonl; echo "exit=$?"` → `exit=1`.

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_reconcile_cli.py
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tradetk.cli.reconcile import run_reconcile
from tradetk.state.ledger import append_events, fill_event, read_ledger

D = Decimal
NOW = datetime(2026, 8, 7, 12, tzinfo=timezone.utc)


class _FakeVenue:
    def __init__(self, results):  # {ticker: (status, result)}
        self._r = results

    def market(self, ticker):
        from tradetk.venues.base import VenueMarket
        status, result = self._r.get(ticker, ("open", None))
        return VenueMarket(ticker=ticker, title="x", status=status, result=result)


def _seed_fill(path, ticker="A", underlying="BTC", contracts=5, cost="1.75",
               fee="0.05", price="0.34", resolution=NOW - timedelta(hours=1)):
    append_events(path, [fill_event(
        ticker=ticker, underlying=underlying, side="yes", contracts=contracts,
        assumed_price=D(price), fee=D(fee), cost=D(cost),
        resolution_time=resolution, ts=NOW - timedelta(days=1),
    )])


def test_resolved_position_settles_and_is_idempotent(tmp_path, reconcile_config):
    ledger = tmp_path / "ledger.jsonl"
    _seed_fill(ledger)
    venue = _FakeVenue({"A": ("finalized", "no")})
    s1 = run_reconcile(config=reconcile_config, ledger_path=ledger, venue=venue, now=NOW)
    assert s1["settled"][0]["ticker"] == "A"
    events = read_ledger(ledger)
    assert sum(1 for e in events if e["type"] == "settle") == 1
    s2 = run_reconcile(config=reconcile_config, ledger_path=ledger, venue=venue, now=NOW)
    assert s2["settled"] == []          # already settled; idempotent
    assert sum(1 for e in read_ledger(ledger) if e["type"] == "settle") == 1


def test_unresolved_past_resolution_is_pending_never_forced(tmp_path, reconcile_config):
    ledger = tmp_path / "ledger.jsonl"
    _seed_fill(ledger)
    venue = _FakeVenue({"A": ("open", None)})
    s = run_reconcile(config=reconcile_config, ledger_path=ledger, venue=venue, now=NOW)
    assert s["pending_settlement"] == ["A"]
    assert not any(e["type"] == "settle" for e in read_ledger(ledger))


def test_fee_reconciliation_reports_drift(tmp_path, reconcile_config):
    ledger = tmp_path / "ledger.jsonl"
    # fee recorded as 0.99 on a 5 x $0.34 fill — the model will disagree loudly
    _seed_fill(ledger, fee="0.99")
    venue = _FakeVenue({"A": ("open", None)})
    s = run_reconcile(config=reconcile_config, ledger_path=ledger, venue=venue, now=NOW)
    rec = s["fee_reconciliation"][0]
    assert rec["ticker"] == "A" and rec["matches"] is False


def test_one_bad_read_skips_that_position_only(tmp_path, reconcile_config):
    ledger = tmp_path / "ledger.jsonl"
    _seed_fill(ledger, ticker="A")
    _seed_fill(ledger, ticker="B", underlying="ETH")

    class _Flaky(_FakeVenue):
        def market(self, ticker):
            if ticker == "A":
                raise RuntimeError("boom")
            return super().market(ticker)

    venue = _Flaky({"B": ("finalized", "yes")})
    s = run_reconcile(config=reconcile_config, ledger_path=ledger, venue=venue, now=NOW)
    assert any("A" in e for e in s["errors"])
    assert s["settled"][0]["ticker"] == "B"
```

Add a tiny `reconcile_config` fixture (conftest or this file): load `config.example.yaml` the way existing fixtures do — read `tests/conftest.py` first and reuse its config builder.

- [ ] **Step 3: Run to verify failure**

Run: `uv run --system-certs pytest tests/test_reconcile_cli.py -v`
Expected: FAIL — no module `tradetk.cli.reconcile`.

- [ ] **Step 4: Implement `cli/reconcile.py`**

```python
# src/tradetk/cli/reconcile.py
"""``reconcile`` — settle the live book from venue truth; verify fees vs reality.

Safe by construction: read-only venue calls, and the only ledger writes are
``settle`` events recording what the venue already decided. Fill events are
``execute``'s alone; a settle cannot create risk. Runnable constantly — the
sweep routine may adopt it later.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import truststore

from tradetk.config.loader import load_config
from tradetk.costs.fees import FeeRounding, KalshiFeeModel, reconcile_fill
from tradetk.state.ledger import append_events, project, read_ledger, settle_event
from tradetk.state.settle import settle_position
from tradetk.venues.kalshi import KalshiVenue

log = logging.getLogger("tradetk.cli.reconcile")


def run_reconcile(
    *, config: Any, ledger_path: str | Path, venue: Any, now: datetime,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "settled": [], "pending_settlement": [], "fee_reconciliation": [], "errors": [],
    }
    events = read_ledger(ledger_path)
    book = project(
        events, starting_capital=Decimal(str(config.capital.total_capital)), today=now.date()
    )

    # -- settle from venue truth (paper's exact posture) -----------------
    settles = []
    for pos in book.open:
        try:
            market = venue.market(pos.ticker)
        except Exception as exc:  # noqa: BLE001 - one bad read must not kill the run
            summary["errors"].append(f"settle-read {pos.ticker}: {exc}")
            continue
        outcome = settle_position(
            side=pos.side, contracts=pos.contracts, cost=pos.cost, market=market
        )
        if outcome is None:
            if pos.resolution_time <= now:
                summary["pending_settlement"].append(pos.ticker)
            continue
        settles.append(settle_event(
            ticker=pos.ticker, result=outcome.result, side=pos.side,
            contracts=pos.contracts, proceeds=outcome.proceeds,
            realized_pnl=outcome.realized_pnl, resolution_time=pos.resolution_time, ts=now,
        ))
        summary["settled"].append(
            {"ticker": pos.ticker, "realized_pnl": str(outcome.realized_pnl)}
        )
    append_events(ledger_path, settles)

    # -- fee model vs recorded reality ------------------------------------
    model = KalshiFeeModel(rounding=FeeRounding.cent)
    for e in events:
        if e["type"] != "fill":
            continue
        try:
            rec = reconcile_fill(
                model, int(e["contracts"]), e["assumed_price"], e["fee"]
            )
            rec["ticker"] = e["ticker"]
            summary["fee_reconciliation"].append(rec)
            if not rec["matches"]:
                log.warning(
                    "fee model mismatch on %s: predicted %s, actual %s",
                    e["ticker"], rec["predicted_fee"], rec["actual_fee"],
                )
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"fee-reconcile {e['ticker']}: {exc}")
    return summary


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Settle the live book from venue results.")
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--ledger", default="data/live/ledger.jsonl")
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr
    )
    truststore.inject_into_ssl()
    config = load_config(args.config)
    now = datetime.now(timezone.utc)
    with KalshiVenue(environment=config.venue.environment.value) as venue:
        summary = run_reconcile(
            config=config, ledger_path=args.ledger, venue=venue, now=now
        )
    print(json.dumps(summary, indent=2 if args.pretty else None, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

Note: fee reconciliation intentionally reads the PRE-settle `events` list (fills only); a fill recorded with `assumed_price` (paper's field name, reused for the actual average price in live fills) reconciles fine. Verify `reconcile_fill(model, contracts, price, actual_fee)` positional signature against `src/tradetk/costs/fees.py:258` before relying on it.

- [ ] **Step 5: Run to verify pass, full suite, ruff, commit**

Run: `uv run --system-certs pytest tests/test_reconcile_cli.py -v` → all pass; `uv run --system-certs pytest -q` → green; `uv run --system-certs ruff check src/tradetk/cli/reconcile.py tests/test_reconcile_cli.py` → clean.

```bash
git add .gitignore data/live/README.md src/tradetk/cli/reconcile.py tests/test_reconcile_cli.py tests/conftest.py
git commit -m "Execute: reconcile — settle the live book from venue truth, verify fees"
```

---

### Task 2: The order client — private, signed, isolated in `cli/execute.py`

The ONLY file allowed to talk to the order endpoint. This task creates the module with just the client + parse layer; Task 3 adds the gates and `main`.

**Files:**
- Create: `src/tradetk/cli/execute.py` (client portion)
- Test: `tests/test_execute_client.py`

**Interfaces:**
- Consumes: `KalshiSigner`, `BASE_URLS`, `API_PREFIX`, `VenueAuthError`, `VenueError` from `venues.kalshi` (importing the signer is fine — it is not an order endpoint).
- Produces (Task 3 relies on these):
  - `OrderResult(order_id, status, filled_contracts: int, average_price: Decimal | None, fee: Decimal | None, fee_source: str, raw_status: str)`.
  - `_OrderClient(environment, key_id, private_key_path, *, client=None)` with `.place_limit(*, ticker, side, contracts, price_cents, client_order_id) -> str` (order id), `.status(order_id) -> dict`, `.cancel(order_id) -> None`, `.fills(order_id) -> list[dict]`, `.result(order_id) -> OrderResult`.
  - `_dollars_to_cents(price: Decimal) -> int` (exact; raises on sub-cent remainder).

- [ ] **Step 1: Verify the wire format against the official docs**

Before writing code, fetch Kalshi's official API reference (docs.kalshi.com — the trade-api/v2 portfolio endpoints) and confirm: the create-order path and body field names (`action`, `client_order_id`, `count`, `side`, `ticker`, `type`, `yes_price`/`no_price` in integer cents), the get-order path and its `status` values, the cancel verb/path, the fills path and per-fill fields, and **where the actual fee appears** (order object vs fills). Record what you confirmed (with the doc URL) in your report. If the actual fee is genuinely absent from both order and fill responses, `OrderResult.fee_source` becomes `"schedule"` and the fee is computed from the live fee schedule — an honest fallback that Task 3 surfaces in the summary; otherwise `fee_source` is `"venue"`.

- [ ] **Step 2: Write the failing contract tests**

Fake the transport (httpx `MockTransport` or a stub client object), never the signer — sign with a throwaway RSA key generated in the test (cryptography is a dep). Assert:

```python
# tests/test_execute_client.py  (shape — adjust field names to what Step 1 confirmed)
def test_place_limit_signs_and_shapes_the_request(...):
    # captures the outgoing request:
    #   POST {base}/trade-api/v2/portfolio/orders
    #   headers include KALSHI-ACCESS-KEY / -SIGNATURE / -TIMESTAMP
    #   body: action=buy, type=limit, side/ticker/count as given,
    #         yes_price (or no_price) == price_cents, client_order_id passed through
    ...

def test_result_parses_fill_and_fee(...):
    # scripted status+fills responses -> OrderResult with venue-reported
    # contracts, Decimal average price (cents -> dollars), fee + fee_source
    ...

def test_dollars_to_cents_is_exact():
    assert _dollars_to_cents(Decimal("0.34")) == 34
    with pytest.raises(ValueError):
        _dollars_to_cents(Decimal("0.3450"))

def test_no_credentials_refuses_before_any_request(...):
    # constructing/calling without key id + key path raises VenueAuthError;
    # the fake transport records ZERO requests
    ...

def test_error_never_contains_key_material(...):
    # force a 401; assert the raised message contains neither the key id
    # nor the key path contents
    ...
```

- [ ] **Step 3: Run to verify failure; implement the client**

The client is ~120 lines: an httpx.Client against `BASE_URLS[environment]`, every request signed via `KalshiSigner.headers(method, full_path)`, POST/GET/DELETE helpers private to the class, and `result()` composing status+fills into `OrderResult`. Decimal at every boundary (`_dollars_to_cents` on the way out; cents→`Decimal("0.01")` multiples on the way in). Follow the module docstring pattern of the repo; the module docstring must state loudly: **this module is the only order path in the project, and the assistant never runs it.**

- [ ] **Step 4: Verify pass, ruff, commit**

Run: `uv run --system-certs pytest tests/test_execute_client.py -v` → pass; full suite green; ruff clean.

```bash
git add src/tradetk/cli/execute.py tests/test_execute_client.py
git commit -m "Execute: the order client — signed, isolated, reality-parsing"
```

---

### Task 3: The gate stack, re-decision, confirmation, lifecycle

**Files:**
- Modify: `src/tradetk/cli/execute.py` (add gates + `run_execute` + `main`)
- Test: `tests/test_execute_gates.py`

**Interfaces:**
- Consumes: Task 2's client surface; `assess_candidate` (translation.assessment); `screen_new_entry`, `screen_cost`, `screen_halts`, `BookHealth`, `HaltLimits`, `RiskLimits` (risk); `read_ledger`, `project`, `append_events`, `fill_event` (state.ledger); `config_fingerprint` (proposals); `GateLimits`/`SizingLimits.from_config`; `load_underlying_data` (cli.backtest), `_data_age` (cli.paper); overlay loading exactly as `cli/propose.py:main` does; `parse_claim` on the proposal's ticker via `venue.market()`; `reconcile_fill`.
- Produces: `run_execute(*, config, proposal, registry, ledger_path, venue, order_client, data, overlay, strategy, now, io) -> dict` — testable core; `io` is a tiny injected interface `(is_tty() -> bool, prompt(text) -> str, show(text) -> None)` so tests script the human; `main(argv) -> int` wires the real TTY (`sys.stdin.isatty`, `input`).

- [ ] **Step 1: Write the failing gate tests**

One test per refusal, each asserting (a) the exact `refused` reason string in the summary and (b) `order_client.calls == []` — the fake client records every invocation, and a refusal means zero:

```python
# tests/test_execute_gates.py — reasons (exact strings):
#   not_a_tty                mode_not_live            prod_not_flagged
#   maker_config             fingerprint_mismatch     claim_resolved
#   already_open             halted:<breaker>         side_flipped
#   redecision_failed        confirmation_mismatch
# plus:
#   test_contracts_capped_at_proposal      (fresh 7 vs proposal 5 -> order 5)
#   test_contracts_follow_smaller_fresh    (fresh 3 vs proposal 5 -> order 3)
#   test_exact_phrase_places_order         (fake fills fully; ledger fill event
#       carries the FAKE VENUE's numbers, not the proposal's; reconcile_fill ran)
#   test_partial_fill_records_partial      (timeout -> cancel called; ledger
#       event has filled=2 of 5 at the venue-reported average)
#   test_zero_fill_records_nothing         (cancel called; no ledger event;
#       summary says so)
#   test_unknown_state_reports_order_id    (status() raises after place ->
#       summary carries order_id + "state_unknown"; NO retry call recorded)
```

Build an `execute_env` fixture (conftest): reuse `propose_env`'s recipe (real config loaded from `config.example.yaml` **with `mode: live`, `live_trading_confirmed: true`, `prefer_maker: false`, `allow_crossing: true` overridden in the fixture copy**), a real proposal dict minted by `build_proposal` (reuse Task-2-of-propose's fixture recipe), a scripted `_FakeOrderClient` (records `calls`, scripted fill outcomes), a scripted `io` (`is_tty` flag, canned prompt answers), fake venue/data/strategy as in `propose_env`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run --system-certs pytest tests/test_execute_gates.py -v`
Expected: FAIL — `run_execute` doesn't exist.

- [ ] **Step 3: Implement the gate stack + lifecycle**

`run_execute` runs the gates IN THIS ORDER, returning `{"refused": <reason>}` on the first failure (and appending nothing):

1. `io.is_tty()` → else `not_a_tty`.
2. `config.mode is Mode.live` (validator already forced `live_trading_confirmed`) → else `mode_not_live`. Environment prod additionally requires `config.venue.use_production` (`prod_not_flagged`) **and** a second prompt whose answer must be exactly `LIVE ORDERS ON PRODUCTION` (reason `prod_phrase_mismatch`). `config.orders.prefer_maker` must be False and `allow_crossing` True → else `maker_config` with the explanation string.
3. Proposal facts: `proposal["schema_version"] == 1`; `config_fingerprint(config) == proposal["config_fingerprint"]` → else `fingerprint_mismatch`; fetch `venue.market(ticker)` → parse claim; resolved (`market.result` truthy or `claim.resolution_time <= now`) → `claim_resolved`; ticker open in the live ledger projection → `already_open`.
4. Halts: `BookHealth` from the live-ledger projection + `_data_age(data, now)` → `screen_halts` → `halted:<reason>`.
5. Re-decision: fresh snapshot → `strategy.estimate` (abstain → `redecision_failed`) → `assess_candidate(..., overlay=overlay)`; assessment None → `redecision_failed`; `assessment.side.value != proposal["decision"]["side"]` → `side_flipped`; `screen_new_entry` + `screen_cost` → `redecision_failed`. Order contracts = `min(assessment.contracts_requested, proposal_contracts)`; price cap = the worst (marginal) level price consumed walking the fresh book for that size — compute with a small local helper that walks `book.yes_asks`/`yes_bids` the same way `walk_to_buy_*` does but returns the deepest level price touched. Never place above it.
6. Show t0 vs t1 (side, contracts, avg price, cost, fee, net edge, and deltas) via `io.show`; `io.prompt("type the confirmation phrase: ")` must equal `config.orders.confirmation_phrase` exactly → else `confirmation_mismatch`.

Then the lifecycle: `client_order_id = uuid4().hex`; `order_client.place_limit(...)`; poll `result(order_id)` every ~2s until filled or `orders.limit_order_timeout_seconds` (inject a `sleep`/clock for tests); on timeout `cancel(order_id)` (a cancel failure is reported with the id, not raised); build the summary from `OrderResult`: full/partial fill → `fill_event` with **venue numbers** (`assumed_price=result.average_price`, `fee=result.fee`, `cost=contracts*avg+fee` — compute Decimal-exact) appended to the ledger + `reconcile_fill` run and its dict in the summary; zero fill → no event; any exception after placement → `{"order_id": ..., "state_unknown": True}` and STOP (no retry).

`main(argv)`: args `--proposal` (required), `--config`, `--registry`, `--ledger` (default `data/live/ledger.jsonl`), `--vol-lookback-days`, `--pretty`. Wires: real `io` (isatty/input/print to stderr), env-var credentials (`KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`; both unset → refusal `no_credentials` BEFORE any client construction), `_OrderClient(config.venue.environment.value, ...)`, venue/provider/data/overlay exactly as `cli/propose.py:main` builds them (single-ticker scope), strategy from the proposal's recorded strategy name. Prints the summary JSON; exit 0 on a completed attempt, 3 on any refusal (distinct from 2 = scan/infra failure).

- [ ] **Step 4: Run all tests, ruff, order-path isolation check, commit**

Run: `uv run --system-certs pytest tests/test_execute_gates.py tests/test_execute_client.py -v` → pass; full suite green; ruff clean.
Isolation check: `uv run --system-certs python -c "import ast,glob; bad=[f for f in glob.glob('src/tradetk/**/*.py', recursive=True) if f.replace('\\\\','/') != 'src/tradetk/cli/execute.py' and 'portfolio/orders' in open(f, encoding='utf-8').read()]; print('BAD:', bad) if bad else print('clean')"` → `clean`.

```bash
git add src/tradetk/cli/execute.py tests/test_execute_gates.py tests/conftest.py
git commit -m "Execute: six gates, full re-decision, typed phrase, record reality"
```

---

### Task 4: Docs — README tick, GUARDRAILS, boundary notes

**Files:**
- Modify: `README.md` (tick step 17; Status header → step 18)
- Modify: `memory/GUARDRAILS.md` (add `reconcile` to the always-safe list; restate that `execute` is never run by a routine or the assistant — match the existing phrasing style)
- Modify: `routines/sweep.md` (one line noting `reconcile` exists and MAY be added to the sweep later — not wired now)

**Interfaces:** none (docs only).

- [ ] **Step 1: Make the three edits** (match each file's existing tone; the README step-17 line gets the established 2–4 line summary style noting: only order path, six gates, interactive-only, reconcile settles the live book).
- [ ] **Step 2: Full suite once (prove nothing broke), commit**

```bash
git add README.md memory/GUARDRAILS.md routines/sweep.md
git commit -m "Execute: tick step 17 — the boundary is real"
```

---

## Self-Review

**Spec coverage:** taker-capped limit (Task 3 step 5 price-cap helper + maker_config gate); full re-decision (gate 5 reusing assess_candidate/screens; side_flipped; min-contracts cap); six-gate order incl. prod double-phrase (gates 1–6); no age knob (absent by design); order client isolated in execute (Tasks 2–3 + isolation check); credentials via env/named files, never logged (Task 2 tests); record reality + reconcile_fill on the spot (Task 3 lifecycle tests); no auto-retry (unknown-state test); reconcile command with settle/pending/fee-drift/skip-one posture (Task 1); `data/live/` committed (Task 1); docs (Task 4).
**Placeholder scan:** Task 2's tests are shape-sketches by necessity — the exact wire field names come from Step 1's mandatory doc verification (recorded in the report); this is a directed verification, not a gap. No TODO/TBD.
**Type consistency:** `OrderResult` fields consumed by Task 3's lifecycle match Task 2's definition; `run_execute`/`run_reconcile` summary keys match their tests; refusal strings listed once and used in both steps; `fill_event(assumed_price=...)` reuse matches `state/ledger.py`'s real signature.
