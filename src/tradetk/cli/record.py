"""``record`` — the tape daemon, and the same work on demand.

Every capability here is available as a single poll (``--once``, the default) as
well as inside the loop (``--daemon``), because no capability may exist only in a
long-running process. Output is JSON on stdout; ``--pretty`` indents it.

Records Kalshi orderbook + market metadata snapshots for eligible crypto
markets. Any history beyond the recorded window has to be accumulated by
polling and cannot be recovered afterwards.

    uv run python -m tradetk.cli.record --once --pretty
    uv run python -m tradetk.cli.record --daemon --interval 300

Stop a daemon with Ctrl-C, or by creating a ``KILL`` file in the project root.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import truststore
from dotenv import load_dotenv

from tradetk.signals.recorder import TapeSource, TapeWriter, poll_source
from tradetk.venues.books import (
    book_source,
    crypto_series,
    eligible_markets,
    market_metadata_source,
)
from tradetk.venues.kalshi import KalshiVenue

log = logging.getLogger("tradetk.cli.record")

KILL_FILE = "KILL"


def build_book_sources(
    venue: KalshiVenue, *, max_hours: float, depth: int, max_markets: int
) -> tuple[list[TapeSource], dict[str, Any]]:
    """Discover eligible crypto markets and build book + metadata sources."""
    series = crypto_series(venue, short_dated_only=True)
    tickers = [s["ticker"] for s in series if s.get("ticker")]
    markets = eligible_markets(venue, tickers, max_hours_to_close=max_hours)

    # Deepest books first: with limited polls, the markets we could actually
    # trade are worth more tape than illiquid ones.
    markets.sort(key=lambda m: float(m.volume or 0), reverse=True)
    selected = [m.ticker for m in markets[:max_markets]]

    info = {
        "crypto_series": len(tickers),
        "eligible_markets": len(markets),
        "recording_books_for": len(selected),
    }
    if not selected:
        return [], info
    # ORDER IS LOAD-BEARING: metadata BEFORE books.
    #
    # The shadow evaluator resolves a ticker's claim strictly as-of the book's
    # timestamp, and metadata recorded later is invisible by design (that guard
    # is what stops a backtest reading contract terms it could not have known).
    # With books polled first, every book in a poll predates its own poll's
    # metadata by a few seconds and can never be parsed into a claim.
    #
    # A daemon run hid this: poll N's books resolved against poll N-1's
    # metadata, so only the very first poll was lost. A `--once` run — which is
    # exactly what the scheduled sweep does — lost *everything*, silently:
    # 25 books captured, 25 skipped as `no_parseable_claim`, zero records
    # written, exit code 0.
    #
    # Recording metadata first makes the ordering true rather than lucky: the
    # terms genuinely were captured before the book, so the as-of guard is
    # satisfied honestly instead of being relaxed.
    return (
        [market_metadata_source(venue, tickers), book_source(venue, selected, depth=depth)],
        info,
    )


def poll_all(writer: TapeWriter, sources: list[TapeSource]) -> dict[str, Any]:
    """Poll every source once. A failing source is reported, not fatal — a dead
    endpoint must not stop the others from recording."""
    results = []
    for source in sources:
        try:
            outcome = poll_source(writer, source)
            results.append(outcome.as_dict())
        except Exception as exc:  # noqa: BLE001 - one bad source must not halt the tape
            log.error("source %s failed: %s", source.endpoint, exc)
            results.append({"endpoint": source.endpoint, "error": f"{type(exc).__name__}: {exc}"})

    written = sum(r.get("written", 0) or 0 for r in results)
    gaps = [r["endpoint"] for r in results if (r.get("gap") or {}).get("gap_detected")]
    errors = [r["endpoint"] for r in results if r.get("error")]

    # Tightest interval any source's measured density implies.
    suggestions = [
        s for s in (
            (r.get("coverage") or {}).get("suggested_max_interval_seconds") for r in results
        ) if s
    ]
    return {
        "polled_at": datetime.now(timezone.utc).isoformat(),
        "sources": results,
        "summary": {
            "sources_polled": len(results),
            "rows_written": written,
            "gaps_detected": gaps,
            "errors": errors,
            "suggested_interval_seconds": min(suggestions) if suggestions else None,
        },
    }


def next_interval(
    configured: float, suggested: float | None, *, floor: float, adaptive: bool
) -> tuple[float, str]:
    """Choose the next sleep, preferring the measured-density suggestion.

    Market activity swings by more than an order of magnitude, so a fixed
    interval that is safe at rest opens gaps during a burst. Never polls faster
    than `floor` (rate-limit courtesy) and never slower than configured.
    """
    if not adaptive or suggested is None:
        return configured, "configured"
    if suggested >= configured:
        return configured, "configured (density allows it)"
    chosen = max(suggested, floor)
    reason = "adapted to observed density" if chosen > floor else "floor (density very high)"
    return chosen, reason


def _kill_requested(root: Path) -> bool:
    return (root / KILL_FILE).exists()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Record raw provider responses to the tape.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Poll once and exit (default).")
    mode.add_argument("--daemon", action="store_true", help="Poll on an interval until stopped.")
    ap.add_argument("--interval", type=float, default=300.0,
                    help="Max seconds between polls in daemon mode (default 300).")
    ap.add_argument("--no-adaptive", action="store_true",
                    help="Disable shortening the interval when measured density demands it.")
    ap.add_argument("--min-interval", type=float, default=60.0,
                    help="Never poll faster than this, whatever density suggests (default 60).")
    ap.add_argument("--tape-dir", default="data/tape")
    ap.add_argument("--market-data-env", default="prod", choices=sorted(("demo", "prod")),
                    help="Environment to READ market data from. Demo has no strike data and "
                         "no depth, so prod is the default; execution is unaffected and this "
                         "path has no order endpoint.")
    ap.add_argument("--book-depth", type=int, default=10)
    ap.add_argument("--book-max-hours", type=float, default=48.0,
                    help="Only record markets closing within this many hours.")
    ap.add_argument("--book-max-markets", type=int, default=25,
                    help="Cap markets snapshotted per poll, deepest first.")
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    truststore.inject_into_ssl()
    load_dotenv()

    root = Path.cwd()
    writer = TapeWriter(args.tape_dir)
    indent = 2 if args.pretty else None

    with ExitStack() as stack:
        sources: list[TapeSource] = []
        # Read-only market data. The adapter has no order endpoint, so this
        # cannot touch execution regardless of environment.
        venue = stack.enter_context(KalshiVenue(args.market_data_env))
        book_sources, discovery = build_book_sources(
            venue, max_hours=args.book_max_hours, depth=args.book_depth,
            max_markets=args.book_max_markets,
        )
        discovery["market_data_environment"] = args.market_data_env
        sources += book_sources

        if not sources:
            print(json.dumps({"ok": False, "error": "no eligible markets to record",
                              "discovery": discovery}, indent=indent))
            return 2

        if not args.daemon:
            report = poll_all(writer, sources)
            report["discovery"] = discovery
            report["ok"] = not report["summary"]["errors"]
            print(json.dumps(report, indent=indent, default=str))
            return 0 if report["ok"] else 1

        stopping = {"flag": False}

        def _stop(signum: int, _frame: object) -> None:
            log.info("signal %s received; finishing current poll then exiting", signum)
            stopping["flag"] = True

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

        polls = 0
        while not stopping["flag"]:
            if _kill_requested(root):
                log.warning("%s file present; stopping.", KILL_FILE)
                break
            report = poll_all(writer, sources)
            report["discovery"] = discovery
            polls += 1

            sleep_for, why = next_interval(
                args.interval,
                report["summary"]["suggested_interval_seconds"],
                floor=args.min_interval,
                adaptive=not args.no_adaptive,
            )
            report["next_poll_seconds"] = sleep_for
            report["next_poll_reason"] = why
            print(json.dumps(report, indent=indent, default=str), flush=True)
            if sleep_for < args.interval:
                log.info("polling in %.0fs (%s) rather than %.0fs", sleep_for, why, args.interval)

            # Sleep in short slices so Ctrl-C and KILL are responsive.
            waited = 0.0
            while waited < sleep_for and not stopping["flag"]:
                if _kill_requested(root):
                    break
                time.sleep(min(1.0, sleep_for - waited))
                waited += 1.0

        print(json.dumps({"ok": True, "stopped": True, "polls": polls}, indent=indent))
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
