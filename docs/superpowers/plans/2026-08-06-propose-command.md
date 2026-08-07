# Propose Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `propose` — the read-only half of the execution boundary: scan the live market, run the full overlay-aware pipeline, and write one auditable `proposals/<ts>-<ticker>.json` per admitted trade.

**Architecture:** Task 1 extracts the per-side assessment loop (now needed by three consumers) from `BacktestEngine._best_assessment` into a pure `translation/assessment.py::assess_candidate`, with the engine and paper's `choose_side` becoming thin wrappers — proven behaviour-identical by the untouched backtest suite (the oracle) and paper's existing cross-check test. Task 2 builds the proposal artifact: config fingerprint, proposal dict builder, no-overwrite writer. Task 3 wires the `propose` CLI: project the live ledger (empty today) → scan live read-only → halt gate → evaluate ranked-by-edge capped-at-free-slots → write files + full "why not" summary.

**Tech Stack:** Python 3.12 (`uv`), Pydantic config, `Decimal` money, pytest, ruff (line-length 100).

## Global Constraints

- **Money is `Decimal`, never float.** Serialize as `str(...)` in JSON.
- **All commands run under `uv run --system-certs`** (corporate MITM CA). Never `--native-tls`.
- **No order endpoint** anywhere in `cli/propose.py`'s import graph; venue and provider access strictly read-only. The live ledger `data/live/ledger.jsonl` is **read-only** to propose.
- **Behaviour identity for the extraction:** the full backtest suite passes **untouched** (same trades, same skip-counter names and counts), and paper's `test_choose_side_matches_engine_best_assessment` passes untouched.
- **`translation/` stays leaf-ward:** `translation/assessment.py` must NOT import from `overlay/`, `backtest/`, or `cli/` — the overlay parameter is duck-typed (`Any`).
- **`propose` reads limits only from config** (`from_config` constructors); CLI flags are I/O and selection only.
- **ruff line-length 100** — self-check new lines (repo ruff does not enforce E501); do not change the ruff config.
- **ruff clean + full suite green at the end of every task:** `uv run --system-certs pytest -q` and `uv run --system-certs ruff check src/ tests/`.
- One commit per task, message style `Propose: <summary>`.

---

### Task 1: Extract the shared assessment loop

Three consumers (engine overlay-on, paper overlay-off, propose overlay-on) — extract once. The moved code is `BacktestEngine._best_assessment` (`src/tradetk/backtest/engine.py:355-411`) **verbatim**, minus `self._skipped` increments, which become returned reason strings the engine folds — preserving its counters exactly.

**Files:**
- Create: `src/tradetk/translation/assessment.py`
- Modify: `src/tradetk/backtest/engine.py` (`_best_assessment` becomes a wrapper)
- Modify: `src/tradetk/cli/paper.py` (`choose_side` becomes a wrapper)
- Tests: NONE new — the untouched backtest suite + paper's cross-check test ARE the proof. Zero test-file edits allowed in this task.

**Interfaces:**
- Consumes: `plan_size`, `assess_side`, `side_depth`, `GateLimits`, `SizingLimits`, `EdgeAssessment` from `translation/`; `Side` from `venues.base`.
- Produces (Tasks 2–3 and the engine/paper rely on these exact names):
  - `CandidateOutcome(assessment: EdgeAssessment | None, binding_cap: str, skips: tuple[str, ...])` (frozen dataclass).
  - `assess_candidate(claim, estimate, book, when, capital_in_use, *, gate_limits, sizing_limits, fee_model, overlay=None) -> CandidateOutcome`.

- [ ] **Step 1: Read the source of truth**

Read `src/tradetk/backtest/engine.py` lines 340–415 (the `_best_assessment` method and its imports) and `src/tradetk/cli/paper.py` lines 60–95 (`choose_side`). The extraction must reproduce the engine's loop **exactly** — side order, price source, `plan_size`/`assess_side` arguments, best-by-`net_edge_pp` selection, and the skip-reason strings (`overlay_blocked`, `overlay_side_forbidden`, `unsizeable_{cap}`, `gate_{name}`) in the exact order the engine emits them.

- [ ] **Step 2: Create `translation/assessment.py`**

```python
# src/tradetk/translation/assessment.py
"""The per-side assessment loop, shared by backtest, paper, and propose.

Size each side against its own book, gate it at that size, keep the better
passing side. Extracted from ``BacktestEngine._best_assessment`` when propose
became the third consumer; the backtest suite is the behaviour oracle.

Pure: instead of incrementing a skip counter, the loop returns the ordered
tuple of skip-reason strings it produced, and each caller folds them into its
own accounting. ``overlay`` is duck-typed (``for_underlying(underlying, when)``
returning a policy with ``blocked``/``gate_limits``/``sizing_limits``/
``allowed_sides``) so this module never imports ``overlay/`` and stays a leaf.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from tradetk.translation.edge import EdgeAssessment, GateLimits, assess_side, side_depth
from tradetk.translation.sizing import SizingLimits, plan_size
from tradetk.venues.base import BinaryBook, Side


@dataclass(frozen=True)
class CandidateOutcome:
    """What the loop decided, and every reason it said no along the way."""

    assessment: EdgeAssessment | None
    binding_cap: str
    skips: tuple[str, ...]


def assess_candidate(
    claim: Any, estimate: Any, book: BinaryBook, when: datetime,
    capital_in_use: Decimal, *,
    gate_limits: GateLimits, sizing_limits: SizingLimits, fee_model: Any,
    overlay: Any = None,
) -> CandidateOutcome:
    """Size and assess each side; return the better passing one.

    Sizing has to happen per side because the two sides trade at different
    prices, and the contract count depends on the price. So each side is
    sized against its own book, then gated at that size — never sized at one
    price and gated at another.

    When a vault overlay is present, its verdict is resolved once for this
    claim and then *narrows* the loop: a blocked underlying assesses no
    side, a bias forbids the side that contradicts it, risk shrinks the
    sizing target, and a catalyst raises the gate. With no overlay every
    value below is exactly the global limit, so the behaviour is unchanged.
    """
    skips: list[str] = []
    allowed: tuple[Side, ...] | None = None
    if overlay is not None:
        policy = overlay.for_underlying(claim.underlying, when)
        if policy.blocked:
            skips.append("overlay_blocked")
            return CandidateOutcome(None, "none", tuple(skips))
        gate_limits = policy.gate_limits
        sizing_limits = policy.sizing_limits
        allowed = policy.allowed_sides(claim)

    best: EdgeAssessment | None = None
    best_cap = "none"
    for side in (Side.yes, Side.no):
        if allowed is not None and side not in allowed:
            skips.append("overlay_side_forbidden")
            continue
        price = book.best_yes_ask if side is Side.yes else book.best_no_ask
        if price is None:
            continue
        depth = side_depth(book, side)
        plan = plan_size(
            price, fee_model, sizing_limits,
            book_depth=depth, capital_in_use=capital_in_use,
        )
        if not plan.tradeable:
            skips.append(f"unsizeable_{plan.binding_cap.value}")
            continue
        assessment = assess_side(
            claim, estimate, book, side=side, contracts=plan.contracts,
            fee_model=fee_model, limits=gate_limits, now=when,
        )
        if not assessment.passed:
            for failure in assessment.failures:
                skips.append(f"gate_{failure.gate.value}")
            continue
        if best is None or assessment.net_edge_pp > best.net_edge_pp:
            best, best_cap = assessment, plan.binding_cap.value
    return CandidateOutcome(best, best_cap, tuple(skips))
```

**Fidelity check before moving on:** open the engine's `_best_assessment` beside this and verify line-by-line that the only differences are (a) `self.gate_limits`/`self.sizing_limits`/`self.overlay`/`self.fee_model` became parameters, and (b) each `self._skipped[X] += 1` became `skips.append(X)` at the same position. Any other difference is a bug.

- [ ] **Step 3: Make the engine a wrapper**

Replace the body of `BacktestEngine._best_assessment` (keep the method, its signature, and its docstring reference) with:

```python
    def _best_assessment(
        self, claim: Claim, opinion_estimate, book: BinaryBook, when: datetime,
        capital_in_use: Decimal,
    ) -> tuple[EdgeAssessment | None, str]:
        """Size and assess each side via the shared loop; fold its skip reasons.

        The loop itself lives in ``translation/assessment.py`` (shared with
        paper and propose); this wrapper only supplies the engine's limits and
        folds the returned skip reasons into ``self._skipped``, which keeps the
        counters byte-for-byte identical to the pre-extraction engine.
        """
        outcome = assess_candidate(
            claim, opinion_estimate, book, when, capital_in_use,
            gate_limits=self.gate_limits, sizing_limits=self.sizing_limits,
            fee_model=self.fee_model, overlay=self.overlay,
        )
        for reason in outcome.skips:
            self._skipped[reason] += 1
        return outcome.assessment, outcome.binding_cap
```

Add `from tradetk.translation.assessment import assess_candidate` to the engine's imports. Remove imports that become unused (likely `plan_size`, `side_depth`, `assess_side` — verify with ruff, which flags unused imports as F401).

- [ ] **Step 4: Make paper's `choose_side` a wrapper**

Replace the body of `choose_side` in `src/tradetk/cli/paper.py` (keep name, signature, and docstring — the cross-check test imports it):

```python
def choose_side(
    claim, estimate, book: BinaryBook, when: datetime, capital_in_use: Decimal, *,
    gate_limits: GateLimits, sizing_limits: SizingLimits, fee_model: KalshiFeeModel,
) -> tuple[EdgeAssessment | None, str]:
    """The overlay-off entry to the shared assessment loop.

    A cross-check test asserts this returns the same choice as the engine for the
    same inputs, so the two cannot silently disagree on a decision.
    """
    outcome = assess_candidate(
        claim, estimate, book, when, capital_in_use,
        gate_limits=gate_limits, sizing_limits=sizing_limits,
        fee_model=fee_model, overlay=None,
    )
    return outcome.assessment, outcome.binding_cap
```

Add the `assess_candidate` import; remove now-unused imports (`plan_size`, `side_depth`, `assess_side` — verify with ruff).

- [ ] **Step 5: Run the oracle**

Run: `uv run --system-certs pytest -q`
Expected: **490 passed — the same 490, zero test edits.** Any failure means the extraction is not behaviour-identical: fix the extraction, never the tests.

Run: `uv run --system-certs ruff check src/tradetk/translation/assessment.py src/tradetk/backtest/engine.py src/tradetk/cli/paper.py`
Expected: clean (this also catches leftover unused imports).

- [ ] **Step 6: Verify the leaf constraint, then commit**

Run: `uv run --system-certs python -c "import ast; mods=[n.module for n in ast.walk(ast.parse(open('src/tradetk/translation/assessment.py').read())) if isinstance(n, ast.ImportFrom)]; bad=[m for m in mods if m and m.startswith('tradetk') and not (m.startswith('tradetk.translation') or m=='tradetk.venues.base')]; print('LEAK:', bad) if bad else print('clean')"`
Expected: `clean`.

```bash
git add src/tradetk/translation/assessment.py src/tradetk/backtest/engine.py src/tradetk/cli/paper.py
git commit -m "Propose: extract the shared per-side assessment loop (behaviour-identical)"
```

---

### Task 2: The proposal artifact — fingerprint, builder, writer

**Files:**
- Create: `src/tradetk/proposals.py`
- Test: `tests/test_proposals.py`

**Interfaces:**
- Consumes: `EdgeAssessment.as_dict()` (exists, `translation/edge.py:154`); `Claim` (pydantic model — `claim.model_dump(mode="json")`); `PaperBook` from `state/ledger.py` (for the risk snapshot); `RiskDecision` (halt check).
- Produces (Task 3 relies on these exact names):
  - `config_fingerprint(config) -> str` — `"sha256:<hex>"` over the decision-shaping config sections.
  - `build_proposal(*, claim, assessment, book_state, halt, overlay_verdict, candle_age_seconds, strategy_name, vol_lookback_days, created_at) -> dict`.
  - `write_proposal(proposals_dir, proposal, *, created_at, ticker) -> Path` — writes `<UTCts>-<safe-ticker>.json`, **refuses to overwrite** (raises `FileExistsError`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_proposals.py
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tradetk.proposals import build_proposal, config_fingerprint, write_proposal

D = Decimal
NOW = datetime(2026, 8, 6, 21, 40, 55, tzinfo=timezone.utc)


class _Cfg:
    """Duck-typed config: sections are pydantic-like with model_dump()."""

    class _Section:
        def __init__(self, **kw):
            self._kw = kw

        def model_dump(self, mode="json"):
            return dict(self._kw)

    def __init__(self, edge=3.0):
        s = self._Section
        self.capital = s(total_capital=20.0, max_positions=6)
        self.edge_gate = s(min_net_edge_pp=edge, margin_pp=1.0)
        self.liquidity = s(min_book_depth_multiple=5.0)
        self.horizon = s(max_hours_to_resolution=168.0)
        self.risk = s(max_daily_loss_dollars=5.0)
        self.orders = s(prefer_maker=True)
        self.venue = s(name="kalshi", environment="demo")
        self.fees = s(maker_fee=0.0)
        self.strategy = s(name="baseline_vol")


def test_fingerprint_is_stable_and_sensitive():
    a, b = config_fingerprint(_Cfg()), config_fingerprint(_Cfg())
    assert a == b and a.startswith("sha256:")
    assert config_fingerprint(_Cfg(edge=4.0)) != a


def test_write_refuses_to_overwrite(tmp_path):
    p = {"schema_version": 1}
    first = write_proposal(tmp_path, p, created_at=NOW, ticker="KXBTC-T99")
    assert first.exists()
    with pytest.raises(FileExistsError):
        write_proposal(tmp_path, p, created_at=NOW, ticker="KXBTC-T99")


def test_filename_is_utc_stamp_and_ticker(tmp_path):
    path = write_proposal(tmp_path, {}, created_at=NOW, ticker="KXETH-06AUG-T3599.99")
    assert path.name == "20260806T214055Z-KXETH-06AUG-T3599.99.json"


def test_money_survives_the_round_trip(tmp_path, proposal_fixture):
    proposal = proposal_fixture  # built via build_proposal with Decimal inputs
    path = write_proposal(tmp_path, proposal, created_at=NOW, ticker="T")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["schema_version"] == 1
    assert isinstance(loaded["decision"]["capital_at_risk"], str)
    assert D(loaded["decision"]["capital_at_risk"]) == D("1.68")
    assert loaded["created_at"] == "2026-08-06T21:40:55+00:00"
```

Add a `proposal_fixture` in the same file: call `build_proposal` with a real `EdgeAssessment` and its `BinaryBook` built the same way `tests/test_paper_cli.py`'s `engine_case` fixture builds them (reuse those builders from `tests/conftest.py` — read it first; pass the book as `book=`), a `PaperBook`-like state (`state/ledger.py`'s `project([])` on an empty event list is the cheapest real one), `halt=RiskDecision(admitted=True, reason=None)`, `overlay_verdict={"ok": False, "note": "no overlay"}`, `candle_age_seconds=D("60")`. Assert the built dict contains the keys: `schema_version, created_at, strategy, claim, decision, book, risk, halt, overlay, config_fingerprint` — with `config_fingerprint` passed in by the caller (Task 3 computes it once per run and hands it to `build_proposal` — add it as a keyword arg).

- [ ] **Step 2: Run to verify failure**

Run: `uv run --system-certs pytest tests/test_proposals.py -v`
Expected: FAIL — no module `tradetk.proposals`.

- [ ] **Step 3: Implement `src/tradetk/proposals.py`**

```python
# src/tradetk/proposals.py
"""The proposal artifact: what `propose` writes and `execute` will consume.

A proposal is FACTS, not judgment: the claim, the decision with its full cost
breakdown, the book as it stood, and the config fingerprint that shaped it.
Whether it is still valid later is `execute`'s re-validation policy (step 17),
deliberately not encoded here.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

#: The config sections that shape a trading decision. venue is included so a
#: demo-minted proposal can never be executed against prod unnoticed.
_FINGERPRINT_SECTIONS = (
    "capital", "edge_gate", "liquidity", "horizon",
    "risk", "orders", "venue", "fees", "strategy",
)


def config_fingerprint(config: Any) -> str:
    """sha256 over the canonical JSON of the decision-shaping config sections."""
    payload = {
        name: getattr(config, name).model_dump(mode="json")
        for name in _FINGERPRINT_SECTIONS
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _book_view(book: Any) -> dict[str, Any]:
    """Top-of-book facts execute will re-validate against."""
    from tradetk.venues.base import Side

    return {
        "best_yes_bid": str(book.best_yes_bid) if book.best_yes_bid is not None else None,
        "best_yes_ask": str(book.best_yes_ask) if book.best_yes_ask is not None else None,
        "best_no_bid": str(book.best_no_bid) if book.best_no_bid is not None else None,
        "best_no_ask": str(book.best_no_ask) if book.best_no_ask is not None else None,
        "yes_depth": str(book.depth(Side.yes)),
        "no_depth": str(book.depth(Side.no)),
        "retrieved_at": book.retrieved_at.isoformat() if book.retrieved_at else None,
    }


def build_proposal(
    *,
    claim: Any,
    assessment: Any,
    book: Any,
    book_state: Any,
    halt: Any,
    overlay_verdict: dict[str, Any],
    candle_age_seconds: Decimal,
    strategy_name: str,
    vol_lookback_days: int,
    created_at: datetime,
    config_fingerprint: str,
) -> dict[str, Any]:
    """Assemble the full decision trace for one admitted trade."""
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": created_at.isoformat(),
        "strategy": {"name": strategy_name, "vol_lookback_days": vol_lookback_days},
        "claim": claim.model_dump(mode="json"),
        "decision": assessment.as_dict(),
        "book": _book_view(book),
        "signals": {"candle_age_seconds": str(candle_age_seconds)},
        "risk": {
            "slots_used": book_state.risk_state().slots_used,
            "capital_deployed": str(book_state.capital_deployed),
            "realized_today": str(book_state.realized_today),
            "drawdown": str(book_state.drawdown),
            "halt": {"admitted": halt.admitted, "reason": halt.reason},
        },
        "overlay": overlay_verdict,
        "config_fingerprint": config_fingerprint,
    }


def write_proposal(
    proposals_dir: str | Path, proposal: dict[str, Any], *,
    created_at: datetime, ticker: str,
) -> Path:
    """Write one proposal file; refuse to replace one a human may be reading."""
    stamp = created_at.strftime("%Y%m%dT%H%M%SZ")
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in ticker)
    path = Path(proposals_dir) / f"{stamp}-{safe}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"proposal already exists, refusing to overwrite: {path}")
    path.write_text(json.dumps(proposal, indent=2, default=str), encoding="utf-8")
    return path
```

Implementer note on `_book_view`: verify `BinaryBook.retrieved_at` exists (it's set by `parse_orderbook`); guard with `getattr(book, "retrieved_at", None)` if optional on the model. Do not invent fields — every value in the file must come from a real object.

- [ ] **Step 4: Run to verify pass, ruff, commit**

Run: `uv run --system-certs pytest tests/test_proposals.py -v` → all pass.
Run: `uv run --system-certs ruff check src/tradetk/proposals.py tests/test_proposals.py` → clean.

```bash
git add src/tradetk/proposals.py tests/test_proposals.py
git commit -m "Propose: the proposal artifact — fingerprint, builder, no-overwrite writer"
```

---

### Task 3: The `propose` CLI — scan, gate, rank, write

**Files:**
- Create: `src/tradetk/cli/propose.py`
- Modify: `.gitignore` (ignore `proposals/`)
- Modify: `README.md` (tick step 16, Status header → step 17 next)
- Test: `tests/test_propose_cli.py` (+ minimal additions to `tests/conftest.py` if a knob is missing)

**Interfaces:**
- Consumes: `assess_candidate` (Task 1); `build_proposal`, `write_proposal`, `config_fingerprint` (Task 2); `read_ledger`, `project` (state/ledger); `BookHealth`, `HaltLimits`, `RiskLimits`, `OpenRisk`, `RiskState`, `screen_halts`, `screen_new_entry`, `screen_cost` (risk); `parse_claim`/`parse_claims` (translation.claims); `crypto_series`, `eligible_markets` (venues.books); `venue.orderbook(ticker)`; `load_underlying_data` (cli.backtest); `_data_age` (cli.paper — same-package reuse); `load_overlay` + `build_registry` (overlay, loaded exactly as `cli/backtest.py:184-205` does); `get_strategy`, `StrategyContext`.
- Produces: `run_propose(*, config, registry, ledger_path, proposals_dir, markets, books, data, overlay, strategy, now, vol_lookback_days, data_age_seconds=None) -> dict` (testable core, live I/O injected); `main(argv) -> int`.

- [ ] **Step 1: Write the failing tests**

Read `tests/conftest.py` first — reuse its claim/book/config builders. Tests (fake objects, no network):

```python
# tests/test_propose_cli.py
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tradetk.cli.propose import run_propose
from tradetk.state.ledger import fill_event, append_events

D = Decimal
NOW = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)

# A `propose_env` fixture (this file or conftest) assembles: a real config
# (config.example.yaml pattern), registry, one-or-two VenueMarket objects whose
# claims parse, matching BinaryBook dicts, a data/snapshot stub, and a stub
# strategy returning a fixed non-abstaining estimate — the same recipe as
# paper_env, but handing run_propose `markets`/`books` directly.


def test_clean_run_writes_one_valid_proposal(propose_env, tmp_path):
    env = propose_env(proposals_dir=tmp_path / "proposals")
    summary = run_propose(**env, now=NOW)
    assert summary["halted"] is None
    assert len(summary["proposed"]) == 1
    path = summary["proposed"][0]["file"]
    doc = json.loads(open(path, encoding="utf-8").read())
    assert doc["schema_version"] == 1
    assert doc["config_fingerprint"].startswith("sha256:")
    assert isinstance(doc["decision"]["capital_at_risk"], str)


def test_halted_run_writes_nothing(propose_env, tmp_path):
    env = propose_env(proposals_dir=tmp_path / "p", data_age_seconds=D("999999"))
    summary = run_propose(**env, now=NOW)
    assert summary["halted"] == "stale_data_halt"
    assert summary["proposed"] == []
    assert not list((tmp_path / "p").glob("*.json"))


def test_slot_cap_admits_best_edge_first(propose_env, tmp_path):
    # two passing candidates, but the live ledger already holds max_positions-1
    # positions → exactly ONE file, and it is the higher-net-edge candidate.
    env = propose_env(proposals_dir=tmp_path / "p", two_candidates=True,
                      prefill_open=5)  # config max_positions=6
    summary = run_propose(**env, now=NOW)
    assert len(summary["proposed"]) == 1
    assert summary["skips"].get("no_free_slot", 0) >= 1


def test_blocked_overlay_underlying_yields_no_file(propose_env, tmp_path):
    class _BlockedPolicy:
        blocked = True
    class _Overlay:
        ok = True
        def for_underlying(self, underlying, now):
            return _BlockedPolicy()
        def as_dict(self):
            return {"ok": True}
    env = propose_env(proposals_dir=tmp_path / "p", overlay=_Overlay())
    summary = run_propose(**env, now=NOW)
    assert summary["proposed"] == []
    assert summary["skips"].get("overlay_blocked", 0) >= 1


def test_live_ledger_is_never_written(propose_env, tmp_path):
    ledger = tmp_path / "live.jsonl"
    env = propose_env(proposals_dir=tmp_path / "p", ledger_path=ledger)
    run_propose(**env, now=NOW)
    assert not ledger.exists()  # propose reads it; only execute may append
```

`propose_env(prefill_open=N)` seeds the live ledger with N open `fill_event`s (distinct tickers/underlyings so concentration doesn't bind first — or bind deliberately and assert that reason instead; pick one and assert exactly).

- [ ] **Step 2: Run to verify failure**

Run: `uv run --system-certs pytest tests/test_propose_cli.py -v`
Expected: FAIL — no module `tradetk.cli.propose`.

- [ ] **Step 3: Implement `cli/propose.py`**

```python
# src/tradetk/cli/propose.py
"""``propose`` — scan live, run the full gate stack, write proposal files.

The read-only half of the execution boundary. Never contacts the order
endpoint; the live ledger is projected read-only (only ``execute`` appends).
One file per admitted trade: one file = one order = one typed confirmation.

Phases: **load** the live ledger (empty until step 17) → **scan** live markets,
books, and fresh candles (read-only) → **halt** gate once → **evaluate** every
candidate through the shared overlay-aware assessment, rank passing candidates
by net edge, admit greedily against the rolling risk state → **write** one
proposal per admitted trade plus a full why-not summary.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import truststore

from tradetk.cli.backtest import load_underlying_data
from tradetk.cli.paper import _data_age
from tradetk.config.loader import load_config
from tradetk.costs.fees import FeeRounding, KalshiFeeModel
from tradetk.proposals import build_proposal, config_fingerprint, write_proposal
from tradetk.risk import (
    BookHealth, HaltLimits, OpenRisk, RiskLimits, RiskState,
    screen_cost, screen_halts, screen_new_entry,
)
from tradetk.signals.hyperliquid import HyperliquidProvider
from tradetk.state.ledger import project, read_ledger
from tradetk.strategy import StrategyContext, get_strategy
from tradetk.translation.assessment import assess_candidate
from tradetk.translation.claims import ClaimParseError, UnderlyingRegistry, parse_claim
from tradetk.translation.edge import GateLimits
from tradetk.translation.sizing import SizingLimits
from tradetk.venues.books import crypto_series, eligible_markets
from tradetk.venues.kalshi import KalshiVenue

log = logging.getLogger("tradetk.cli.propose")


def run_propose(
    *,
    config: Any,
    registry: UnderlyingRegistry,
    ledger_path: str | Path,
    proposals_dir: str | Path,
    markets: list[Any],
    books: dict[str, Any],
    data: Any,
    overlay: Any,
    strategy: Any,
    now: datetime,
    vol_lookback_days: int = 30,
    data_age_seconds: Decimal | int | str | None = None,
) -> dict[str, Any]:
    risk_limits = RiskLimits.from_config(config)
    halt_limits = HaltLimits.from_config(config)
    gate_limits = GateLimits.from_config(config)
    sizing_limits = SizingLimits.from_config(config)
    fee_model = KalshiFeeModel(rounding=FeeRounding.cent)
    fingerprint = config_fingerprint(config)

    skips: Counter[str] = Counter()
    summary: dict[str, Any] = {
        "halted": None, "proposed": [], "skips": skips, "errors": [], "overlay": None,
    }
    summary["overlay"] = overlay.as_dict() if overlay is not None else {"ok": False}

    # -- phase 1: load (read-only — only execute may append) -------------
    book_state = project(
        read_ledger(ledger_path),
        starting_capital=Decimal(str(config.capital.total_capital)), today=now.date(),
    )

    # -- phase 3 (2 = scan happened in the caller): halt gate, once ------
    age = (
        Decimal(str(data_age_seconds)) if data_age_seconds is not None
        else _data_age(data, now)
    )
    health = BookHealth(
        realized_today=book_state.realized_today, drawdown=book_state.drawdown,
        data_age_seconds=age, drawdown_latched=book_state.drawdown_latched,
    )
    halt = screen_halts(health, halt_limits)
    if not halt.admitted:
        summary["halted"] = halt.reason
        summary["skips"] = dict(skips)
        return summary

    # -- phase 4: evaluate every candidate, then admit best-edge-first ---
    passing: list[tuple[Any, Any, Any]] = []  # (claim, assessment, book)
    for market in markets:
        try:
            claim = parse_claim(market, registry)
        except ClaimParseError:
            skips["no_parseable_claim"] += 1
            continue
        try:
            if claim.resolution_time <= now:
                skips["already_resolved"] += 1
                continue
            book = books.get(claim.ticker)
            if book is None:
                skips["no_book"] += 1
                continue
            snapshot = data.snapshot_at(claim.underlying, now, lookback_days=vol_lookback_days)
            if snapshot is None:
                skips["no_underlying_data"] += 1
                continue
            opinion = strategy.estimate(
                claim, StrategyContext(now=now, snapshot=snapshot, book=book)
            )
            if opinion.abstained:
                skips["strategy_abstained"] += 1
                continue
            outcome = assess_candidate(
                claim, opinion.estimate, book, now, book_state.capital_deployed,
                gate_limits=gate_limits, sizing_limits=sizing_limits,
                fee_model=fee_model, overlay=overlay,
            )
            for reason in outcome.skips:
                skips[reason] += 1
            if outcome.assessment is not None:
                passing.append((claim, outcome.assessment, book))
        except Exception as exc:  # noqa: BLE001 - one bad candidate must not kill the run
            summary["errors"].append(f"evaluate {market.ticker}: {exc}")
            continue

    passing.sort(key=lambda entry: entry[1].net_edge_pp, reverse=True)
    risk_state = book_state.risk_state()
    capital_in_use = book_state.capital_deployed
    for claim, assessment, book in passing:
        entry = screen_new_entry(claim.underlying, risk_state, risk_limits)
        if not entry.admitted:
            skips[entry.reason] += 1
            continue
        afford = screen_cost(assessment.capital_at_risk, risk_state, risk_limits)
        if not afford.admitted:
            skips[afford.reason] += 1
            continue
        overlay_verdict = (
            overlay.for_underlying(claim.underlying, now).as_dict()
            if overlay is not None and getattr(overlay, "ok", False)
            else {"ok": False, "note": "no overlay"}
        )
        proposal = build_proposal(
            claim=claim, assessment=assessment, book=book, book_state=book_state, halt=halt,
            overlay_verdict=overlay_verdict, candle_age_seconds=age,
            strategy_name=strategy.name, vol_lookback_days=vol_lookback_days,
            created_at=now, config_fingerprint=fingerprint,
        )
        try:
            path = write_proposal(proposals_dir, proposal, created_at=now, ticker=claim.ticker)
        except FileExistsError as exc:
            summary["errors"].append(str(exc))
            continue
        summary["proposed"].append({
            "file": str(path), "ticker": claim.ticker, "side": assessment.side.value,
            "contracts": assessment.contracts_requested,
            "capital_at_risk": str(assessment.capital_at_risk),
            "net_edge_pp": str(assessment.net_edge_pp),
        })
        risk_state = RiskState(
            open=risk_state.open + (OpenRisk(claim.ticker, claim.underlying,
                                             assessment.capital_at_risk),)
        )
        capital_in_use += assessment.capital_at_risk

    summary["skips"] = dict(skips)
    return summary
```

Then `main(argv)`: args `--config`, `--registry`, `--ledger` (default `data/live/ledger.jsonl`), `--proposals-dir` (default `None` → `config.paths.proposals_dir`), `--strategy` (default `baseline_vol`), `--vol-lookback-days` (default 30), `--pretty`. Body mirrors `cli/paper.py`'s `main`: basicConfig + truststore; load config/registry/strategy; `now = datetime.now(timezone.utc)`; open `HyperliquidProvider` + `KalshiVenue(environment=config.venue.environment.value)`; scan: `series = crypto_series(venue)` → `eligible_markets(venue, [s["ticker"] for s in series], max_hours_to_close=float(config.horizon.max_hours_to_resolution), now=now)`; per market fetch `books[m.ticker] = venue.orderbook(m.ticker)` (per-ticker try/except → `errors`, continue); `symbols` from parsed claims' underlyings (parse errors counted, not fatal); `data = load_underlying_data(provider, symbols, start=now, end=now, lookback_days=args.vol_lookback_days)`; overlay loaded exactly as `cli/backtest.py:184-205` (broken toolkit config → WARNING + `VaultOverlayConfig()`; `load_overlay(vault_cfg, base_gate=gate_limits, base_sizing=sizing_limits, registry=build_registry(), as_of=now, now=now)`; not-ok overlay → stderr warning, proceed passing the overlay object — `VaultOverlay.for_underlying` returns base policy when not ok, same as backtest); call `run_propose`; print summary JSON; exit 0 normally (including halted), exit 2 if the venue scan itself failed wholesale (no series and no markets).

Implementer notes: verify `crypto_series` returns dicts with a `"ticker"` key (read `venues/books.py:35-47` and the series payload usage in `cli/record.py` — mirror record's series→tickers path); verify `venue.orderbook(ticker)` signature; verify `strategy.name` exists on `BaseStrategy`; `load_underlying_data` with `start == end == now` fetches `lookback+1` days back, which is exactly what the estimate needs — confirm against `cli/backtest.py:50-79`.

- [ ] **Step 4: Wire `.gitignore` and README**

Add to `.gitignore` (transient local artifacts, consumed by execute — NOT committed evidence, unlike `data/paper/`):
```
proposals/
```
Verify: `git check-ignore proposals/x.json; echo "exit=$?"` → prints the path, exit 0 (ignored).
README: tick `- [ ] 16.` → `- [x] 16.` with a one-line summary in the established style; update the `## Status:` header line to point at step 17.

- [ ] **Step 5: Run all tests, ruff, the no-order-path check, then commit**

Run: `uv run --system-certs pytest tests/test_propose_cli.py tests/test_proposals.py -v` → all pass.
Run: `uv run --system-certs pytest -q` → full suite green.
Run: `uv run --system-certs ruff check src/ tests/` → clean.
Run: `uv run --system-certs python -c "import tradetk.cli.propose, sys; mods=[m for m in sys.modules if m.startswith('tradetk')]; bad=[m for m in mods if 'execute' in m or m.endswith('.orders')]; print('BAD:', bad) if bad else print('clean')"` → `clean`.

```bash
git add src/tradetk/cli/propose.py tests/test_propose_cli.py tests/conftest.py .gitignore README.md
git commit -m "Propose: the propose command — scan, gate, rank, write one file per trade"
```

---

## Self-Review

**Spec coverage:**
- One file per trade + no-overwrite → Task 2 (`write_proposal`), Task 3 (loop writes per admitted candidate).
- Live-ledger seam, read-only → Task 3 phase 1 (`read_ledger`/`project` only; `test_live_ledger_is_never_written`).
- Overlay ON via shared loop → Task 1 (`assess_candidate(overlay=...)`), Task 3 passes the loaded overlay; blocked-underlying test.
- The extraction, oracle-guarded, skip counters preserved → Task 1 (returned-reasons fold; zero test edits; leaf check).
- Phases (load → scan → halt → evaluate → write); scan-before-halt → Task 3 (`main` scans, `run_propose` receives `data`; halt uses `_data_age`).
- Ranked by net edge, capped at free slots, rolling state → Task 3 (sort + greedy admit; slot-cap test).
- Proposal contents incl. config fingerprint, Decimal-as-strings → Task 2 (builder + fingerprint + round-trip test).
- Config as source of truth, flags I/O-only → Task 3 (`from_config` everywhere; flags list).
- Error handling (per-candidate skip; wholesale failure exit 2; halt exit 0; broken overlay warn-and-proceed) → Task 3.
- `proposals/` gitignored → Task 3 Step 4.
- Halted run writes zero files → Task 3 (`test_halted_run_writes_nothing`).

**Placeholder scan:** the `propose_env` fixture body is described, not fully coded — deliberate, same guided-scaffolding pattern as step 15's `paper_env` (which the implementer assembles from real builders; explicit note included). Two implementer-verify notes (assessment `book` attribute, `crypto_series` ticker key) direct verification against named files rather than leaving gaps. No TODO/TBD anywhere.

**Type consistency:** `CandidateOutcome.assessment/binding_cap/skips` used identically in Tasks 1 and 3; `build_proposal`/`write_proposal`/`config_fingerprint` signatures match between Tasks 2 and 3 (including the `config_fingerprint` kwarg added in Task 2 Step 1's fixture note and present in Task 2 Step 3 and Task 3's call); `run_propose` summary keys (`halted/proposed/skips/errors/overlay`) match the tests.
