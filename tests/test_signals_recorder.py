"""The tape: idempotent appends, gap detection, coverage math, partitioning.

These matter more than most tests in the project — the recorder captures data
that cannot be re-fetched, so a silent bug here destroys history permanently
rather than producing a wrong answer we can recompute.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tradetk.signals.recorder import (
    TapeSource,
    TapeWriter,
    coverage_estimate,
    detect_gap,
    poll_source,
    row_hash,
)

T0 = 1784737000  # epoch seconds
NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _row(ts: int, wallet: str = "0xabc", **extra) -> dict:
    return {"ts": ts, "wallet": wallet, "usd_amount": 1500.0, **extra}


# ── content hashing ────────────────────────────────────────────────


def test_hash_is_key_order_independent() -> None:
    assert row_hash({"a": 1, "b": 2}) == row_hash({"b": 2, "a": 1})


def test_hash_changes_with_content() -> None:
    assert row_hash({"a": 1}) != row_hash({"a": 2})


# ── idempotent appends ─────────────────────────────────────────────


def test_append_writes_rows(tmp_path) -> None:
    w = TapeWriter(tmp_path)
    res = w.append("/api/poly/whales", [_row(T0), _row(T0 + 1)], event_ts_field="ts", now=NOW)
    assert (res.fetched, res.written, res.duplicates) == (2, 2, 0)
    assert res.rows_in_partition == 2


def test_reappending_identical_rows_is_a_noop(tmp_path) -> None:
    """Overlapping polls are the normal case; they must not inflate the tape."""
    w = TapeWriter(tmp_path)
    rows = [_row(T0), _row(T0 + 1)]
    w.append("/api/poly/whales", rows, event_ts_field="ts", now=NOW)
    res = w.append("/api/poly/whales", rows, event_ts_field="ts", now=NOW)
    assert (res.written, res.duplicates) == (0, 2)
    assert res.rows_in_partition == 2


def test_partial_overlap_writes_only_new_rows(tmp_path) -> None:
    w = TapeWriter(tmp_path)
    w.append("/api/poly/whales", [_row(T0), _row(T0 + 1)], event_ts_field="ts", now=NOW)
    res = w.append(
        "/api/poly/whales",
        [_row(T0 + 1), _row(T0 + 2), _row(T0 + 3)],  # one seen, two new
        event_ts_field="ts", now=NOW,
    )
    assert (res.written, res.duplicates) == (2, 1)
    assert res.rows_in_partition == 4


def test_duplicates_within_one_batch_collapse(tmp_path) -> None:
    w = TapeWriter(tmp_path)
    res = w.append("/api/poly/whales", [_row(T0), _row(T0)], event_ts_field="ts", now=NOW)
    assert res.rows_in_partition == 1


def test_first_observation_time_is_preserved(tmp_path) -> None:
    """Re-seeing a row must not rewrite when we first observed it."""
    w = TapeWriter(tmp_path)
    later = NOW + timedelta(hours=3)
    w.append("/api/poly/whales", [_row(T0)], event_ts_field="ts", now=NOW)
    w.append("/api/poly/whales", [_row(T0)], event_ts_field="ts", now=later)
    frame = w.read("/api/poly/whales")
    assert len(frame) == 1
    assert frame.iloc[0]["recorded_at"].to_pydatetime().hour == NOW.hour


def test_empty_append_is_safe(tmp_path) -> None:
    w = TapeWriter(tmp_path)
    res = w.append("/api/poly/whales", [], event_ts_field="ts", now=NOW)
    assert (res.fetched, res.written) == (0, 0)


def test_payload_is_stored_raw_and_reparseable(tmp_path) -> None:
    """The tape must survive a wrong model: raw JSON can be re-parsed later."""
    import json

    w = TapeWriter(tmp_path)
    weird = {"ts": T0, "field_we_did_not_model": {"nested": [1, 2]}}
    w.append("/api/poly/whales", [weird], event_ts_field="ts", now=NOW)
    stored = json.loads(w.read("/api/poly/whales").iloc[0]["payload"])
    assert stored == weird


# ── partitioning ───────────────────────────────────────────────────


def test_partitions_by_utc_day(tmp_path) -> None:
    w = TapeWriter(tmp_path)
    day2 = NOW + timedelta(days=1)
    w.append("/api/poly/whales", [_row(T0)], event_ts_field="ts", now=NOW)
    w.append("/api/poly/whales", [_row(T0 + 99)], event_ts_field="ts", now=day2)
    files = sorted(p.name for p in (tmp_path / "api_poly_whales").glob("*.parquet"))
    assert files == ["date=2026-07-22.parquet", "date=2026-07-23.parquet"]
    assert len(w.read("/api/poly/whales")) == 2  # read spans partitions


def test_last_event_ts_spans_partitions(tmp_path) -> None:
    w = TapeWriter(tmp_path)
    w.append("/api/poly/whales", [_row(T0)], event_ts_field="ts", now=NOW)
    w.append("/api/poly/whales", [_row(T0 + 500)], event_ts_field="ts",
             now=NOW + timedelta(days=1))
    assert w.last_event_ts("/api/poly/whales") == T0 + 500


def test_last_event_ts_none_when_empty(tmp_path) -> None:
    assert TapeWriter(tmp_path).last_event_ts("/api/poly/whales") is None


# ── gap detection (pure) ───────────────────────────────────────────


def test_no_gap_on_first_ever_poll() -> None:
    g = detect_gap("e", None, [T0, T0 + 5])
    assert g.gap_detected is False
    assert g.had_prior_data is False


def test_overlap_proves_continuity() -> None:
    """Oldest row <= previous newest means we saw everything in between."""
    g = detect_gap("e", T0 + 5, [T0 + 3, T0 + 9])
    assert g.gap_detected is False
    assert g.gap_seconds is None


def test_gap_detected_when_poll_starts_after_last_record() -> None:
    g = detect_gap("e", T0, [T0 + 600, T0 + 900])
    assert g.gap_detected is True
    assert g.gap_seconds == 600.0


def test_contiguous_boundary_is_not_a_gap() -> None:
    # oldest == previous newest: the same row reappeared, so nothing was missed.
    assert detect_gap("e", T0, [T0, T0 + 5]).gap_detected is False


def test_empty_poll_reports_no_gap() -> None:
    g = detect_gap("e", T0, [])
    assert g.gap_detected is False
    assert g.poll_oldest_ts is None


# ── coverage math ──────────────────────────────────────────────────


def test_coverage_from_observed_density() -> None:
    # 61 rows spanning 3600s => 60 rows/hour; a 240-row cap covers 4h.
    stamps = [T0 + i * 60 for i in range(61)]
    cov = coverage_estimate(stamps, row_cap=240)
    assert cov["rows_per_hour"] == pytest.approx(60.0)
    assert cov["cap_coverage_hours"] == pytest.approx(4.0)
    # Suggested interval is a quarter of the window, for spike headroom.
    assert cov["suggested_max_interval_seconds"] == 3600


def test_coverage_degrades_gracefully_on_thin_data() -> None:
    assert coverage_estimate([], 250)["cap_coverage_hours"] is None
    assert coverage_estimate([T0], 250)["cap_coverage_hours"] is None
    assert coverage_estimate([T0, T0], 250)["cap_coverage_hours"] is None  # zero span


# ── poll_source wiring ─────────────────────────────────────────────


def _source(rows: list[dict], cap: int | None = 250) -> TapeSource:
    return TapeSource(
        name="whales", endpoint="/api/poly/whales",
        fetch=lambda: (rows, {"full_access": False}),
        event_ts_field="ts", row_cap=cap,
    )


def test_poll_source_records_and_reports(tmp_path) -> None:
    w = TapeWriter(tmp_path)
    out = poll_source(w, _source([_row(T0), _row(T0 + 60)]), now=NOW)
    assert out.append.written == 2
    assert out.gap.gap_detected is False
    assert out.envelope == {"full_access": False}


def test_poll_source_detects_gap_against_prior_tape(tmp_path) -> None:
    w = TapeWriter(tmp_path)
    poll_source(w, _source([_row(T0)]), now=NOW)
    out = poll_source(w, _source([_row(T0 + 7200)]), now=NOW)  # 2h later, no overlap
    assert out.gap.gap_detected is True
    assert out.gap.gap_seconds == 7200.0


def test_second_poll_with_overlap_reports_no_gap(tmp_path) -> None:
    w = TapeWriter(tmp_path)
    poll_source(w, _source([_row(T0), _row(T0 + 60)]), now=NOW)
    out = poll_source(w, _source([_row(T0 + 60), _row(T0 + 120)]), now=NOW)
    assert out.gap.gap_detected is False
    assert out.append.written == 1  # only the genuinely new row
