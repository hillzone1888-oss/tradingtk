"""``paper`` — advance a simulated book one poll, using the live decision path.

Runs inside the sweep, after record+shadow. It reads the freshest recorded book
(the slice `record` just captured), evaluates it through the exact gate stack the
backtest uses, and "fills" by walking that book — no order is ever sent, and no
order endpoint is in this module's import graph. Positions persist in an
append-only ledger and settle from the venue's real resolution (read-only).

Five phases, in this order and no other: **load** the ledger into a book
projection, **settle** every open position against the venue's real result (this
runs even when the poll is about to halt — a resolved position must free its
capital regardless), **halt** — screen the book's health once, before any new
risk is considered, **evaluate** the freshest recorded book per ticker through
the shared gate stack and fill by walking it, **emit** a JSON-ready summary.
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

from tradetk.backtest.replay import ReplayError, TapeReplay
from tradetk.cli.backtest import load_underlying_data
from tradetk.config.loader import load_config
from tradetk.costs.fees import FeeRounding, KalshiFeeModel
from tradetk.risk import (
    BookHealth,
    HaltLimits,
    OpenRisk,
    RiskLimits,
    RiskState,
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
from tradetk.translation.edge import EdgeAssessment, GateLimits, assess_side, side_depth
from tradetk.translation.sizing import SizingLimits, plan_size
from tradetk.venues.base import BinaryBook, Side
from tradetk.venues.kalshi import KalshiVenue

log = logging.getLogger("tradetk.cli.paper")


def choose_side(
    claim, estimate, book: BinaryBook, when: datetime, capital_in_use: Decimal, *,
    gate_limits: GateLimits, sizing_limits: SizingLimits, fee_model: KalshiFeeModel,
) -> tuple[EdgeAssessment | None, str]:
    """The overlay-off twin of ``BacktestEngine._best_assessment``.

    A cross-check test asserts this returns the same choice as the engine for the
    same inputs, so the two cannot silently disagree on a decision.
    """
    best: EdgeAssessment | None = None
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


def _latest_books(replay: TapeReplay) -> dict[str, BinaryBook]:
    """The freshest book per ticker — the slice `record` just captured."""
    latest: dict[str, BinaryBook] = {}
    for obs in replay.observations():  # yielded in (observed_at, ticker) order
        latest[obs.ticker] = obs.book
    return latest


def _data_age(data: Any, now: datetime) -> Decimal:
    """Seconds since the freshest closed candle across the fetched underlyings.

    Reads only the public :meth:`MarketDataSet.coverage` surface, never the
    private series dict, so a caller supplying its own `data` object only has to
    implement that one method.
    """
    newest: datetime | None = None
    for cov in data.coverage():
        last_close = cov.get("last_close")
        if not last_close:
            continue
        ts = datetime.fromisoformat(last_close)
        if newest is None or ts > newest:
            newest = ts
    if newest is None:
        return Decimal("Infinity")
    return Decimal(str((now - newest).total_seconds()))


def run_paper_poll(
    *,
    tape_dir: str | Path,
    registry: UnderlyingRegistry,
    config: Any,
    ledger_path: str | Path,
    venue: Any,
    strategy: Any,
    data: Any,
    now: datetime,
    vol_lookback_days: int = 30,
    data_age_seconds: Decimal | int | str | None = None,
) -> dict[str, Any]:
    risk_limits = RiskLimits.from_config(config)
    halt_limits = HaltLimits.from_config(config)
    starting_capital = Decimal(str(config.capital.total_capital))
    fee_model = KalshiFeeModel(rounding=FeeRounding.cent)
    sizing_limits = SizingLimits.from_config(config)
    gate_limits = GateLimits.from_config(config)

    summary: dict[str, Any] = {
        "halted": None, "settled": [], "fills": [], "pending_settlement": [], "errors": [],
    }

    # -- phase 1: load ---------------------------------------------------
    events = read_ledger(ledger_path)
    book = project(events, starting_capital=starting_capital, today=now.date())

    # -- phase 2: settle first — runs even when phase 3 is about to halt,
    #    because a resolved position must free its capital regardless. ----
    settle_events = []
    for pos in book.open:
        try:
            market = venue.market(pos.ticker)
        except Exception as exc:  # noqa: BLE001 - one bad read must not kill the poll
            summary["errors"].append(f"settle-read {pos.ticker}: {exc}")
            summary["pending_settlement"].append(pos.ticker)
            continue
        outcome = settle_position(
            side=pos.side, contracts=pos.contracts, cost=pos.cost, market=market
        )
        if outcome is None:
            if pos.resolution_time <= now:
                summary["pending_settlement"].append(pos.ticker)
            continue
        settle_events.append(settle_event(
            ticker=pos.ticker, result=outcome.result, side=pos.side, contracts=pos.contracts,
            proceeds=outcome.proceeds, realized_pnl=outcome.realized_pnl,
            resolution_time=pos.resolution_time, ts=now,
        ))
        summary["settled"].append(
            {"ticker": pos.ticker, "realized_pnl": str(outcome.realized_pnl)}
        )
    append_events(ledger_path, settle_events)
    events = read_ledger(ledger_path)
    book = project(events, starting_capital=starting_capital, today=now.date())

    # -- phase 3: halt gate, once, after settlement has posted -----------
    age = (
        Decimal(str(data_age_seconds)) if data_age_seconds is not None
        else _data_age(data, now)
    )
    health = BookHealth(realized_today=book.realized_today, drawdown=book.drawdown,
                        data_age_seconds=age, drawdown_latched=book.drawdown_latched)
    decision = screen_halts(health, halt_limits)
    if not decision.admitted:
        append_events(ledger_path, [halt_event(
            reason=decision.reason, realized_today=book.realized_today,
            drawdown=book.drawdown, data_age_seconds=age, ts=now,
        )])
        summary["halted"] = decision.reason
        return summary

    # -- phase 4: evaluate the freshest recorded book per ticker ---------
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
        try:
            claim = replay.claim_as_of(ticker, now, registry)
            if claim is None:
                continue
            if claim.resolution_time <= now:
                continue
            snapshot = data.snapshot_at(claim.underlying, now, lookback_days=vol_lookback_days)
            if snapshot is None:
                continue
            opinion = strategy.estimate(
                claim, StrategyContext(now=now, snapshot=snapshot, book=live_book)
            )
            if opinion.abstained:
                continue
            if not screen_new_entry(claim.underlying, risk_state, risk_limits).admitted:
                continue
            assessment, _cap = choose_side(
                claim, opinion.estimate, live_book, now, capital_in_use,
                gate_limits=gate_limits, sizing_limits=sizing_limits, fee_model=fee_model,
            )
            if assessment is None:
                continue
            if not screen_cost(assessment.capital_at_risk, risk_state, risk_limits).admitted:
                continue

            # The fill IS the walk `assess_side` already did: it gated this exact
            # book through `execution_cost`, which walks `yes_asks`/`yes_bids`
            # itself. Reusing that result (rather than re-walking with the raw
            # `walk_to_buy_*` primitive) keeps the fee in the fill instead of
            # silently dropping it, and guarantees the fill can never diverge from
            # the numbers that were actually gated.
            execution = assessment.execution
            filled = int(execution.contracts_filled) if execution else 0
            if filled <= 0:
                continue
            cost = assessment.capital_at_risk
            price = execution.average_price if execution else None
            ev = fill_event(
                ticker=ticker, underlying=claim.underlying, side=assessment.side.value,
                contracts=filled, assumed_price=price or Decimal(0),
                fee=execution.fee if execution else Decimal(0), cost=cost,
                resolution_time=claim.resolution_time, ts=now,
            )
            fills.append(ev)
            summary["fills"].append({
                "ticker": ticker, "side": assessment.side.value,
                "contracts": filled, "cost": str(cost),
            })
            # Let later candidates in this same poll see the newly-used slot and
            # capital, so the loop cannot over-commit within one pass.
            risk_state = RiskState(
                open=risk_state.open + (OpenRisk(ticker, claim.underlying, cost),)
            )
            capital_in_use += cost
        except Exception as exc:  # noqa: BLE001 - one bad candidate must not kill the poll
            summary["errors"].append(f"evaluate {ticker}: {exc}")
            continue

    append_events(ledger_path, fills)
    return summary


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

    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr
    )
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
    with (
        HyperliquidProvider() as provider,
        KalshiVenue(environment=config.venue.environment.value) as venue,
    ):
        data = load_underlying_data(
            provider, symbols, start=start, end=end, lookback_days=args.vol_lookback_days
        )
        summary = run_paper_poll(
            tape_dir=args.tape_dir, registry=registry, config=config, ledger_path=args.ledger,
            venue=venue, strategy=strategy, data=data, now=now,
            vol_lookback_days=args.vol_lookback_days,
        )
    print(json.dumps(summary, indent=2 if args.pretty else None, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
