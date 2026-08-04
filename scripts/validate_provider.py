"""Report Hyperliquid reachability. Non-zero exit if unreachable, so it can gate a run."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import truststore

from tradetk.signals.hyperliquid import HyperliquidProvider


def _check_hyperliquid(symbol: str) -> dict[str, Any]:
    try:
        with HyperliquidProvider() as hl:
            snap = hl.mid_price(symbol)
        return {"reachable": True, "symbol": snap.symbol, "mid": snap.mid}
    except Exception as exc:  # noqa: BLE001 - report, don't crash the check
        return {"reachable": False, "error": f"{type(exc).__name__}: {exc}"}


def run(symbol: str) -> dict[str, Any]:
    truststore.inject_into_ssl()  # OS trust store for the corporate/MITM CA
    hl = _check_hyperliquid(symbol)
    return {"symbol": symbol, "providers": {"hyperliquid": hl}, "ok": hl["reachable"]}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Check Hyperliquid reachability.")
    ap.add_argument("--symbol", default="BTC")
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args(argv)
    result = run(args.symbol)
    print(json.dumps(result, indent=2 if args.pretty else None))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
