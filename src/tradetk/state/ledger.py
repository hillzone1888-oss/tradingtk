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
