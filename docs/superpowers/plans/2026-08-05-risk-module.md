# Risk Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the backtest's three book-level risk checks (slot cap, per-underlying concentration, capital cap) into a pure, shared `risk/` core, behaviour-identical.

**Architecture:** A functional core with no state — `RiskLimits` (caps), `RiskState` (an immutable snapshot of the open book), and two pure decision functions mirroring the backtest's two existing checkpoints. The backtest keeps its own `open_positions` and derives a `RiskState` to consult the core; a future executor will do the same against a persisted book. No stateful ledger, so no parallel bookkeeping to keep in sync.

**Tech Stack:** Python 3.12 (via `uv`), `Decimal` money, frozen dataclasses, pytest. Lint: `ruff` (line length 100). All commands use `uv run --system-certs` on this machine (corporate MITM CA).

## Global Constraints

- Python 3.12 via `uv`; run tests and lint with `uv run --system-certs …` (the deprecated flag is `--native-tls`; do not use it).
- Money is `Decimal`, never float. Import as `from decimal import Decimal`.
- `ruff check src tests scripts` must pass; line length 100.
- Behaviour-identity is the acceptance test: the full existing suite must pass **unchanged** — same trades, same skip counts. The backtest is the oracle.
- The risk core is pure: no I/O, no logging, no mutation, no exceptions in the normal path.
- Do NOT build the declared halts (`max_daily_loss_dollars`, `max_total_drawdown_dollars`, `data_staleness_halt_seconds`). They are a named seam wired at step 15, not here.
- Do NOT change probability, sizing, settlement, or venue behaviour.
- Every commit ends with this trailer, verbatim:
  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01N4kWT7mUaXLnRBSrTw8uMa
  ```

---

### Task 1: The pure risk core

**Files:**
- Create: `src/tradetk/risk/limits.py`
- Create: `src/tradetk/risk/state.py`
- Create: `src/tradetk/risk/gate.py`
- Modify: `src/tradetk/risk/__init__.py` (replace the scaffold docstring with real exports)
- Test: `tests/test_risk_gate.py`

**Interfaces:**
- Consumes: nothing (leaf module). `RiskLimits.from_config` reads `config.capital.{max_positions, max_slots_per_underlying, total_capital}` — the existing `CapitalConfig` (see `src/tradetk/config/schema.py`), whose validators already enforce `max_slots_per_underlying <= max_positions`.
- Produces (Task 2 relies on these exact names/types):
  - `RiskLimits(max_positions: int, max_slots_per_underlying: int, total_capital: Decimal)`, `RiskLimits.from_config(config) -> RiskLimits`
  - `OpenRisk(ticker: str, underlying: str, capital_at_risk: Decimal)`
  - `RiskState(open: tuple[OpenRisk, ...] = ())` with `.slots_used: int`, `.slots_for(underlying: str) -> int`, `.capital_deployed: Decimal`
  - `RiskDecision(admitted: bool, reason: str | None = None)`
  - `screen_new_entry(underlying: str, state: RiskState, limits: RiskLimits) -> RiskDecision`
  - `screen_cost(capital_at_risk: Decimal, state: RiskState, limits: RiskLimits) -> RiskDecision`

- [ ] **Step 1: Write the failing test**

Create `tests/test_risk_gate.py`:

```python
"""The book-level risk gate: pure decisions over an immutable snapshot.

These are known-answer tests. The boundary operators matter — a slot cap that
admits one position too many, or a capital cap off by a cent, is a real-money
error that no downstream test would catch — so the exact `>=` / `>` edges are
pinned here.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tradetk.risk import (
    OpenRisk,
    RiskLimits,
    RiskState,
    screen_cost,
    screen_new_entry,
)

D = Decimal

LIMITS = RiskLimits(max_positions=3, max_slots_per_underlying=2, total_capital=D("20.00"))


def _state(*positions: tuple[str, str, str]) -> RiskState:
    return RiskState(
        open=tuple(OpenRisk(t, u, D(c)) for t, u, c in positions)
    )


# ── slot cap ───────────────────────────────────────────────────────


def test_empty_book_admits() -> None:
    decision = screen_new_entry("BTC", RiskState(), LIMITS)
    assert decision.admitted is True
    assert decision.reason is None


def test_a_full_book_is_refused_a_new_slot() -> None:
    state = _state(("A", "BTC", "2"), ("B", "ETH", "2"), ("C", "SOL", "2"))
    decision = screen_new_entry("DOGE", state, LIMITS)
    assert decision.admitted is False
    assert decision.reason == "no_free_slot"


def test_the_slot_cap_binds_at_greater_or_equal() -> None:
    """3 open against a cap of 3 must refuse, not admit a fourth."""
    state = _state(("A", "BTC", "2"), ("B", "ETH", "2"), ("C", "SOL", "2"))
    assert screen_new_entry("BTC", state, LIMITS).reason == "no_free_slot"


# ── per-underlying concentration ───────────────────────────────────


def test_an_underlying_at_its_cap_is_refused() -> None:
    state = _state(("A", "BTC", "2"), ("B", "BTC", "2"))
    decision = screen_new_entry("BTC", state, LIMITS)
    assert decision.admitted is False
    assert decision.reason == "underlying_concentration_limit"


def test_a_different_underlying_is_still_admitted() -> None:
    state = _state(("A", "BTC", "2"), ("B", "BTC", "2"))
    assert screen_new_entry("ETH", state, LIMITS).admitted is True


def test_the_slot_cap_is_checked_before_concentration() -> None:
    """A full book reports no_free_slot even if the underlying also maxed —
    the order the engine records reasons in must not change."""
    full = RiskLimits(max_positions=2, max_slots_per_underlying=2, total_capital=D("20.00"))
    state = _state(("A", "BTC", "2"), ("B", "BTC", "2"))
    assert screen_new_entry("BTC", state, full).reason == "no_free_slot"


# ── capital cap ────────────────────────────────────────────────────


def test_a_cost_within_remaining_capital_is_admitted() -> None:
    state = _state(("A", "BTC", "18.00"))
    assert screen_cost(D("2.00"), state, LIMITS).admitted is True


def test_a_cost_that_exceeds_remaining_capital_is_refused() -> None:
    state = _state(("A", "BTC", "18.50"))
    decision = screen_cost(D("2.00"), state, LIMITS)
    assert decision.admitted is False
    assert decision.reason == "insufficient_capital"


def test_spending_the_book_to_the_penny_is_allowed() -> None:
    """The capital cap binds at strictly greater-than: exactly total is fine."""
    state = _state(("A", "BTC", "18.00"))
    assert screen_cost(D("2.00"), state, LIMITS).admitted is True


def test_one_cent_over_the_book_is_refused() -> None:
    state = _state(("A", "BTC", "18.00"))
    assert screen_cost(D("2.01"), state, LIMITS).reason == "insufficient_capital"


def test_a_negative_cost_is_a_programming_error() -> None:
    with pytest.raises(AssertionError):
        screen_cost(D("-0.01"), RiskState(), LIMITS)


# ── state helpers ──────────────────────────────────────────────────


def test_state_reports_slots_and_capital() -> None:
    state = _state(("A", "BTC", "2.00"), ("B", "BTC", "3.00"), ("C", "ETH", "1.50"))
    assert state.slots_used == 3
    assert state.slots_for("BTC") == 2
    assert state.slots_for("ETH") == 1
    assert state.capital_deployed == D("6.50")


def test_an_empty_state_deploys_zero_capital() -> None:
    assert RiskState().capital_deployed == D("0")


# ── limits from config ─────────────────────────────────────────────


def test_limits_are_read_from_config() -> None:
    """from_config reads config.capital.* and coerces total_capital to Decimal,
    matching how SizingLimits.from_config reads the same block."""
    from types import SimpleNamespace

    config = SimpleNamespace(capital=SimpleNamespace(
        max_positions=6, max_slots_per_underlying=2, total_capital=20.0,
    ))
    limits = RiskLimits.from_config(config)
    assert limits.max_positions == 6
    assert limits.max_slots_per_underlying == 2
    assert limits.total_capital == D("20.0")
    assert isinstance(limits.total_capital, Decimal)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --system-certs python -m pytest tests/test_risk_gate.py -q`
Expected: FAIL — `ImportError: cannot import name 'RiskLimits' from 'tradetk.risk'`.

- [ ] **Step 3: Implement `src/tradetk/risk/limits.py`**

```python
"""The portfolio caps: how many slots, how concentrated, how much capital.

`total_capital` is intentionally duplicated with `SizingLimits` rather than
moved: the sizer needs it too (a position is capped against remaining capital).
Both dataclasses read the same `config.capital.total_capital`, so they cannot
disagree — the single source of truth is the config field, not either struct.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class RiskLimits:
    """Book-level caps. Values come from config and are validated there."""

    max_positions: int
    max_slots_per_underlying: int
    total_capital: Decimal

    @classmethod
    def from_config(cls, config: Any) -> "RiskLimits":
        return cls(
            max_positions=int(config.capital.max_positions),
            max_slots_per_underlying=int(config.capital.max_slots_per_underlying),
            total_capital=Decimal(str(config.capital.total_capital)),
        )
```

- [ ] **Step 4: Implement `src/tradetk/risk/state.py`**

```python
"""A snapshot of the open book, from the risk point of view only.

`RiskState` deliberately knows nothing about settlement, PnL, resolution time,
or strikes. Each consumer builds one from its own storage — the backtest from an
in-memory dict, a future executor from a persisted file — so the same decision
functions serve both without either having to share a book format.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class OpenRisk:
    """One open position, reduced to what a risk decision needs."""

    ticker: str
    underlying: str
    capital_at_risk: Decimal


@dataclass(frozen=True)
class RiskState:
    open: tuple[OpenRisk, ...] = ()

    @property
    def slots_used(self) -> int:
        return len(self.open)

    def slots_for(self, underlying: str) -> int:
        return sum(1 for o in self.open if o.underlying == underlying)

    @property
    def capital_deployed(self) -> Decimal:
        return sum((o.capital_at_risk for o in self.open), Decimal(0))
```

- [ ] **Step 5: Implement `src/tradetk/risk/gate.py`**

```python
"""The decision: admit a candidate, or refuse it with a reason.

Two functions, not one, on purpose. The backtest screens slots and concentration
*before* sizing a candidate — so a full book does not burn sizing work, and the
reason is recorded distinctly — and screens capital *after* sizing, because the
cost is not known until then. Collapsing the two would change which reason
surfaces for a candidate that fails more than one check.

The reasons are open strings, not a closed enum, so the step-15 halt seam can add
`daily_loss_halt` / `drawdown_halt` without breaking any consumer.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tradetk.risk.limits import RiskLimits
from tradetk.risk.state import RiskState


@dataclass(frozen=True)
class RiskDecision:
    admitted: bool
    reason: str | None = None


_ADMIT = RiskDecision(admitted=True, reason=None)


def screen_new_entry(underlying: str, state: RiskState, limits: RiskLimits) -> RiskDecision:
    """Pre-sizing: is there room for one more position in this underlying?"""
    if state.slots_used >= limits.max_positions:
        return RiskDecision(False, "no_free_slot")
    if state.slots_for(underlying) >= limits.max_slots_per_underlying:
        return RiskDecision(False, "underlying_concentration_limit")
    return _ADMIT


def screen_cost(capital_at_risk: Decimal, state: RiskState, limits: RiskLimits) -> RiskDecision:
    """Post-sizing: does this cost fit under the book's capital ceiling?"""
    assert capital_at_risk >= 0, "capital_at_risk must be non-negative"
    if state.capital_deployed + capital_at_risk > limits.total_capital:
        return RiskDecision(False, "insufficient_capital")
    return _ADMIT
```

- [ ] **Step 6: Replace `src/tradetk/risk/__init__.py`**

Replace the entire file (currently a one-line scaffold docstring) with:

```python
"""Book-level risk: the shared gate the backtest and the executor both consult.

Pure and stateless. The caller owns its book and derives a `RiskState` snapshot;
the gate only decides. See docs/superpowers/specs/2026-08-05-risk-module-design.md.
"""

from tradetk.risk.gate import RiskDecision, screen_cost, screen_new_entry
from tradetk.risk.limits import RiskLimits
from tradetk.risk.state import OpenRisk, RiskState

__all__ = [
    "RiskLimits",
    "RiskState",
    "OpenRisk",
    "RiskDecision",
    "screen_new_entry",
    "screen_cost",
]
```

- [ ] **Step 7: Run the risk tests to verify they pass**

Run: `uv run --system-certs python -m pytest tests/test_risk_gate.py -q`
Expected: PASS (14 tests).

- [ ] **Step 8: Run the full suite — nothing else uses `risk/` yet, so it must still be green**

Run: `uv run --system-certs python -m pytest -q`
Expected: PASS, all tests, output pristine.

- [ ] **Step 9: Lint**

Run: `uv run --system-certs ruff check src/tradetk/risk tests/test_risk_gate.py`
Expected: `All checks passed!`

- [ ] **Step 10: Commit**

```bash
git add src/tradetk/risk tests/test_risk_gate.py
git commit -m "$(cat <<'EOF'
Risk core: a pure book-level gate the backtest and executor can share

RiskLimits + RiskState + two decision functions mirroring the backtest's two
checkpoints (slots/concentration pre-sizing, capital post-sizing). No state, no
I/O; the caller owns its book and derives a snapshot. total_capital is sourced
from the same config field as SizingLimits, so the two cannot drift.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N4kWT7mUaXLnRBSrTw8uMa
EOF
)"
```

---

### Task 2: Wire the risk core into the backtest (behaviour-identical)

**Files:**
- Modify: `src/tradetk/backtest/engine.py` (import; constructor; `run()` checkpoints + capital; summary)
- Modify: `src/tradetk/cli/backtest.py` (build `RiskLimits`, pass it, drop the old kwargs)
- Modify: `tests/test_backtest.py` (the `engine()` helper default; the one override call site)
- Test: `tests/test_backtest_overlay.py` — no change expected; it exercises the engine and stands as part of the oracle.

**Interfaces:**
- Consumes: `RiskLimits`, `RiskState`, `OpenRisk`, `screen_new_entry`, `screen_cost` from Task 1.
- Produces: `BacktestEngine.__init__` gains a required keyword-only `risk_limits: RiskLimits` and drops `max_positions` / `max_slots_per_underlying`. The summary JSON keeps the identical `portfolio.max_positions` / `portfolio.max_slots_per_underlying` keys, now read from `risk_limits`.

This is a behaviour-preserving refactor. The acceptance test is the **existing suite passing unchanged**. Make the edits, then run the whole suite; identical trades and skip counts are the definition of done.

- [ ] **Step 1: Add the import to `src/tradetk/backtest/engine.py`**

Find the existing risk-related import line (the module imports `assess_side` etc. from `tradetk.translation.edge`). Add, alongside the other `from tradetk...` imports near the top of the file:

```python
from tradetk.risk import OpenRisk, RiskLimits, RiskState, screen_cost, screen_new_entry
```

- [ ] **Step 2: Swap the constructor parameters**

In `BacktestEngine.__init__` (currently lines ~333-334 and ~346-347), replace the two cap parameters with one `risk_limits`.

Replace these two signature lines:

```python
        max_positions: int = 6,
        max_slots_per_underlying: int = 2,
```

with:

```python
        risk_limits: RiskLimits,
```

(A required keyword-only argument after optional keyword-only ones is valid — every call site is updated in this task.)

Then replace these two assignment lines:

```python
        self.max_positions = max_positions
        self.max_slots_per_underlying = max_slots_per_underlying
```

with:

```python
        self.risk_limits = risk_limits
```

- [ ] **Step 3: Route the capital ceiling through `risk_limits`**

In `run()`, find:

```python
        total_capital = self.sizing_limits.total_capital
```

Replace with:

```python
        total_capital = self.risk_limits.total_capital
```

(Same value — `RiskLimits` and `SizingLimits` both read `config.capital.total_capital` — so this is behaviour-identical. `sizing_limits` still keeps its own `total_capital` for the sizer.)

- [ ] **Step 4: Replace the two inline checkpoints with the risk core**

In `run()`, find this block (the portfolio-limit checks before sizing, currently ~lines 524-534):

```python
            # Portfolio limits, checked before sizing so a full book does not
            # burn work — and so the reason is recorded distinctly.
            if len(open_positions) >= self.max_positions:
                self._skipped["no_free_slot"] += 1
                continue
            same_underlying = sum(
                1 for p in open_positions.values() if p.claim.underlying == claim.underlying
            )
            if same_underlying >= self.max_slots_per_underlying:
                self._skipped["underlying_concentration_limit"] += 1
                continue
```

Replace it with a `RiskState` projection and the pre-sizing screen:

```python
            # Portfolio limits, checked before sizing so a full book does not
            # burn work — and so the reason is recorded distinctly. The book is
            # projected to its risk-relevant fields and judged by the shared
            # gate, so the backtest and the live executor decide identically.
            risk_state = RiskState(open=tuple(
                OpenRisk(t, p.claim.underlying, p.cost)
                for t, p in open_positions.items()
            ))
            entry = screen_new_entry(claim.underlying, risk_state, self.risk_limits)
            if not entry.admitted:
                self._skipped[entry.reason] += 1
                continue
```

- [ ] **Step 5: Replace the inline capital check with `screen_cost`**

Still in `run()`, find (currently ~lines 542-545):

```python
            cost = assessment.capital_at_risk
            if capital_in_use + cost > total_capital:
                self._skipped["insufficient_capital"] += 1
                continue
```

Replace with (reusing the `risk_state` built in Step 4 — `open_positions` has not changed between the two checkpoints, and `risk_state.capital_deployed` equals the running `capital_in_use` by the same invariant the code already relies on):

```python
            cost = assessment.capital_at_risk
            afford = screen_cost(cost, risk_state, self.risk_limits)
            if not afford.admitted:
                self._skipped[afford.reason] += 1
                continue
```

- [ ] **Step 6: Read the summary caps from `risk_limits`**

In the summary dict, find the `portfolio` block (currently ~lines 608-611):

```python
                "portfolio": {
                    "max_positions": self.max_positions,
                    "max_slots_per_underlying": self.max_slots_per_underlying,
                },
```

Replace with:

```python
                "portfolio": {
                    "max_positions": self.risk_limits.max_positions,
                    "max_slots_per_underlying": self.risk_limits.max_slots_per_underlying,
                },
```

- [ ] **Step 7: Build and pass `RiskLimits` in `src/tradetk/cli/backtest.py`**

Add the import near the other `from tradetk...` imports at the top of the file:

```python
from tradetk.risk import RiskLimits
```

Immediately after the `gate = GateLimits(...)` block ends (currently ~line 176, before the `from tradetk.config.schema import VaultOverlayConfig` line), insert:

```python
    risk = RiskLimits(
        max_positions=args.max_positions,
        max_slots_per_underlying=args.max_per_underlying,
        total_capital=Decimal(args.total_capital),
    )
```

Then in the `BacktestEngine(...)` call, replace these two lines:

```python
        max_positions=args.max_positions,
        max_slots_per_underlying=args.max_per_underlying,
```

with:

```python
        risk_limits=risk,
```

(`Decimal` and `args.total_capital` are already used a few lines above for `sizing`, so both are in scope.)

- [ ] **Step 8: Update the `engine()` test helper in `tests/test_backtest.py`**

The helper (currently ~lines 92-108) relies on the old constructor defaults (`max_positions=6`, `max_slots_per_underlying=2`) and the sizing `total_capital` of `D("20.00")`. Preserve those exact values by adding a `risk_limits` to the default `kwargs` dict. Add the import near the other `from tradetk...` imports at the top of the file:

```python
from tradetk.risk import RiskLimits
```

In the `kwargs = dict(...)` inside `engine()`, add this entry (put it right after the `sizing_limits=SizingLimits(...)` entry):

```python
        risk_limits=RiskLimits(
            max_positions=6, max_slots_per_underlying=2, total_capital=D("20.00"),
        ),
```

- [ ] **Step 9: Update the one override call site in `tests/test_backtest.py`**

Find (currently ~line 284):

```python
    result = engine(max_positions=2, max_slots_per_underlying=2).run(
```

Replace the `engine(...)` arguments so the override goes through `risk_limits`:

```python
    result = engine(risk_limits=RiskLimits(
        max_positions=2, max_slots_per_underlying=2, total_capital=D("20.00"),
    )).run(
```

(The `engine()` helper does `kwargs.update(overrides)`, so passing `risk_limits=` replaces the default built in Step 8.)

- [ ] **Step 10: Add one characterization test pinning the skip-counter names**

The extraction must not rename the skip reasons the calibration/reporting code reads. Append to `tests/test_backtest.py` (the `book`, `market`, `replay`, `engine`, `BookObservation`, `RiskLimits` names are already in scope from this module):

This mirrors the proven fixture in `test_slot_limit_is_enforced` (20 distinct
BTC contracts, which reliably fills the book), but caps the book at one slot so
the remaining eligible contracts are refused — and asserts the reason by name.
Append to `tests/test_backtest.py`:

```python
def test_book_level_skip_reasons_keep_their_names() -> None:
    """The extraction into risk/ must not rename the reasons downstream reports
    read. With one slot, a filled book records the refusals as `no_free_slot`."""
    observations = [
        BookObservation(f"KXBTCD-T{100000 + i}", T0 + dt.timedelta(minutes=i), book())
        for i in range(20)
    ]
    metadata = {
        f"KXBTCD-T{100000 + i}": [
            (T0, market(ticker=f"KXBTCD-T{100000 + i}", strike=str(100000 + i)))
        ]
        for i in range(20)
    }
    result = engine(
        risk_limits=RiskLimits(
            max_positions=1, max_slots_per_underlying=1, total_capital=D("20.00"),
        )
    ).run(replay(observations, metadata))
    assert result.skipped.get("no_free_slot", 0) >= 1
```

- [ ] **Step 11: Run the full suite — behaviour-identity is the acceptance test**

Run: `uv run --system-certs python -m pytest -q`
Expected: PASS, all tests (the prior count plus the new characterization test), output pristine. Any changed trade or skip count is a regression, not an accepted diff — investigate before proceeding.

- [ ] **Step 12: Lint**

Run: `uv run --system-certs ruff check src tests scripts`
Expected: `All checks passed!`

- [ ] **Step 13: Commit**

```bash
git add src/tradetk/backtest/engine.py src/tradetk/cli/backtest.py tests/test_backtest.py
git commit -m "$(cat <<'EOF'
Backtest consults the shared risk gate instead of inline checks

The slot, concentration, and capital checks now come from tradetk.risk: the
engine projects its open book to a RiskState and calls the same decision
functions the executor will. Behaviour is identical — same trades, same skip
counts — with the reasons and summary keys unchanged.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N4kWT7mUaXLnRBSrTw8uMa
EOF
)"
```

---

## Verification

After both tasks:

- `uv run --system-certs python -m pytest -q` is green, output pristine.
- `uv run --system-certs ruff check src tests scripts` passes.
- `git grep -n "self.max_positions\|self.max_slots_per_underlying" src/tradetk/backtest/engine.py` returns nothing — the loose attributes are gone.
- `git grep -n "screen_new_entry\|screen_cost" src/tradetk/backtest/engine.py` shows both wired at the two checkpoints.
- The `risk/` core has no import of `backtest`, `cli`, `overlay`, or `venues` — it is a leaf (`git grep -n "import" src/tradetk/risk` shows only stdlib + `tradetk.risk.*`).
- No halt logic exists (`git grep -ni "daily_loss\|drawdown\|staleness" src/tradetk/risk` returns nothing).
- The backtest summary JSON still carries `portfolio.max_positions` and `portfolio.max_slots_per_underlying`.
