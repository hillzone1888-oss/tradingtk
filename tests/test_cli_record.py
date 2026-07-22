"""`record` CLI: adaptive interval choice and resilient multi-source polling."""

from __future__ import annotations

from tradetk.cli.record import next_interval, poll_all
from tradetk.signals.recorder import TapeSource, TapeWriter

T0 = 1784737000


def _row(ts: int) -> dict:
    return {"ts": ts, "wallet": "0xabc", "usd_amount": 1500.0}


# ── adaptive interval ──────────────────────────────────────────────


def test_uses_configured_interval_when_density_is_low() -> None:
    interval, why = next_interval(300.0, suggested=900.0, floor=60.0, adaptive=True)
    assert interval == 300.0
    assert "configured" in why


def test_shortens_interval_when_density_is_high() -> None:
    """A burst must pull the poll in, or the row cap silently loses trades."""
    interval, why = next_interval(300.0, suggested=120.0, floor=60.0, adaptive=True)
    assert interval == 120.0
    assert "adapted" in why


def test_never_polls_faster_than_floor() -> None:
    interval, why = next_interval(300.0, suggested=5.0, floor=60.0, adaptive=True)
    assert interval == 60.0
    assert "floor" in why


def test_adaptive_can_be_disabled() -> None:
    interval, why = next_interval(300.0, suggested=10.0, floor=60.0, adaptive=False)
    assert interval == 300.0
    assert why == "configured"


def test_missing_suggestion_falls_back_to_configured() -> None:
    assert next_interval(300.0, suggested=None, floor=60.0, adaptive=True)[0] == 300.0


# ── multi-source polling ───────────────────────────────────────────


def _source(name: str, rows: list[dict], *, boom: bool = False) -> TapeSource:
    def fetch():
        if boom:
            raise RuntimeError("endpoint down")
        return rows, {}

    return TapeSource(
        name=name, endpoint=f"/api/{name}", fetch=fetch, event_ts_field="ts", row_cap=250
    )


def test_poll_all_aggregates_counts(tmp_path) -> None:
    w = TapeWriter(tmp_path)
    report = poll_all(w, [_source("a", [_row(T0)]), _source("b", [_row(T0), _row(T0 + 1)])])
    assert report["summary"]["rows_written"] == 3
    assert report["summary"]["errors"] == []


def test_one_failing_source_does_not_stop_the_others(tmp_path) -> None:
    """A dead endpoint must never cost us the sources that are still alive."""
    w = TapeWriter(tmp_path)
    report = poll_all(w, [_source("dead", [], boom=True), _source("live", [_row(T0)])])
    assert report["summary"]["errors"] == ["/api/dead"]
    assert report["summary"]["rows_written"] == 1  # the healthy source still recorded


def test_summary_reports_tightest_suggested_interval(tmp_path) -> None:
    w = TapeWriter(tmp_path)
    dense = [_row(T0 + i) for i in range(61)]  # 1 row/sec -> very short coverage
    sparse = [_row(T0 + i * 600) for i in range(61)]  # 1 row/10min
    report = poll_all(w, [_source("dense", dense), _source("sparse", sparse)])
    suggestions = [
        s["coverage"]["suggested_max_interval_seconds"] for s in report["sources"]
    ]
    assert report["summary"]["suggested_interval_seconds"] == min(suggestions)
