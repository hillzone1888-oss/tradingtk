"""The tape — an append-only record of raw provider responses.

This module exists because the data it captures **cannot be bought later**:

* Moon Dev's whale log is capped at 250 rows on a standard key. At the observed
  ~1,373 whale trades/day that is roughly **4 hours** of coverage, and the
  ``days`` parameter cannot reach further back than the cap allows. History is
  therefore accumulated by polling, never queried.
* Prediction-market orderbook *depth* history is essentially unavailable at any
  price. The backtest needs it to model realistic fills.

Design decisions and why:

**Rows are stored raw, not parsed.** A wrong pydantic model silently corrupts
the tape forever; raw JSON can be re-parsed once the model is fixed. This is not
hypothetical — ``PolyDailyRollup`` shipped with field names taken from prose
rather than the live API and returned ``None`` for every value.

**Dedup is content-addressed.** Overlapping polls re-deliver the same rows, so
each row carries a sha256 of its canonical JSON. Re-recording is idempotent,
which means a crash-and-restart or an over-eager poll costs nothing.

**Gaps are detected and recorded, never papered over.** If a poll's oldest row
is newer than the newest row already on tape, trades happened that we will never
see. That is a permanent hole in the sample, and every downstream report must be
able to find out. We record it rather than discovering it later as a mystery.

The writer is endpoint-agnostic so the orderbook snapshots (which need the venue
adapter) append to the same tape with the same guarantees.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd

log = logging.getLogger("tradetk.signals.recorder")

# Tape schema. Deliberately narrow and generic: anything endpoint-specific lives
# inside `payload` so a new source needs no migration.
TAPE_COLUMNS = ["recorded_at", "endpoint", "row_hash", "event_ts", "payload"]


def row_hash(row: dict[str, Any]) -> str:
    """Stable content hash of a raw row (order-independent, 128-bit)."""
    canonical = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _event_ts_of(row: dict[str, Any], field_name: str | None) -> int | None:
    """Extract the event timestamp used for gap detection, if the source has one."""
    if field_name is None:
        return None
    value = row.get(field_name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


@dataclass(frozen=True)
class AppendResult:
    """Outcome of one append. `written` counts genuinely new rows."""

    endpoint: str
    fetched: int
    written: int
    duplicates: int
    path: str | None
    rows_in_partition: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "fetched": self.fetched,
            "written": self.written,
            "duplicates": self.duplicates,
            "path": self.path,
            "rows_in_partition": self.rows_in_partition,
        }


@dataclass(frozen=True)
class GapReport:
    """Whether this poll is contiguous with what is already on tape.

    `gap_seconds` is the span between the newest row previously recorded and the
    oldest row in this poll. A positive value means trades occurred in between
    that the row cap prevented us from ever seeing.
    """

    endpoint: str
    had_prior_data: bool
    previous_newest_ts: int | None
    poll_oldest_ts: int | None
    poll_newest_ts: int | None
    gap_detected: bool
    gap_seconds: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "had_prior_data": self.had_prior_data,
            "previous_newest_ts": self.previous_newest_ts,
            "poll_oldest_ts": self.poll_oldest_ts,
            "poll_newest_ts": self.poll_newest_ts,
            "gap_detected": self.gap_detected,
            "gap_seconds": self.gap_seconds,
        }


def detect_gap(
    endpoint: str,
    previous_newest_ts: int | None,
    event_timestamps: Iterable[int],
) -> GapReport:
    """Compare a poll's coverage against the newest row already on tape.

    Pure: no IO, so the gap logic is testable without a filesystem.
    """
    stamps = sorted(int(t) for t in event_timestamps)
    oldest = stamps[0] if stamps else None
    newest = stamps[-1] if stamps else None

    gap = False
    gap_seconds: float | None = None
    if previous_newest_ts is not None and oldest is not None:
        # Strictly greater: an overlap (oldest <= previous newest) proves
        # continuity, which is exactly what a safe poll interval buys us.
        if oldest > previous_newest_ts:
            gap = True
            gap_seconds = float(oldest - previous_newest_ts)

    return GapReport(
        endpoint=endpoint,
        had_prior_data=previous_newest_ts is not None,
        previous_newest_ts=previous_newest_ts,
        poll_oldest_ts=oldest,
        poll_newest_ts=newest,
        gap_detected=gap,
        gap_seconds=gap_seconds,
    )


def coverage_estimate(
    event_timestamps: Iterable[int], row_cap: int
) -> dict[str, Any]:
    """Estimate how long the row cap covers, from the poll's own density.

    Answers the operational question "how often must I poll to avoid gaps?"
    using observed data rather than an assumption.
    """
    stamps = sorted(int(t) for t in event_timestamps)
    if len(stamps) < 2:
        return {"rows": len(stamps), "span_seconds": None, "rows_per_hour": None,
                "cap_coverage_hours": None, "suggested_max_interval_seconds": None}

    span = stamps[-1] - stamps[0]
    if span <= 0:
        return {"rows": len(stamps), "span_seconds": 0, "rows_per_hour": None,
                "cap_coverage_hours": None, "suggested_max_interval_seconds": None}

    rows_per_hour = (len(stamps) - 1) * 3600.0 / span
    cap_hours = row_cap / rows_per_hour if rows_per_hour > 0 else None
    # Poll at <= 1/4 of the coverage window so a volume spike cannot open a gap.
    suggested = cap_hours * 3600.0 / 4.0 if cap_hours else None
    return {
        "rows": len(stamps),
        "span_seconds": span,
        "rows_per_hour": round(rows_per_hour, 1),
        "cap_coverage_hours": round(cap_hours, 2) if cap_hours else None,
        "suggested_max_interval_seconds": int(suggested) if suggested else None,
    }


class TapeWriter:
    """Append-only, date-partitioned parquet tape with idempotent writes.

    Layout: ``<tape_dir>/<endpoint>/date=YYYY-MM-DD.parquet`` (UTC). Partitioning
    by day keeps each file small enough to rewrite atomically on append, which is
    what makes dedup cheap without a database.
    """

    def __init__(self, tape_dir: str | Path) -> None:
        self._dir = Path(tape_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def partition_path(self, endpoint: str, when: datetime) -> Path:
        safe = endpoint.strip("/").replace("/", "_")
        day = when.astimezone(timezone.utc).strftime("%Y-%m-%d")
        return self._dir / safe / f"date={day}.parquet"

    def append(
        self,
        endpoint: str,
        rows: list[dict[str, Any]],
        *,
        event_ts_field: str | None = None,
        now: datetime | None = None,
    ) -> AppendResult:
        """Append raw `rows`, skipping any whose content hash is already present."""
        stamp = now or datetime.now(timezone.utc)
        if not rows:
            return AppendResult(endpoint, 0, 0, 0, None, self.row_count(endpoint, stamp))

        frame = pd.DataFrame(
            [
                {
                    "recorded_at": stamp,
                    "endpoint": endpoint,
                    "row_hash": row_hash(r),
                    "event_ts": _event_ts_of(r, event_ts_field),
                    "payload": json.dumps(r, sort_keys=True, separators=(",", ":"), default=str),
                }
                for r in rows
            ],
            columns=TAPE_COLUMNS,
        )
        # Within-batch duplicates are possible if the API repeats a row.
        frame = frame.drop_duplicates(subset="row_hash", keep="first")

        path = self.partition_path(endpoint, stamp)
        path.parent.mkdir(parents=True, exist_ok=True)

        before = 0
        if path.exists():
            existing = pd.read_parquet(path)
            before = len(existing)
            frame = pd.concat([existing, frame], ignore_index=True)

        # keep="first" preserves the ORIGINAL observation time for a repeated row
        # — the tape records when we first saw it, not when we last re-saw it.
        merged = frame.drop_duplicates(subset="row_hash", keep="first").reset_index(drop=True)
        merged.to_parquet(path, index=False)

        written = len(merged) - before
        return AppendResult(
            endpoint=endpoint,
            fetched=len(rows),
            written=written,
            duplicates=len(rows) - written,
            path=str(path),
            rows_in_partition=len(merged),
        )

    def row_count(self, endpoint: str, when: datetime | None = None) -> int:
        path = self.partition_path(endpoint, when or datetime.now(timezone.utc))
        return len(pd.read_parquet(path)) if path.exists() else 0

    def last_event_ts(self, endpoint: str) -> int | None:
        """Newest `event_ts` recorded for `endpoint` across all partitions.

        Scans every partition because a poll may straddle a UTC midnight.
        """
        safe = endpoint.strip("/").replace("/", "_")
        directory = self._dir / safe
        if not directory.exists():
            return None
        best: int | None = None
        for path in sorted(directory.glob("date=*.parquet")):
            frame = pd.read_parquet(path, columns=["event_ts"])
            series = frame["event_ts"].dropna()
            if series.empty:
                continue
            candidate = int(series.max())
            best = candidate if best is None else max(best, candidate)
        return best

    def read(self, endpoint: str) -> pd.DataFrame:
        """All recorded rows for `endpoint`, oldest observation first."""
        safe = endpoint.strip("/").replace("/", "_")
        directory = self._dir / safe
        if not directory.exists():
            return pd.DataFrame(columns=TAPE_COLUMNS)
        frames = [pd.read_parquet(p) for p in sorted(directory.glob("date=*.parquet"))]
        if not frames:
            return pd.DataFrame(columns=TAPE_COLUMNS)
        return pd.concat(frames, ignore_index=True).sort_values("recorded_at").reset_index(drop=True)


@dataclass(frozen=True)
class TapeSource:
    """One pollable endpoint.

    `fetch` returns raw rows; keeping it a plain callable means the venue's
    orderbook snapshots plug in later without touching the recorder loop.
    """

    name: str
    endpoint: str
    fetch: Callable[[], tuple[list[dict[str, Any]], dict[str, Any]]]
    event_ts_field: str | None = None
    row_cap: int | None = None


@dataclass
class PollOutcome:
    append: AppendResult
    gap: GapReport
    coverage: dict[str, Any] = field(default_factory=dict)
    envelope: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.append.as_dict(),
            "gap": self.gap.as_dict(),
            "coverage": self.coverage,
            "envelope": self.envelope,
            "error": self.error,
        }


def poll_source(
    writer: TapeWriter, source: TapeSource, *, now: datetime | None = None
) -> PollOutcome:
    """Fetch one source, detect gaps against the tape, and append.

    Gap detection reads the prior high-water mark *before* the append, since
    appending would otherwise move the mark we are comparing against.
    """
    previous = writer.last_event_ts(source.endpoint) if source.event_ts_field else None
    rows, meta = source.fetch()

    stamps = [
        ts for ts in (_event_ts_of(r, source.event_ts_field) for r in rows) if ts is not None
    ]
    gap = detect_gap(source.endpoint, previous, stamps)
    if gap.gap_detected:
        log.warning(
            "TAPE GAP on %s: %.0fs of flow between the last recorded row and this "
            "poll's oldest row was never captured and cannot be recovered. "
            "Reduce the poll interval.",
            source.endpoint, gap.gap_seconds or 0.0,
        )

    result = writer.append(
        source.endpoint, rows, event_ts_field=source.event_ts_field, now=now
    )
    coverage = coverage_estimate(stamps, source.row_cap) if source.row_cap else {}

    if source.row_cap and result.fetched >= source.row_cap:
        log.info(
            "%s returned %d rows at the %d-row cap — the true history is deeper "
            "than one poll can reach; only continuous polling extends it.",
            source.endpoint, result.fetched, source.row_cap,
        )

    return PollOutcome(append=result, gap=gap, coverage=coverage, envelope=meta)
