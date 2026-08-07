"""``propose`` — scan live, run the full gate stack, write proposal files.

The read-only half of the execution boundary. Never contacts the order
endpoint; the live ledger is projected read-only (only ``execute`` appends).
One file per admitted trade: one file = one order = one typed confirmation.

Phases: **load** the live ledger (empty until step 17) -> **scan** live markets,
books, and fresh candles (read-only) -> **halt** gate once -> **evaluate** every
candidate through the shared overlay-aware assessment, rank passing candidates
by net edge, admit greedily against the rolling risk state -> **write** one
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

    # -- phase 1: load (read-only -- only execute may append) ------------
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


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Scan live markets, run the full gate stack, write one proposal per "
        "admitted trade. Read-only: no order endpoint is in this module's import graph."
    )
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--registry", default="config/underlyings.yaml")
    ap.add_argument("--ledger", default="data/live/ledger.jsonl")
    ap.add_argument("--proposals-dir", default=None,
                    help="Defaults to config.paths.proposals_dir.")
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
    proposals_dir = args.proposals_dir or config.paths.proposals_dir

    with (
        HyperliquidProvider() as provider,
        KalshiVenue(environment=config.venue.environment.value) as venue,
    ):
        # -- scan: read-only market discovery, mirroring cli/record.py's
        #    series -> tickers -> eligible_markets path. --------------------
        series = crypto_series(venue)
        tickers = [s["ticker"] for s in series if s.get("ticker")]
        markets = eligible_markets(
            venue, tickers, max_hours_to_close=float(config.horizon.max_hours_to_resolution),
            now=now,
        )

        if not series and not markets:
            print(json.dumps(
                {"halted": None, "proposed": [], "errors": ["no eligible markets found"]},
                indent=2 if args.pretty else None,
            ), file=sys.stdout)
            return 2

        errors: list[str] = []
        books: dict[str, Any] = {}
        for m in markets:
            try:
                books[m.ticker] = venue.orderbook(m.ticker)
            except Exception as exc:  # noqa: BLE001 - one thin market must not stop the scan
                errors.append(f"orderbook {m.ticker}: {exc}")
                continue

        # Parse errors here are informational only -- run_propose reparses
        # every market itself and folds its own `no_parseable_claim` skip.
        parse_errors: Counter[str] = Counter()
        symbols: set[str] = set()
        for m in markets:
            try:
                claim = parse_claim(m, registry)
                symbols.add(claim.underlying)
            except ClaimParseError as exc:
                parse_errors[exc.reason.value] += 1

        data = load_underlying_data(
            provider, symbols, start=now, end=now, lookback_days=args.vol_lookback_days
        )

        # -- overlay: loaded exactly as cli/backtest.py does. `config` is
        #    already loaded above (propose needs it for capital/risk/etc.,
        #    unlike backtest which only touches it here), so this reads the
        #    already-validated `vault_overlay` block rather than reloading
        #    the file; the try/except is kept for symmetry with backtest's
        #    loud-warning posture in case the attribute access itself fails.
        from tradetk.config.schema import VaultOverlayConfig
        from tradetk.overlay.loader import load_overlay
        from tradetk.overlay.verifiers import build_registry

        try:
            vault_cfg = config.vault_overlay
        except Exception as exc:  # noqa: BLE001 - a broken config must not stop propose
            log.warning("vault_overlay config unavailable (%s); overlay off", exc)
            vault_cfg = VaultOverlayConfig()

        overlay = load_overlay(
            vault_cfg, base_gate=GateLimits.from_config(config),
            base_sizing=SizingLimits.from_config(config),
            registry=build_registry(), as_of=now, now=now,
        )
        if not overlay.ok:
            print(f"warning: vault overlay unavailable, propose unmodified: "
                  f"{overlay.error}", file=sys.stderr)

        summary = run_propose(
            config=config, registry=registry, ledger_path=args.ledger,
            proposals_dir=proposals_dir, markets=markets, books=books, data=data,
            overlay=overlay, strategy=strategy, now=now,
            vol_lookback_days=args.vol_lookback_days,
        )

    summary["errors"] = errors + summary.get("errors", [])
    summary["parse_errors"] = dict(parse_errors)
    print(json.dumps(summary, indent=2 if args.pretty else None, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
