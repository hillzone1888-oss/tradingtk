"""Cross-validate data providers and report reachability / discrepancies.

Prints structured JSON to stdout (``--pretty`` for humans). Exit code is non-zero
if a provider is unreachable so it can gate a run.

Design note: the spec's rule is "any signal computable from native data should be
computed from native data," and this script is where overlapping signals get
diffed. Right now the Moon Dev provider only implements the **Polymarket GLOBAL
flow** family, which has *no* native Hyperliquid equivalent — so there is nothing
to cross-check numerically yet, and the script says so plainly rather than
fabricating an agreement metric. The `COMPARATORS` registry is where HL-derived
Moon Dev signals (liquidations, HLP, order-flow) will plug in once implemented.

Usage:
    uv run python scripts/validate_provider.py --symbol BTC --pretty
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import truststore

from tradetk.signals.hyperliquid import HyperliquidProvider
from tradetk.signals.moondev import MoonDevProvider

# (name, description) -> callable(hl, md, symbol) returning a discrepancy dict.
# Empty until an HL-derived Moon Dev signal is implemented to diff against native.
COMPARATORS: list[tuple[str, str]] = []


def _check_hyperliquid(symbol: str) -> dict[str, Any]:
    try:
        with HyperliquidProvider() as hl:
            snap = hl.mid_price(symbol)
        return {"reachable": True, "symbol": snap.symbol, "mid": snap.mid}
    except Exception as exc:  # noqa: BLE001 - report, don't crash the whole check
        return {"reachable": False, "error": f"{type(exc).__name__}: {exc}"}


def _check_moondev() -> dict[str, Any]:
    key = os.environ.get("MOONDEV_API_KEY")
    try:
        with MoonDevProvider(api_key=key) as md:
            health = md.poly_health()  # public, no key required
        return {
            "reachable": True,
            "status": health.status,
            "queue_depth": health.queue_depth,
            "uptime_minutes": health.uptime_minutes,
            "api_key_present": bool(key),
        }
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False, "error": f"{type(exc).__name__}: {exc}"}


def run(symbol: str) -> dict[str, Any]:
    truststore.inject_into_ssl()  # OS trust store for the corporate/MITM CA
    hl = _check_hyperliquid(symbol)
    md = _check_moondev()
    return {
        "symbol": symbol,
        "providers": {"hyperliquid": hl, "moondev": md},
        "comparisons": [] if not COMPARATORS else None,
        "note": (
            "No overlapping native-computable Moon Dev signal is implemented yet "
            "(only Polymarket GLOBAL flow, which has no native HL equivalent). "
            "Numeric cross-checks activate when HL-derived Moon Dev endpoints land."
        ),
        "ok": hl["reachable"] and md["reachable"],
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Cross-validate data providers.")
    ap.add_argument("--symbol", default="BTC")
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args(argv)

    result = run(args.symbol)
    print(json.dumps(result, indent=2 if args.pretty else None))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
