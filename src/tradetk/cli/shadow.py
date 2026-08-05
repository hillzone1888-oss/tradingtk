"""``shadow`` — score every eligible market without trading any of it.

    uv run python -m tradetk.cli.shadow                     # score + append to the log
    uv run python -m tradetk.cli.shadow --dry-run --pretty  # score, write nothing
    uv run python -m tradetk.cli.shadow --stats             # what the log holds

This is the sample-size engine. Six slots against thousands of eligible
contracts means trading your way to a readable sample takes months; scoring
every contract costs nothing and produces hundreds of testable forecasts a day.

Writes are idempotent on ``(ticker, observed_at, strategy)``, so re-running over
the same tape does not inflate the sample.

Read-only with respect to the venue. It touches market data and the local log,
and nothing else.
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
from tradetk.costs.fees import FeeRounding, KalshiFeeModel
from tradetk.shadow.evaluator import ShadowEvaluator
from tradetk.shadow.records import ShadowStore
from tradetk.signals.hyperliquid import HyperliquidProvider
from tradetk.strategy import available_strategies, get_strategy
from tradetk.translation.claims import UnderlyingRegistry
from tradetk.translation.edge import GateLimits
from tradetk.translation.sizing import SizingLimits

log = logging.getLogger("tradetk.cli.shadow")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Score the full eligible universe and append to the shadow log."
    )
    ap.add_argument("--tape-dir", default="data/tape")
    ap.add_argument("--shadow-dir", default="data/shadow")
    ap.add_argument("--registry", default="config/underlyings.yaml")
    ap.add_argument("--config", default="config/config.yaml",
                    help="Toolkit config; read only for the vault_overlay block.")
    ap.add_argument(
        "--strategy", default="baseline_vol",
        help=f"One of: {', '.join(available_strategies()) or '(none)'}",
    )
    ap.add_argument("--vol-multiplier", type=float, default=1.0)
    ap.add_argument("--vol-lookback-days", type=int, default=30)

    ap.add_argument("--min-edge-pp", type=float, default=3.0)
    ap.add_argument("--margin-pp", type=float, default=1.0)
    ap.add_argument("--min-depth-multiple", type=float, default=5.0)
    ap.add_argument("--max-participation-pct", type=float, default=10.0)
    ap.add_argument("--max-hours", type=float, default=168.0)
    ap.add_argument("--allow-deep-tail", action="store_true")
    ap.add_argument("--rounding", default="cent", choices=[r.value for r in FeeRounding])

    ap.add_argument("--position-target", type=str, default="2.00")
    ap.add_argument("--per-position-ceiling", type=str, default="3.00")
    ap.add_argument("--total-capital", type=str, default="20.00")

    ap.add_argument("--dry-run", action="store_true", help="Score but write nothing.")
    ap.add_argument("--stats", action="store_true", help="Show what the log holds and exit.")
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr
    )
    truststore.inject_into_ssl()

    store = ShadowStore(args.shadow_dir)
    indent = 2 if args.pretty else None

    if args.stats:
        print(json.dumps(store.summary(), indent=indent, default=str))
        return 0

    registry = UnderlyingRegistry.from_yaml(args.registry)
    try:
        replay = TapeReplay.from_tape(args.tape_dir)
    except ReplayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    start, end = replay.span
    symbols = {
        claim.underlying
        for ticker in replay.tickers
        if (claim := replay.claim_as_of(ticker, end, registry)) is not None
    }
    with HyperliquidProvider() as provider:
        data = load_underlying_data(
            provider, symbols, start=start,
            end=min(end, datetime.now(timezone.utc)),
            lookback_days=args.vol_lookback_days,
        )

    from tradetk.config.schema import VaultOverlayConfig
    from tradetk.overlay.loader import load_overlay
    from tradetk.overlay.verifiers import build_registry

    gate = GateLimits(
        min_net_edge_pp=Decimal(str(args.min_edge_pp)),
        margin_pp=Decimal(str(args.margin_pp)),
        min_book_depth_multiple=Decimal(str(args.min_depth_multiple)),
        max_book_participation_pct=Decimal(str(args.max_participation_pct)),
        max_hours_to_resolution=Decimal(str(args.max_hours)),
        reject_deep_tail=not args.allow_deep_tail,
    )
    sizing = SizingLimits(
        position_target=Decimal(args.position_target),
        per_position_ceiling=Decimal(args.per_position_ceiling),
        total_capital=Decimal(args.total_capital),
        max_book_participation_pct=Decimal(str(args.max_participation_pct)),
    )
    try:
        from tradetk.config.loader import load_config

        vault_cfg = load_config(args.config).vault_overlay
    except Exception as exc:  # noqa: BLE001 - a broken config must not stop shadow
        log.info("vault_overlay config unavailable (%s); overlay off", exc)
        vault_cfg = VaultOverlayConfig()
    overlay = load_overlay(
        vault_cfg, base_gate=gate, base_sizing=sizing,
        registry=build_registry(), as_of=start, now=start,
    )
    if not overlay.ok:
        print(f"warning: vault overlay unavailable, annotations off: "
              f"{overlay.error}", file=sys.stderr)

    evaluator = ShadowEvaluator(
        strategy=get_strategy(args.strategy, vol_multiplier=args.vol_multiplier),
        registry=registry,
        data=data,
        fee_model=KalshiFeeModel(rounding=FeeRounding(args.rounding)),
        gate_limits=gate,
        sizing_limits=sizing,
        vol_lookback_days=args.vol_lookback_days,
        overlay=overlay,
    )
    run = evaluator.run(replay)

    payload = run.summary()
    if args.dry_run:
        payload["write"] = {"skipped": True, "reason": "--dry-run"}
    else:
        payload["write"] = store.append(run.records)
        payload["log"] = store.summary()

    payload["vault_overlay"] = overlay.as_dict()

    print(json.dumps(payload, indent=indent, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
