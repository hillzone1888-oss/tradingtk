"""``scan`` — list eligible markets, and account for every one excluded.

At $20 with a handful of slots, the rejection log is more informative than the
trade log, so exclusions are first-class output here rather than a debug aside:
every filtered market is counted by reason with a worked example.

    uv run python -m tradetk.cli.scan --pretty
    uv run python -m tradetk.cli.scan --underlying BTC --max-hours 6 --pretty
    uv run python -m tradetk.cli.scan --show-rejected --pretty

Read-only. It touches market data and nothing else.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

import truststore

from tradetk.translation.claims import UnderlyingRegistry, parse_claims
from tradetk.venues.books import crypto_series, eligible_markets
from tradetk.venues.kalshi import KalshiVenue

log = logging.getLogger("tradetk.cli.scan")


def scan(
    venue: KalshiVenue,
    registry: UnderlyingRegistry,
    *,
    max_hours: float,
    underlyings: list[str] | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Discover markets, parse them into claims, and report both sides."""
    ref = now or datetime.now(timezone.utc)
    series = crypto_series(venue, short_dated_only=True)
    tickers = [s["ticker"] for s in series if s.get("ticker")]

    # Horizon filtering happens here; claim parsing decides everything else.
    markets = eligible_markets(
        venue, tickers, max_hours_to_close=max_hours,
        require_machine_readable_strike=False,  # let the parser explain rejections
        now=ref,
    )
    report = parse_claims(markets, registry)

    claims = report.claims
    if underlyings:
        wanted = {u.upper() for u in underlyings}
        claims = [c for c in claims if c.underlying in wanted]

    rows = [
        {
            "ticker": c.ticker,
            "underlying": c.underlying,
            "operator": c.operator.value,
            "threshold": str(c.threshold) if c.threshold is not None else None,
            "lower_bound": str(c.lower_bound) if c.lower_bound is not None else None,
            "upper_bound": str(c.upper_bound) if c.upper_bound is not None else None,
            "hours_to_resolution": round(c.hours_to_resolution(ref), 2),
            "resolution_source": c.resolution_source,
            "reference_is_measured": c.reference_is_measured,
            "claim": c.describe(),
        }
        for c in sorted(claims, key=lambda x: x.resolution_time)
    ]

    by_underlying: dict[str, int] = {}
    for c in claims:
        by_underlying[c.underlying] = by_underlying.get(c.underlying, 0) + 1

    return {
        "scanned_at": ref.isoformat(),
        "market_data_environment": venue.environment,
        "universe": {
            "crypto_series": len(tickers),
            "markets_considered": len(markets),
            "eligible_claims": report.eligible_count,
            "shown_after_filters": len(rows),
            "by_underlying": dict(sorted(by_underlying.items())),
        },
        "excluded": report.as_dict(),
        "markets": rows,
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="List eligible markets and why others were excluded.")
    ap.add_argument("--env", default="prod", choices=("demo", "prod"),
                    help="Market-data environment to read (default prod; demo has no strikes).")
    ap.add_argument("--registry", default="config/underlyings.yaml")
    ap.add_argument("--max-hours", type=float, default=48.0)
    ap.add_argument("--underlying", action="append",
                    help="Restrict output to these assets; repeatable.")
    ap.add_argument("--limit", type=int, default=50, help="Max markets listed (0 = all).")
    ap.add_argument("--show-rejected", action="store_true",
                    help="Include the per-reason rejection detail in the output.")
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    truststore.inject_into_ssl()

    registry = UnderlyingRegistry.from_yaml(args.registry)
    indent = 2 if args.pretty else None

    with KalshiVenue(args.env) as venue:
        result = scan(venue, registry, max_hours=args.max_hours, underlyings=args.underlying)

    if args.limit:
        result["markets"] = result["markets"][: args.limit]
    if not args.show_rejected:
        result["excluded"].pop("rejected_examples", None)

    print(json.dumps(result, indent=indent, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
