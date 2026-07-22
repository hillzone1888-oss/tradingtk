"""``record`` — the tape daemon, and the same work on demand.

Every capability here is available as a single poll (``--once``, the default) as
well as inside the loop (``--daemon``), because no capability may exist only in a
long-running process. Output is JSON on stdout; ``--pretty`` indents it.

Run this early and keep it running. The Moon Dev whale log reaches back only as
far as its row cap allows (~4 hours on a standard key), so any history beyond
that has to be accumulated by polling and cannot be recovered afterwards.

    uv run python -m tradetk.cli.record --once --pretty
    uv run python -m tradetk.cli.record --daemon --interval 900

Stop a daemon with Ctrl-C, or by creating a ``KILL`` file in the project root.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import truststore
from dotenv import load_dotenv

from tradetk.signals.moondev import TIER_CAPS, MoonDevProvider
from tradetk.signals.recorder import TapeSource, TapeWriter, poll_source

log = logging.getLogger("tradetk.cli.record")

KILL_FILE = "KILL"

# endpoint -> (event-timestamp field, tier-cap key). Sources without an event
# timestamp are point-in-time aggregates: still worth recording, but gap
# detection does not apply to them.
MOONDEV_SOURCES: dict[str, tuple[str | None, str | None]] = {
    "/api/poly/whales": ("ts", "whales"),
    "/api/poly/whales/daily": (None, None),
    "/api/poly/whales/top-traders": (None, "leaderboard"),
    "/api/poly/whales/top-markets": (None, "leaderboard"),
    "/api/poly/profitable-traders": (None, "profitable_traders"),
}


def build_moondev_sources(
    provider: MoonDevProvider, tier: str, selected: list[str] | None
) -> list[TapeSource]:
    """Wrap the Moon Dev endpoints as pollable tape sources."""
    sources: list[TapeSource] = []
    for endpoint, (ts_field, cap_key) in MOONDEV_SOURCES.items():
        if selected and not any(s in endpoint for s in selected):
            continue
        cap = TIER_CAPS[tier][cap_key] if cap_key else None
        params: dict[str, Any] = {}
        if cap_key == "whales":
            # Ask for the full cap every poll: overlap with the previous poll is
            # what proves continuity, and unused rows are free.
            params = {"limit": cap, "min_usd": 1000}
        elif cap:
            params = {"limit": cap}

        sources.append(
            TapeSource(
                name=endpoint.rsplit("/", 1)[-1],
                endpoint=endpoint,
                fetch=lambda e=endpoint, p=params: provider.fetch_raw(e, p),
                event_ts_field=ts_field,
                row_cap=cap,
            )
        )
    return sources


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

    Whale density swings by more than an order of magnitude, so a fixed interval
    that is safe at rest opens gaps during a burst. Never polls faster than
    `floor` (rate-limit courtesy) and never slower than configured.
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
    ap.add_argument("--tier", default="standard", choices=sorted(TIER_CAPS))
    ap.add_argument("--source", action="append",
                    help="Substring of an endpoint to record; repeatable. Default: all.")
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    truststore.inject_into_ssl()
    load_dotenv()

    key = os.environ.get("MOONDEV_API_KEY")
    if not key:
        print(json.dumps({"ok": False,
                          "error": "MOONDEV_API_KEY is not set; nothing to record."}))
        return 2

    root = Path.cwd()
    writer = TapeWriter(args.tape_dir)
    indent = 2 if args.pretty else None

    with MoonDevProvider(api_key=key, tier=args.tier) as provider:
        sources = build_moondev_sources(provider, args.tier, args.source)
        if not sources:
            print(json.dumps({"ok": False, "error": "no sources matched --source"}, indent=indent))
            return 2

        if not args.daemon:
            report = poll_all(writer, sources)
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
