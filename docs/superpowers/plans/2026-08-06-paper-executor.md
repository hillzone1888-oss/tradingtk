# Paper Executor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the paper executor — the first live-forward loop — that runs the exact live decision path, fills in simulation, persists positions to an event-sourced ledger, settles from the venue's real resolution, and enforces the daily-loss / drawdown / staleness halts.

**Architecture:** A per-poll orchestrator (`cli/paper.py`) inside the existing `sweep` routine, after `record`+`shadow`. It folds an append-only JSONL ledger into the current book (a projection, never a mutated file), settles open positions from a read-only venue call, runs the pure halt gate once, then evaluates the freshest recorded book through the *same* shared pieces the backtest uses (strategy → edge gate → sizing → risk gate) and walks the book to fill. Two new pure leaves — `risk/halts.py` and `state/ledger.py` — carry all the decision and projection logic; the orchestrator only wires them.

**Tech Stack:** Python 3.12 (`uv`), Pydantic config, `Decimal` money, pytest, ruff (line-length 100).

## Global Constraints

- **Money is `Decimal`, never float.** Serialize as `str(...)` in JSON.
- **All commands run under `uv run --system-certs`** (corporate MITM CA). `--native-tls` is deprecated — do not use.
- **No order endpoint** may appear anywhere in `cli/paper.py`'s import graph. Venue and provider access is strictly read-only.
- **`risk/` is a leaf** — new `risk/halts.py` imports only stdlib and within-`risk` modules. No imports from `backtest`, `cli`, `overlay`, `venues`, `translation`.
- **ruff clean, full suite green** at the end of every task: `uv run --system-certs pytest -q` and `uv run --system-certs ruff check src/ tests/`.
- **Frequent commits** — one per task, message style `Paper executor: <summary>` (no `Step N:` prefix; that scheme is per-build-step, and this whole plan is build step 15).
- **UTC everywhere** — `realized_today` and the daily-loss reset use the UTC calendar day, matching `shadow`'s `date=` partitioning.

---

### Task 1: `VenueMarket.result` — the settled outcome (read-only)

Settlement needs the venue's resolved yes/no. `VenueMarket` exposes `status` but not the outcome; Kalshi's payload carries `result`, `parse_market` just drops it. Add it. Touches no order path.

**Files:**
- Modify: `src/tradetk/venues/base.py` (add field to `VenueMarket`, ~line 207)
- Modify: `src/tradetk/venues/kalshi.py` (map it in `parse_market`, ~line 113)
- Test: `tests/test_venue_kalshi.py` (add cases; create if absent)

**Interfaces:**
- Produces: `VenueMarket.result: str | None` — `"yes"`, `"no"`, or `None`/`""` when unresolved. Consumed by Task 3's `settle_position`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_venue_kalshi.py
from tradetk.venues.kalshi import parse_market

def test_parse_market_maps_settled_result():
    m = parse_market({"ticker": "T1", "status": "finalized", "result": "yes", "title": "x"})
    assert m.result == "yes"

def test_parse_market_result_is_none_when_open():
    m = parse_market({"ticker": "T1", "status": "open", "title": "x"})
    assert m.result is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --system-certs pytest tests/test_venue_kalshi.py -v`
Expected: FAIL — `VenueMarket` has no field `result` (or the attribute is missing).

- [ ] **Step 3: Add the field and mapping**

In `src/tradetk/venues/base.py`, inside `class VenueMarket`, after `liquidity`:
```python
    result: str | None = None  # settled outcome: "yes" | "no" | None while unresolved
```

In `src/tradetk/venues/kalshi.py`, inside `parse_market(...)`'s `VenueMarket(...)` call, add:
```python
        result=(raw.get("result") or None),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --system-certs pytest tests/test_venue_kalshi.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tradetk/venues/base.py src/tradetk/venues/kalshi.py tests/test_venue_kalshi.py
git commit -m "Paper executor: VenueMarket carries the settled result (read-only)"
```

---

### Task 2: `risk/halts.py` — the pure halt gate

A pure parallel to step 14's `RiskLimits`/`RiskState`/`screen_new_entry`. Reuses step 14's `RiskDecision`. Reasons are open strings, exactly what step 14 left room for.

**Files:**
- Create: `src/tradetk/risk/halts.py`
- Modify: `src/tradetk/risk/__init__.py` (export the new names)
- Test: `tests/test_risk_halts.py`

**Interfaces:**
- Consumes: `RiskDecision` from `tradetk.risk.gate`.
- Produces:
  - `HaltLimits(max_daily_loss_dollars: Decimal, max_total_drawdown_dollars: Decimal, data_staleness_halt_seconds: Decimal)` + `HaltLimits.from_config(config) -> HaltLimits` (reads `config.risk.*`).
  - `BookHealth(realized_today: Decimal, drawdown: Decimal, data_age_seconds: Decimal, drawdown_latched: bool)`.
  - `screen_halts(health: BookHealth, limits: HaltLimits) -> RiskDecision` — reasons `"drawdown_halt"`, `"daily_loss_halt"`, `"stale_data_halt"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_risk_halts.py
from decimal import Decimal

from tradetk.risk import BookHealth, HaltLimits, screen_halts

D = Decimal
LIMITS = HaltLimits(
    max_daily_loss_dollars=D("5"),
    max_total_drawdown_dollars=D("8"),
    data_staleness_halt_seconds=D("300"),
)


def _health(realized=D("0"), drawdown=D("0"), age=D("0"), latched=False):
    return BookHealth(realized_today=realized, drawdown=drawdown,
                      data_age_seconds=age, drawdown_latched=latched)


def test_healthy_book_is_admitted():
    assert screen_halts(_health(), LIMITS).admitted is True


def test_daily_loss_at_limit_halts():
    d = screen_halts(_health(realized=D("-5")), LIMITS)
    assert d.admitted is False and d.reason == "daily_loss_halt"


def test_daily_loss_just_under_limit_is_admitted():
    assert screen_halts(_health(realized=D("-4.99")), LIMITS).admitted is True


def test_daily_profit_never_halts():
    assert screen_halts(_health(realized=D("50")), LIMITS).admitted is True


def test_drawdown_at_limit_halts():
    d = screen_halts(_health(drawdown=D("8")), LIMITS)
    assert d.admitted is False and d.reason == "drawdown_halt"


def test_drawdown_latch_halts_even_when_current_drawdown_is_zero():
    d = screen_halts(_health(drawdown=D("0"), latched=True), LIMITS)
    assert d.admitted is False and d.reason == "drawdown_halt"


def test_staleness_strictly_greater_than_limit_halts():
    assert screen_halts(_health(age=D("301")), LIMITS).reason == "stale_data_halt"
    assert screen_halts(_health(age=D("300")), LIMITS).admitted is True


def test_drawdown_outranks_daily_loss_when_both_trip():
    d = screen_halts(_health(realized=D("-9"), drawdown=D("9")), LIMITS)
    assert d.reason == "drawdown_halt"


def test_from_config_reads_risk_block():
    class _R:
        max_daily_loss_dollars = 5.0
        max_total_drawdown_dollars = 8.0
        data_staleness_halt_seconds = 300.0

    class _C:
        risk = _R()

    limits = HaltLimits.from_config(_C())
    assert limits.max_daily_loss_dollars == D("5.0")
    assert limits.data_staleness_halt_seconds == D("300.0")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --system-certs pytest tests/test_risk_halts.py -v`
Expected: FAIL — `cannot import name 'BookHealth'`.

- [ ] **Step 3: Implement `risk/halts.py`**

```python
# src/tradetk/risk/halts.py
"""Book-wide capital circuit-breakers, checked once per poll before any entry.

The step-14 seam: `risk/gate.py` kept reasons as open strings precisely so these
halts could be added without breaking a consumer. Pure and stateless, symmetric
to `RiskLimits`/`RiskState`: the caller derives a `BookHealth` snapshot, the gate
only decides. A halt stops *new* risk; it never freezes an open position from
settling — that ordering lives in the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tradetk.risk.gate import RiskDecision

_ADMIT = RiskDecision(admitted=True, reason=None)


@dataclass(frozen=True)
class HaltLimits:
    """The three circuit-breaker thresholds, from `config.risk`."""

    max_daily_loss_dollars: Decimal
    max_total_drawdown_dollars: Decimal
    data_staleness_halt_seconds: Decimal

    @classmethod
    def from_config(cls, config: Any) -> "HaltLimits":
        return cls(
            max_daily_loss_dollars=Decimal(str(config.risk.max_daily_loss_dollars)),
            max_total_drawdown_dollars=Decimal(str(config.risk.max_total_drawdown_dollars)),
            data_staleness_halt_seconds=Decimal(str(config.risk.data_staleness_halt_seconds)),
        )


@dataclass(frozen=True)
class BookHealth:
    """The halt-relevant snapshot. `realized_today` is negative when losing."""

    realized_today: Decimal
    drawdown: Decimal
    data_age_seconds: Decimal
    drawdown_latched: bool


def screen_halts(health: BookHealth, limits: HaltLimits) -> RiskDecision:
    """Severity order: permanent drawdown, then daily loss, then transient staleness."""
    if health.drawdown_latched or health.drawdown >= limits.max_total_drawdown_dollars:
        return RiskDecision(False, "drawdown_halt")
    if -health.realized_today >= limits.max_daily_loss_dollars:
        return RiskDecision(False, "daily_loss_halt")
    if health.data_age_seconds > limits.data_staleness_halt_seconds:
        return RiskDecision(False, "stale_data_halt")
    return _ADMIT
```

- [ ] **Step 4: Export the new names**

In `src/tradetk/risk/__init__.py`, add the import and `__all__` entries:
```python
from tradetk.risk.halts import BookHealth, HaltLimits, screen_halts
```
Add `"HaltLimits"`, `"BookHealth"`, `"screen_halts"` to `__all__`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --system-certs pytest tests/test_risk_halts.py -v`
Expected: PASS (all 9).

- [ ] **Step 6: Verify the leaf invariant + ruff, then commit**

Run: `uv run --system-certs python -c "import ast,sys; src=open('src/tradetk/risk/halts.py').read(); mods=[n.module for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom)]; bad=[m for m in mods if m and m.startswith('tradetk') and not m.startswith('tradetk.risk')]; sys.exit('LEAK: '+str(bad) if bad else 0)"`
Expected: exit 0 (no cross-package imports).
Run: `uv run --system-certs ruff check src/tradetk/risk/ tests/test_risk_halts.py`

```bash
git add src/tradetk/risk/halts.py src/tradetk/risk/__init__.py tests/test_risk_halts.py
git commit -m "Paper executor: pure halt gate (daily-loss, drawdown, staleness)"
```

---

### Task 3: `state/ledger.py` and `state/settle.py` — the event store and projection

The single source of truth: an append-only JSONL event log, folded into the current book. The book, realized-today, drawdown, and the drawdown latch are all *projections* — nothing is mutated in place. Settlement P&L is a small pure function beside it.

**Files:**
- Create: `src/tradetk/state/ledger.py`
- Create: `src/tradetk/state/settle.py`
- Test: `tests/test_paper_ledger.py`
- Test: `tests/test_paper_settle.py`

**Interfaces:**
- Consumes: `RiskState`, `OpenRisk` from `tradetk.risk`; `VenueMarket` from `tradetk.venues.base`.
- Produces:
  - Event constructors returning plain `dict` (JSON-ready): `fill_event`, `settle_event`, `halt_event`, `reset_event` — each carrying an `idempotency_key`.
  - `read_ledger(path) -> list[dict]`; `append_events(path, events) -> int` (skips events whose `idempotency_key` already exists; returns count written).
  - `OpenPaper(ticker, underlying, side, contracts, cost, resolution_time)`.
  - `PaperBook(open: tuple[OpenPaper, ...], realized_today, drawdown, drawdown_latched)` with `.risk_state() -> RiskState` and `.capital_deployed -> Decimal`.
  - `project(events, *, starting_capital: Decimal, today: date) -> PaperBook`.
  - `settle_position(*, side, contracts, cost, market) -> SettleOutcome | None` and `SettleOutcome(result, proceeds, realized_pnl)`.

- [ ] **Step 1: Write the failing settle tests**

```python
# tests/test_paper_settle.py
from decimal import Decimal

from tradetk.state.settle import settle_position
from tradetk.venues.base import VenueMarket

D = Decimal


def _mkt(status="finalized", result="yes"):
    return VenueMarket(ticker="T", title="x", status=status, result=result)


def test_yes_position_wins_when_resolved_yes():
    out = settle_position(side="yes", contracts=5, cost=D("2.00"), market=_mkt(result="yes"))
    assert out.proceeds == D("5") and out.realized_pnl == D("3.00")


def test_yes_position_loses_when_resolved_no():
    out = settle_position(side="yes", contracts=5, cost=D("2.00"), market=_mkt(result="no"))
    assert out.proceeds == D("0") and out.realized_pnl == D("-2.00")


def test_no_position_wins_when_resolved_no():
    out = settle_position(side="no", contracts=4, cost=D("1.50"), market=_mkt(result="no"))
    assert out.proceeds == D("4") and out.realized_pnl == D("2.50")


def test_unresolved_market_is_pending():
    assert settle_position(side="yes", contracts=5, cost=D("2"), market=_mkt(status="open", result=None)) is None
    assert settle_position(side="yes", contracts=5, cost=D("2"), market=_mkt(status="finalized", result="")) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --system-certs pytest tests/test_paper_settle.py -v`
Expected: FAIL — module `tradetk.state.settle` does not exist.

- [ ] **Step 3: Implement `state/settle.py`**

```python
# src/tradetk/state/settle.py
"""Settle one open paper position against the venue's resolved outcome.

Pure. The same contract-payout math the backtest uses: a held side that wins
pays $1 per contract, a losing side pays nothing, and settlement itself is free
(Kalshi charges on trades, not resolution — the entry fee is already in `cost`).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tradetk.venues.base import VenueMarket

_SETTLED = {"settled", "finalized"}


@dataclass(frozen=True)
class SettleOutcome:
    result: str
    proceeds: Decimal
    realized_pnl: Decimal


def settle_position(
    *, side: str, contracts: int, cost: Decimal, market: VenueMarket
) -> SettleOutcome | None:
    """Return the outcome, or ``None`` when the market has not resolved yet."""
    if market.status not in _SETTLED or not market.result:
        return None
    resolved_yes = market.result == "yes"
    side_won = resolved_yes if side == "yes" else not resolved_yes
    proceeds = Decimal(contracts) if side_won else Decimal(0)
    return SettleOutcome(result=market.result, proceeds=proceeds, realized_pnl=proceeds - cost)
```

- [ ] **Step 4: Run settle tests to verify they pass**

Run: `uv run --system-certs pytest tests/test_paper_settle.py -v`
Expected: PASS (4).

- [ ] **Step 5: Write the failing ledger tests**

```python
# tests/test_paper_ledger.py
from datetime import date, datetime, timezone
from decimal import Decimal

from tradetk.state.ledger import (
    append_events,
    fill_event,
    project,
    read_ledger,
    reset_event,
    settle_event,
)

D = Decimal
TODAY = date(2026, 8, 6)


def _ts(day=6, hour=12):
    return datetime(2026, 8, day, hour, tzinfo=timezone.utc)


def _fill(ticker, underlying, side, contracts, cost, ts):
    return fill_event(ticker=ticker, underlying=underlying, side=side, contracts=contracts,
                      assumed_price=D(str(cost)) / contracts, fee=D("0"), cost=D(str(cost)),
                      resolution_time=_ts(day=7), ts=ts)


def test_open_book_is_fills_without_a_later_settle():
    events = [_fill("A", "BTC", "yes", 5, "2.00", _ts())]
    book = project(events, starting_capital=D("20"), today=TODAY)
    assert len(book.open) == 1
    assert book.open[0].ticker == "A" and book.open[0].cost == D("2.00")
    assert book.capital_deployed == D("2.00")


def test_settle_removes_from_book_and_scores_realized_today():
    events = [
        _fill("A", "BTC", "yes", 5, "2.00", _ts(hour=10)),
        settle_event(ticker="A", result="no", side="yes", contracts=5,
                     proceeds=D("0"), realized_pnl=D("-2.00"),
                     resolution_time=_ts(day=7), ts=_ts(hour=11)),
    ]
    book = project(events, starting_capital=D("20"), today=TODAY)
    assert book.open == ()
    assert book.realized_today == D("-2.00")


def test_realized_today_excludes_other_days():
    events = [
        settle_event(ticker="A", result="yes", side="yes", contracts=1, proceeds=D("1"),
                     realized_pnl=D("-1.00"), resolution_time=_ts(day=5), ts=_ts(day=5)),
        settle_event(ticker="B", result="yes", side="yes", contracts=1, proceeds=D("1"),
                     realized_pnl=D("-3.00"), resolution_time=_ts(day=6), ts=_ts(day=6)),
    ]
    book = project(events, starting_capital=D("20"), today=TODAY)
    assert book.realized_today == D("-3.00")


def test_drawdown_is_peak_minus_current_realized_equity():
    events = [
        settle_event(ticker="A", result="yes", side="yes", contracts=6, proceeds=D("6"),
                     realized_pnl=D("4.00"), resolution_time=_ts(day=5), ts=_ts(day=5)),   # equity 24, peak 24
        settle_event(ticker="B", result="no", side="yes", contracts=7, proceeds=D("0"),
                     realized_pnl=D("-7.00"), resolution_time=_ts(day=6), ts=_ts(day=6)),  # equity 17
    ]
    book = project(events, starting_capital=D("20"), today=TODAY)
    assert book.drawdown == D("7.00")


def test_drawdown_latches_on_halt_event_and_clears_on_reset():
    from tradetk.state.ledger import halt_event
    breach = halt_event(reason="drawdown_halt", realized_today=D("0"), drawdown=D("9"),
                        data_age_seconds=D("0"), ts=_ts(hour=9))
    assert project([breach], starting_capital=D("20"), today=TODAY).drawdown_latched is True
    cleared = [breach, reset_event(note="manual", ts=_ts(hour=10))]
    assert project(cleared, starting_capital=D("20"), today=TODAY).drawdown_latched is False


def test_append_is_idempotent_by_key(tmp_path):
    path = tmp_path / "ledger.jsonl"
    e = _fill("A", "BTC", "yes", 5, "2.00", _ts())
    assert append_events(path, [e]) == 1
    assert append_events(path, [e]) == 0          # same key, skipped
    assert len(read_ledger(path)) == 1


def test_risk_state_projection_matches_open_book():
    events = [
        _fill("A", "BTC", "yes", 5, "2.00", _ts()),
        _fill("B", "BTC", "no", 4, "1.50", _ts()),
    ]
    rs = project(events, starting_capital=D("20"), today=TODAY).risk_state()
    assert rs.slots_used == 2 and rs.slots_for("BTC") == 2
    assert rs.capital_deployed == D("3.50")
```

- [ ] **Step 6: Run to verify it fails**

Run: `uv run --system-certs pytest tests/test_paper_ledger.py -v`
Expected: FAIL — module `tradetk.state.ledger` does not exist.

- [ ] **Step 7: Implement `state/ledger.py`**

```python
# src/tradetk/state/ledger.py
"""The paper book's source of truth: an append-only JSONL event log.

The open book, realized-today, drawdown and the drawdown latch are all
*projections* folded from the log — there is no separately-mutated state file to
drift. Money is Decimal, serialized as strings. Append is idempotent by key, so a
retried poll converges to the same book.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tradetk.risk import OpenRisk, RiskState


# -- event constructors ------------------------------------------------

def _iso(ts: datetime) -> str:
    return ts.isoformat()


def fill_event(*, ticker: str, underlying: str, side: str, contracts: int,
               assumed_price: Decimal, fee: Decimal, cost: Decimal,
               resolution_time: datetime, ts: datetime) -> dict[str, Any]:
    return {
        "type": "fill", "ts": _iso(ts), "ticker": ticker, "underlying": underlying,
        "side": side, "contracts": contracts, "assumed_price": str(assumed_price),
        "fee": str(fee), "cost": str(cost), "resolution_time": _iso(resolution_time),
        "idempotency_key": f"fill:{ticker}:{_iso(ts)}",
    }


def settle_event(*, ticker: str, result: str, side: str, contracts: int,
                 proceeds: Decimal, realized_pnl: Decimal,
                 resolution_time: datetime, ts: datetime) -> dict[str, Any]:
    return {
        "type": "settle", "ts": _iso(ts), "ticker": ticker, "result": result, "side": side,
        "contracts": contracts, "proceeds": str(proceeds), "realized_pnl": str(realized_pnl),
        "resolution_time": _iso(resolution_time),
        "idempotency_key": f"settle:{ticker}",
    }


def halt_event(*, reason: str, realized_today: Decimal, drawdown: Decimal,
               data_age_seconds: Decimal, ts: datetime) -> dict[str, Any]:
    return {
        "type": "halt", "ts": _iso(ts), "reason": reason,
        "realized_today": str(realized_today), "drawdown": str(drawdown),
        "data_age_seconds": str(data_age_seconds),
        "idempotency_key": f"halt:{reason}:{_iso(ts)}",
    }


def reset_event(*, note: str, ts: datetime) -> dict[str, Any]:
    return {"type": "reset", "ts": _iso(ts), "note": note,
            "idempotency_key": f"reset:{_iso(ts)}"}


# -- file I/O ----------------------------------------------------------

def read_ledger(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def append_events(path: str | Path, events: list[dict[str, Any]]) -> int:
    """Append events whose idempotency_key is not already present. Returns count written."""
    p = Path(path)
    seen = {e.get("idempotency_key") for e in read_ledger(p)}
    fresh = [e for e in events if e.get("idempotency_key") not in seen]
    if not fresh:
        return 0
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        for e in fresh:
            fh.write(json.dumps(e) + "\n")
    return len(fresh)


# -- projection --------------------------------------------------------

@dataclass(frozen=True)
class OpenPaper:
    ticker: str
    underlying: str
    side: str
    contracts: int
    cost: Decimal
    resolution_time: datetime


@dataclass(frozen=True)
class PaperBook:
    open: tuple[OpenPaper, ...]
    realized_today: Decimal
    drawdown: Decimal
    drawdown_latched: bool

    @property
    def capital_deployed(self) -> Decimal:
        return sum((o.cost for o in self.open), Decimal(0))

    def risk_state(self) -> RiskState:
        return RiskState(open=tuple(
            OpenRisk(o.ticker, o.underlying, o.cost) for o in self.open
        ))


def project(events: list[dict[str, Any]], *, starting_capital: Decimal, today: date) -> PaperBook:
    open_by_ticker: dict[str, OpenPaper] = {}
    realized_today = Decimal(0)
    cumulative = Decimal(0)
    peak = starting_capital
    latched = False

    for e in events:
        etype = e["type"]
        if etype == "fill":
            open_by_ticker[e["ticker"]] = OpenPaper(
                ticker=e["ticker"], underlying=e["underlying"], side=e["side"],
                contracts=int(e["contracts"]), cost=Decimal(e["cost"]),
                resolution_time=datetime.fromisoformat(e["resolution_time"]),
            )
        elif etype == "settle":
            open_by_ticker.pop(e["ticker"], None)
            pnl = Decimal(e["realized_pnl"])
            cumulative += pnl
            peak = max(peak, starting_capital + cumulative)
            if datetime.fromisoformat(e["ts"]).date() == today:
                realized_today += pnl
        elif etype == "halt" and e.get("reason") == "drawdown_halt":
            latched = True
        elif etype == "reset":
            latched = False

    drawdown = peak - (starting_capital + cumulative)
    return PaperBook(
        open=tuple(open_by_ticker.values()),
        realized_today=realized_today,
        drawdown=drawdown,
        drawdown_latched=latched,
    )
```

- [ ] **Step 8: Run all Task-3 tests to verify they pass**

Run: `uv run --system-certs pytest tests/test_paper_ledger.py tests/test_paper_settle.py -v`
Expected: PASS (all).

- [ ] **Step 9: ruff, then commit**

Run: `uv run --system-certs ruff check src/tradetk/state/ tests/test_paper_ledger.py tests/test_paper_settle.py`

```bash
git add src/tradetk/state/ledger.py src/tradetk/state/settle.py tests/test_paper_ledger.py tests/test_paper_settle.py
git commit -m "Paper executor: append-only ledger, projection, and settlement math"
```

---

### Task 4: `cli/paper.py` — the poll orchestrator

Wires the five-phase lifecycle: load → settle → halt → evaluate → emit. The per-side evaluation loop mirrors `BacktestEngine._best_assessment` with the overlay off; a cross-check test proves paper and the engine choose the same side for the same inputs (invariant #3), instead of extracting the engine (out of scope per the spec).

**Files:**
- Create: `src/tradetk/cli/paper.py`
- Test: `tests/test_paper_cli.py`

**Interfaces:**
- Consumes: `read_ledger`, `append_events`, `project`, `fill_event`, `settle_event`, `halt_event` (Task 3); `settle_position` (Task 3); `HaltLimits`, `BookHealth`, `screen_halts`, `RiskLimits`, `screen_new_entry`, `screen_cost` (risk); `plan_size`, `SizingLimits`, `SizingMode`, `assess_side`, `side_depth`, `GateLimits`, `Side` (translation); `TapeReplay` (`backtest.replay`); `MarketDataSet`, `load_underlying_data` (`cli/backtest.py`); `get_strategy`, `StrategyContext` (strategy); `KalshiVenue` (venues); `HyperliquidProvider` (signals); `UnderlyingRegistry` (translation.claims); `KalshiFeeModel`, `FeeRounding` (costs); `load_config` (config.loader).
- Produces:
  - `choose_side(claim, estimate, book, when, capital_in_use, *, gate_limits, sizing_limits, fee_model) -> tuple[EdgeAssessment | None, str]`.
  - `run_paper_poll(*, tape_dir, registry, config, ledger_path, provider, venue, strategy, data, now) -> dict` — the JSON-ready poll summary.
  - `main(argv) -> int` — CLI entry emitting the summary as JSON.

- [ ] **Step 1: Write the failing orchestrator tests (fakes, no network)**

```python
# tests/test_paper_cli.py
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tradetk.cli.paper import run_paper_poll
from tradetk.state.ledger import read_ledger

D = Decimal
NOW = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)

# See conftest fakes described in Step 3. These tests assume:
#   make_env(...) -> a namespace bundling a fake venue, provider, tape, registry,
#   config, strategy, and MarketDataSet wired so exactly one candidate ("A"/BTC)
#   clears every gate at NOW, resolving 1 day out.


def test_a_clean_poll_opens_one_paper_position(paper_env, tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    summary = run_paper_poll(**paper_env(ledger_path=ledger), now=NOW)
    assert summary["halted"] is None
    assert len(summary["fills"]) == 1 and summary["fills"][0]["ticker"] == "A"
    assert any(e["type"] == "fill" for e in read_ledger(ledger))


def test_stale_data_halts_and_opens_nothing(paper_env, tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    # candle age pushed beyond data_staleness_halt_seconds
    summary = run_paper_poll(**paper_env(ledger_path=ledger, data_age_seconds=10_000), now=NOW)
    assert summary["halted"] == "stale_data_halt"
    assert summary["fills"] == []
    assert any(e["type"] == "halt" for e in read_ledger(ledger))


def test_settlement_runs_before_halt_and_closes_a_resolved_position(paper_env, tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    env = paper_env(ledger_path=ledger, prefill_ticker="Z", prefill_result="no")
    summary = run_paper_poll(**env, now=NOW)
    settles = [e for e in read_ledger(ledger) if e["type"] == "settle"]
    assert settles and settles[0]["ticker"] == "Z"


def test_rerun_of_same_poll_is_idempotent(paper_env, tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    env = paper_env(ledger_path=ledger)
    run_paper_poll(**env, now=NOW)
    run_paper_poll(**paper_env(ledger_path=ledger), now=NOW)
    fills = [e for e in read_ledger(ledger) if e["type"] == "fill"]
    assert len(fills) == 1  # second run added no duplicate
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --system-certs pytest tests/test_paper_cli.py -v`
Expected: FAIL — `cannot import name 'run_paper_poll'` (and the `paper_env` fixture is undefined).

- [ ] **Step 3: Add the `paper_env` fixture**

Append to `tests/conftest.py` (create if absent). The fixture builds in-memory fakes so the poll runs without network. Fill counts/prices are chosen so ticker "A" (BTC) passes every gate at NOW.

```python
# tests/conftest.py  (append)
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

D = Decimal
_NOW = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)


class _FakeVenue:
    """Read-only: only .market(ticker) is used, for settlement."""
    def __init__(self, results):  # results: {ticker: (status, result)}
        self._results = results

    def market(self, ticker):
        from tradetk.venues.base import VenueMarket
        status, result = self._results.get(ticker, ("open", None))
        return VenueMarket(ticker=ticker, title="x", status=status, result=result)


@pytest.fixture
def paper_env(monkeypatch):
    """Return a callable producing kwargs for run_paper_poll, with sane fakes.

    The implementer wires this against the real TapeReplay/registry/config/
    strategy/MarketDataSet builders. The one candidate 'A'/BTC must clear every
    gate at _NOW with a book deep enough to fill ~5 contracts near $0.40 ask,
    resolving 1 day out. `data_age_seconds` controls the staleness input;
    `prefill_ticker`/`prefill_result` seed an open position via a fill event so
    settlement has something to close.
    """
    def _make(*, ledger_path, data_age_seconds=D("0"), prefill_ticker=None, prefill_result=None):
        # Implementer: assemble tape (freshest book for 'A'), registry, config
        # (config.risk staleness > 0, capital $20/6 slots), strategy, MarketDataSet,
        # and a _FakeVenue. If prefill_ticker: append a fill_event to ledger_path first,
        # and give _FakeVenue {prefill_ticker: ("finalized", prefill_result)}.
        ...
    return _make
```

Note to implementer: the fixture body is the one place you assemble real objects; keep it small by reusing `UnderlyingRegistry.from_yaml` on a tmp YAML and a minimal hand-built `MarketDataSet`. If a full `MarketDataSet` snapshot is heavy to fake, inject a pre-built `data` object and a `strategy` stub whose `.estimate` returns a fixed non-abstaining `StrategyOpinion` — the gate stack is what's under test here, not the vol model.

- [ ] **Step 4: Implement `cli/paper.py`**

```python
# src/tradetk/cli/paper.py
"""``paper`` — advance a simulated book one poll, using the live decision path.

Runs inside the sweep, after record+shadow. It reads the freshest recorded book
(the slice `record` just captured), evaluates it through the exact gate stack the
backtest uses, and "fills" by walking that book — no order is ever sent, and no
order endpoint is in this module's import graph. Positions persist in an
append-only ledger and settle from the venue's real resolution (read-only).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal

import truststore

from tradetk.backtest.replay import ReplayError, TapeReplay
from tradetk.cli.backtest import load_underlying_data
from tradetk.config.loader import load_config
from tradetk.costs.fees import FeeRounding, KalshiFeeModel
from tradetk.risk import (
    BookHealth,
    HaltLimits,
    RiskLimits,
    screen_cost,
    screen_halts,
    screen_new_entry,
)
from tradetk.signals.hyperliquid import HyperliquidProvider
from tradetk.state.ledger import (
    append_events,
    fill_event,
    halt_event,
    project,
    read_ledger,
    settle_event,
)
from tradetk.state.settle import settle_position
from tradetk.strategy import StrategyContext, get_strategy
from tradetk.translation.claims import UnderlyingRegistry
from tradetk.translation.edge import GateLimits, assess_side, side_depth
from tradetk.translation.sizing import SizingLimits, SizingMode, plan_size
from tradetk.venues.base import Side
from tradetk.venues.kalshi import KalshiVenue

log = logging.getLogger("tradetk.cli.paper")


def choose_side(claim, estimate, book, when, capital_in_use, *,
                gate_limits, sizing_limits, fee_model):
    """The overlay-off twin of BacktestEngine._best_assessment.

    A cross-check test asserts this returns the same choice as the engine for the
    same inputs, so the two cannot silently disagree on a decision.
    """
    best = None
    best_cap = "none"
    for side in (Side.yes, Side.no):
        price = book.best_yes_ask if side is Side.yes else book.best_no_ask
        if price is None:
            continue
        depth = side_depth(book, side)
        plan = plan_size(price, fee_model, sizing_limits,
                         book_depth=depth, capital_in_use=capital_in_use)
        if not plan.tradeable:
            continue
        assessment = assess_side(claim, estimate, book, side=side, contracts=plan.contracts,
                                 fee_model=fee_model, limits=gate_limits, now=when)
        if not assessment.passed:
            continue
        if best is None or assessment.net_edge_pp > best.net_edge_pp:
            best, best_cap = assessment, plan.binding_cap.value
    return best, best_cap


def _latest_books(replay: TapeReplay) -> dict[str, object]:
    """The freshest book per ticker — the slice `record` just captured."""
    latest: dict[str, object] = {}
    for obs in replay.observations:      # yielded in (observed_at, ticker) order
        latest[obs.ticker] = obs.book
    return latest


def run_paper_poll(*, tape_dir, registry, config, ledger_path, provider, venue,
                   strategy, data, now, vol_lookback_days=30, data_age_seconds=None) -> dict:
    risk_limits = RiskLimits.from_config(config)
    halt_limits = HaltLimits.from_config(config)
    starting_capital = Decimal(str(config.capital.total_capital))
    fee_model = KalshiFeeModel(rounding=FeeRounding.cent)
    sizing_limits = SizingLimits(
        position_target=Decimal(str(config.capital.position_target)),
        per_position_ceiling=Decimal(str(config.capital.per_position_ceiling)),
        total_capital=starting_capital,
        max_book_participation_pct=Decimal(str(config.liquidity.max_book_participation_pct)),
        mode=SizingMode.fixed_dollar,
        fixed_contracts=1,
    )
    gate_limits = GateLimits.from_config(config)

    summary: dict = {"halted": None, "settled": [], "fills": [], "pending_settlement": [], "errors": []}

    # -- phase 1: load ------------------------------------------------
    events = read_ledger(ledger_path)
    book = project(events, starting_capital=starting_capital, today=now.date())

    # -- phase 2: settle first (runs even when halted) ----------------
    settle_events = []
    for pos in book.open:
        try:
            market = venue.market(pos.ticker)
        except Exception as exc:  # noqa: BLE001 - one bad read must not kill the poll
            summary["errors"].append(f"settle-read {pos.ticker}: {exc}")
            summary["pending_settlement"].append(pos.ticker)
            continue
        outcome = settle_position(side=pos.side, contracts=pos.contracts, cost=pos.cost, market=market)
        if outcome is None:
            if pos.resolution_time <= now:
                summary["pending_settlement"].append(pos.ticker)
            continue
        settle_events.append(settle_event(
            ticker=pos.ticker, result=outcome.result, side=pos.side, contracts=pos.contracts,
            proceeds=outcome.proceeds, realized_pnl=outcome.realized_pnl,
            resolution_time=pos.resolution_time, ts=now,
        ))
        summary["settled"].append({"ticker": pos.ticker, "realized_pnl": str(outcome.realized_pnl)})
    append_events(ledger_path, settle_events)
    events = read_ledger(ledger_path)
    book = project(events, starting_capital=starting_capital, today=now.date())

    # -- phase 3: halt gate (once) ------------------------------------
    age = Decimal(str(data_age_seconds)) if data_age_seconds is not None else _data_age(data, now)
    health = BookHealth(realized_today=book.realized_today, drawdown=book.drawdown,
                        data_age_seconds=age, drawdown_latched=book.drawdown_latched)
    decision = screen_halts(health, halt_limits)
    if not decision.admitted:
        append_events(ledger_path, [halt_event(
            reason=decision.reason, realized_today=book.realized_today, drawdown=book.drawdown,
            data_age_seconds=age, ts=now,
        )])
        summary["halted"] = decision.reason
        return summary

    # -- phase 4: evaluate --------------------------------------------
    try:
        replay = TapeReplay.from_tape(tape_dir)
    except ReplayError as exc:
        summary["errors"].append(f"tape: {exc}")
        return summary

    risk_state = book.risk_state()
    capital_in_use = book.capital_deployed
    open_tickers = {o.ticker for o in book.open}
    fills = []
    for ticker, live_book in _latest_books(replay).items():
        if ticker in open_tickers:
            continue
        claim = replay.claim_as_of(ticker, now, registry)
        if claim is None:
            continue
        snapshot = data.snapshot_at(claim.underlying, now, lookback_days=vol_lookback_days)
        if snapshot is None:
            continue
        opinion = strategy.estimate(claim, StrategyContext(now=now, snapshot=snapshot, book=live_book))
        if opinion.abstained:
            continue
        if not screen_new_entry(claim.underlying, risk_state, risk_limits).admitted:
            continue
        assessment, _cap = choose_side(claim, opinion.estimate, live_book, now, capital_in_use,
                                       gate_limits=gate_limits, sizing_limits=sizing_limits,
                                       fee_model=fee_model)
        if assessment is None:
            continue
        if not screen_cost(assessment.capital_at_risk, risk_state, risk_limits).admitted:
            continue
        walk = (live_book.walk_to_buy_yes if assessment.side is Side.yes
                else live_book.walk_to_buy_no)
        filled, cost = walk(assessment.contracts_requested)
        if filled <= 0:
            continue
        price = (cost / filled) if filled else Decimal(0)
        ev = fill_event(
            ticker=ticker, underlying=claim.underlying, side=assessment.side.value,
            contracts=int(filled), assumed_price=price, fee=Decimal(0), cost=cost,
            resolution_time=claim.resolution_time, ts=now,
        )
        fills.append(ev)
        summary["fills"].append({"ticker": ticker, "side": assessment.side.value,
                                 "contracts": int(filled), "cost": str(cost)})
        # let later candidates see the newly-used slot and capital
        from tradetk.risk import OpenRisk, RiskState
        risk_state = RiskState(open=risk_state.open + (OpenRisk(ticker, claim.underlying, cost),))
        capital_in_use += cost

    append_events(ledger_path, fills)
    return summary


def _data_age(data, now: datetime) -> Decimal:
    """Seconds since the freshest candle across the fetched underlyings."""
    newest = None
    for series in getattr(data, "series", {}).values():
        for candle in getattr(series, "candles", []):
            ct = getattr(candle, "close_time", None) or getattr(candle, "open_time", None)
            if ct is not None and (newest is None or ct > newest):
                newest = ct
    if newest is None:
        return Decimal("inf")
    return Decimal(str((now - newest).total_seconds()))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Advance the paper book one poll (no orders sent).")
    ap.add_argument("--tape-dir", default="data/tape")
    ap.add_argument("--registry", default="config/underlyings.yaml")
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--ledger", default="data/paper/ledger.jsonl")
    ap.add_argument("--strategy", default="baseline_vol")
    ap.add_argument("--vol-lookback-days", type=int, default=30)
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    truststore.inject_into_ssl()

    config = load_config(args.config)
    registry = UnderlyingRegistry.from_yaml(args.registry)
    strategy = get_strategy(args.strategy)
    now = datetime.now(timezone.utc)

    try:
        replay = TapeReplay.from_tape(args.tape_dir)
    except ReplayError as exc:
        print(json.dumps({"halted": None, "errors": [f"tape: {exc}"]}), file=sys.stdout)
        return 2

    start, end = replay.span
    symbols = {
        claim.underlying
        for ticker in replay.tickers
        if (claim := replay.claim_as_of(ticker, end, registry)) is not None
    }
    with HyperliquidProvider() as provider, KalshiVenue() as venue:
        data = load_underlying_data(provider, symbols, start=start, end=end,
                                    lookback_days=args.vol_lookback_days)
        summary = run_paper_poll(
            tape_dir=args.tape_dir, registry=registry, config=config, ledger_path=args.ledger,
            provider=provider, venue=venue, strategy=strategy, data=data, now=now,
            vol_lookback_days=args.vol_lookback_days,
        )
    print(json.dumps(summary, indent=2 if args.pretty else None, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

Note to implementer: before relying on them, verify against current source — `GateLimits.from_config` exists and reads `config.edge_gate`; `SizingLimits` field names match (`position_target`, `per_position_ceiling`, `total_capital`, `max_book_participation_pct`, `mode`, `fixed_contracts`); and `KalshiVenue()` supports the `with ... as venue` context-manager form. Mirror `cli/backtest.py`'s construction where they differ.

- [ ] **Step 5: Add the cross-check test (invariant #3)**

```python
# tests/test_paper_cli.py  (append)
def test_choose_side_matches_engine_best_assessment(engine_case):
    """Paper's per-side choice equals the engine's with the overlay off."""
    from tradetk.cli.paper import choose_side
    claim, estimate, book, when, cap, gate, sizing, fee, engine = engine_case
    paper_pick, _ = choose_side(claim, estimate, book, when, cap,
                                gate_limits=gate, sizing_limits=sizing, fee_model=fee)
    engine_pick, _ = engine._best_assessment(claim, estimate, book, when, cap)
    assert (paper_pick is None) == (engine_pick is None)
    if paper_pick is not None:
        assert paper_pick.side == engine_pick.side
        assert paper_pick.contracts_requested == engine_pick.contracts_requested
        assert paper_pick.net_edge_pp == engine_pick.net_edge_pp
```

Implementer: build `engine_case` as a fixture constructing a `BacktestEngine` with `overlay=None` and the same limits passed to `choose_side`, plus one claim/estimate/book that trades. Reuse the builders already in `tests/test_backtest.py`.

- [ ] **Step 6: Run all Task-4 tests to verify they pass**

Run: `uv run --system-certs pytest tests/test_paper_cli.py -v`
Expected: PASS (all).

- [ ] **Step 7: Verify no order endpoint in the import graph**

Run: `uv run --system-certs python -c "import tradetk.cli.paper, sys; mods=[m for m in sys.modules if m.startswith('tradetk')]; bad=[m for m in mods if 'execute' in m or m.endswith('.orders')]; print('BAD:', bad) if bad else print('clean')"`
Expected: `clean`.

- [ ] **Step 8: ruff, full suite, commit**

Run: `uv run --system-certs ruff check src/tradetk/cli/paper.py tests/test_paper_cli.py`
Run: `uv run --system-certs pytest -q`
Expected: all green.

```bash
git add src/tradetk/cli/paper.py tests/test_paper_cli.py tests/conftest.py
git commit -m "Paper executor: the five-phase poll orchestrator"
```

---

### Task 5: Wire into the sweep routine and commit the ledger path

Make the paper book durable across cloud runs (committed, not ignored) and add the `paper` phase to the sweep so the book actually advances.

**Files:**
- Modify: `.gitignore` (un-ignore `data/paper/`)
- Modify: `routines/sweep.md` (add the paper phase between shadow and the commit)
- Modify: `README.md` (tick step 15 in the Status checklist)

**Interfaces:** none (docs + config).

- [ ] **Step 1: Ensure the ledger directory is committed, not ignored**

Confirm `data/tape/` stays ignored and `data/shadow/` stays committed, then make `data/paper/` committed the same way. In `.gitignore`, if `data/` is broadly ignored, add a negation after it:
```
!data/paper/
!data/paper/**
```
Create the directory with a keeper so it exists on a fresh clone:
```bash
mkdir -p data/paper
printf '# paper book lives here (ledger.jsonl); committed so cloud runs persist it\n' > data/paper/README.md
git add -f data/paper/README.md
```

- [ ] **Step 2: Verify the path is trackable**

Run: `git check-ignore data/paper/ledger.jsonl; echo "exit=$?"`
Expected: `exit=1` (NOT ignored). If it prints the path (exit 0), the negation didn't take — fix the `.gitignore` ordering.

- [ ] **Step 3: Add the paper phase to `routines/sweep.md`**

After the shadow step and before the commit/notify step, insert:
```markdown
**Step 3.5 — advance the paper book.** Only if step 2 recorded books
successfully (a paper poll on a stale or absent tape would fill against fiction).

    uv run --system-certs python -m tradetk.cli.paper --pretty

Read the JSON summary. Notify if `halted` is non-null (a circuit-breaker
tripped — say which) or if `fills` is non-empty (a paper trade opened — list
ticker/side/contracts/cost). `settled` and `pending_settlement` are for the
digest, not a live ping. `errors` being non-empty is a notify. The ledger at
`data/paper/ledger.jsonl` is committed with everything else in step 5 — the book
does not survive the run otherwise.
```
Also add `tradetk.cli.paper` to the read-only allowed-commands list wherever `record`/`shadow` are enumerated (it sends no orders).

- [ ] **Step 4: Tick the build step**

In `README.md`, change the step-15 line from `- [ ] 15. Paper executor` to `- [x] 15. Paper executor` and update the `## Status:` header line to reference step 16 as next.

- [ ] **Step 5: Full suite (nothing code-level changed, but prove green) and commit**

Run: `uv run --system-certs pytest -q`
Expected: green.

```bash
git add .gitignore routines/sweep.md README.md data/paper/README.md
git commit -m "Paper executor: run it in the sweep, commit the book, tick step 15"
```

---

## Self-Review

**Spec coverage** (each spec section → task):
- Live-forward loop / five phases → Task 4 (`run_paper_poll`).
- Fill model A (walk live book, record assumed price) → Task 4 (walk + `fill_event`).
- Event-sourced ledger, committed, idempotent, projections → Task 3 + Task 5.
- Settlement from venue-actual resolution, `pending_settlement`, settle-before-halt → Task 1 (`result`), Task 3 (`settle_position`), Task 4 (phase 2 before phase 3).
- Halt seam (`HaltLimits`/`BookHealth`/`screen_halts`, three reasons, latch+reset) → Task 2 + Task 3 (latch fold).
- `risk/` leaf, no order endpoint → Task 2 Step 6, Task 4 Step 7.
- Paper decision sequence = backtest order, no silent divergence → Task 4 `choose_side` + cross-check test (Step 5).
- Run inside sweep, JSON summary, notify rules → Task 5.
- Data-staleness on underlying signal age → Task 4 `_data_age` + `data_age_seconds` override.
- Non-goals (no engine refactor, no MTM, no TA) → honored: `choose_side` duplicates rather than refactors; drawdown is realized-only in `project`.

**Placeholder scan:** the only `...` is the `paper_env` fixture body — the single integration-assembly point, left as guided scaffolding (real objects wired against the current builders) with an explicit implementer note. No placeholders remain in shipped `src/` code; the earlier lookback hack is now a real `vol_lookback_days` parameter. No `TODO`/`TBD` anywhere.

**Type consistency:** `fill_event`/`settle_event`/`halt_event`/`reset_event` keyword args match between Task 3's definitions and Task 4's calls; `PaperBook.risk_state()`/`.capital_deployed`, `OpenPaper.side/contracts/cost/resolution_time`, `BookHealth` fields, `HaltLimits.from_config`, and `screen_halts` reasons are used identically across tasks. `SettleOutcome.result/proceeds/realized_pnl` consumed as defined.
