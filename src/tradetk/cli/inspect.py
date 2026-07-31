"""``inspect`` — everything known about one contract, with the full cost breakdown.

Deep-dives a single market: the parsed claim, the live book and its depth, and
what it would actually cost to take a position — fees, spread, and book-walking
slippage, all in probability points so they can be compared directly against an
edge estimate.

    uv run python -m tradetk.cli.inspect KXBTCD-26JUL2214-T75299.99 --pretty
    uv run python -m tradetk.cli.inspect <ticker> --stake 2 --pretty

Read-only. No probability estimate is made here — that is step 8's job, and this
command deliberately reports only what is measured.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import truststore

from tradetk.costs.fees import FeeRounding, KalshiFeeModel
from tradetk.costs.spread import round_trip_cost
from tradetk.translation.claims import ClaimParseError, UnderlyingRegistry, parse_claim
from tradetk.venues.kalshi import KalshiVenue

log = logging.getLogger("tradetk.cli.inspect")


def contracts_for_stake(stake: Decimal, price: Decimal, fee_per_contract: Decimal) -> int:
    """``floor(stake / (price + fee_per_contract))``, minimum 1.

    Sizing is in **contracts**, never dollars: contracts are indivisible, so the
    dollar target is a constraint and the contract count is the decision. The
    caller still has to check that even one contract clears the per-position
    ceiling — that gate lives in the risk module, not here.
    """
    unit = price + fee_per_contract
    if unit <= 0:
        return 1
    return max(1, int(stake / unit))


def inspect_market(
    venue: KalshiVenue,
    registry: UnderlyingRegistry,
    ticker: str,
    *,
    stake: Decimal,
    fee_model: KalshiFeeModel,
    depth: int = 10,
    now: datetime | None = None,
) -> dict[str, Any]:
    ref = now or datetime.now(timezone.utc)
    market = venue.market(ticker)
    book = venue.orderbook(ticker, depth=depth)

    claim_block: dict[str, Any]
    try:
        claim = parse_claim(market, registry)
        claim_block = {
            "eligible": True,
            "underlying": claim.underlying,
            "operator": claim.operator.value,
            "threshold": str(claim.threshold) if claim.threshold is not None else None,
            "lower_bound": str(claim.lower_bound) if claim.lower_bound is not None else None,
            "upper_bound": str(claim.upper_bound) if claim.upper_bound is not None else None,
            "resolution_time": claim.resolution_time.isoformat(),
            "hours_to_resolution": round(claim.hours_to_resolution(ref), 3),
            "resolution_source": claim.resolution_source,
            "reference_is_measured": claim.reference_is_measured,
            "description": claim.describe(),
        }
    except ClaimParseError as exc:
        claim_block = {"eligible": False, "reason": exc.reason.value, "detail": exc.detail}

    ask = book.best_yes_ask
    sizing: dict[str, Any] = {"stake_target": str(stake)}
    costs: dict[str, Any] = {}

    if ask is not None:
        per_contract_fee = fee_model.fee(1, ask)
        n = contracts_for_stake(stake, ask, per_contract_fee)
        sizing.update(
            {
                "price_used": str(ask),
                "note": "priced at the ask — what a buyer actually pays, never the mid",
                "fee_per_contract": str(per_contract_fee),
                "contracts": n,
                "dollars_deployed": str(n * ask),
                "quantisation_shortfall": str(stake - (n * ask)),
            }
        )
        rt = round_trip_cost(book, n, fee_model)
        costs = {
            "taker": rt.as_dict(),
            "maker_if_rested": round_trip_cost(book, n, fee_model, is_maker=True).as_dict(),
            "fee_pct_of_stake_taker": f"{fee_model.cost_pct_of_stake(ask):.4%}",
            "fee_pct_of_stake_maker": f"{fee_model.cost_pct_of_stake(ask, is_maker=True):.4%}",
        }
    else:
        sizing["note"] = "no ask available — nothing to buy at any price"

    return {
        "inspected_at": ref.isoformat(),
        "environment": venue.environment,
        "ticker": ticker,
        "market": {
            "title": market.title,
            "status": market.status,
            "series_ticker": market.series_ticker,
            "volume": str(market.volume) if market.volume is not None else None,
        },
        "claim": claim_block,
        "book": {
            "best_yes_bid": str(book.best_yes_bid) if book.best_yes_bid is not None else None,
            "best_yes_ask": str(ask) if ask is not None else None,
            "spread": str(book.spread) if book.spread is not None else None,
            "yes_bid_levels": [[str(lv.price), str(lv.size)] for lv in book.yes_bids[:5]],
            "yes_ask_levels": [[str(lv.price), str(lv.size)] for lv in book.yes_asks[:5]],
            "ask_side_depth_contracts": str(sum(lv.size for lv in book.yes_asks)),
            "bid_side_depth_contracts": str(sum(lv.size for lv in book.yes_bids)),
            "is_crossed": book.is_crossed(),
        },
        "sizing": sizing,
        "costs": costs,
        "fee_model": fee_model.verify_against_published_table(),
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Deep-dive one contract with a cost breakdown.")
    ap.add_argument("ticker")
    ap.add_argument("--env", default="prod", choices=("demo", "prod"))
    ap.add_argument("--registry", default="config/underlyings.yaml")
    ap.add_argument("--stake", type=str, default="2",
                    help="Dollar sizing target for the contract count (default 2).")
    ap.add_argument("--depth", type=int, default=10)
    ap.add_argument("--rounding", default="cent", choices=[r.value for r in FeeRounding],
                    help="Fee rounding granularity; cent is the conservative default.")
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    truststore.inject_into_ssl()

    registry = UnderlyingRegistry.from_yaml(args.registry)
    fee_model = KalshiFeeModel(rounding=FeeRounding(args.rounding))

    with KalshiVenue(args.env) as venue:
        result = inspect_market(
            venue, registry, args.ticker, stake=Decimal(args.stake),
            fee_model=fee_model, depth=args.depth,
        )

    print(json.dumps(result, indent=2 if args.pretty else None, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
