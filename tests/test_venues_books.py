"""Orderbook tape: eligibility filtering and the event-vs-state-sample rule."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from tradetk.signals.recorder import TapeWriter, poll_source
from tradetk.venues.base import BinaryBook, BookLevel, VenueMarket
from tradetk.venues.books import book_source, eligible_markets, market_metadata_source

NOW = dt.datetime(2026, 7, 22, 12, 0, tzinfo=dt.timezone.utc)


def _market(ticker: str, *, hours: float = 4.0, strike_type: str | None = "greater_or_equal",
            floor: Decimal | None = Decimal("66000"), volume: str = "100") -> VenueMarket:
    return VenueMarket(
        ticker=ticker, title=f"market {ticker}", status="open",
        close_time=NOW + dt.timedelta(hours=hours),
        strike_type=strike_type, floor_strike=floor, volume=Decimal(volume),
    )


class _FakeVenue:
    """Stands in for KalshiVenue; records what was asked for."""

    def __init__(self, markets: dict[str, list[VenueMarket]], book: BinaryBook | None = None):
        self._markets = markets
        self._book = book
        self.book_calls: list[str] = []

    def markets(self, *, series_ticker=None, status="open", limit=200):
        if series_ticker == "BOOM":
            raise RuntimeError("series is down")
        return self._markets.get(series_ticker, [])

    def orderbook(self, ticker: str, *, depth: int = 10) -> BinaryBook:
        self.book_calls.append(ticker)
        if ticker == "BADBOOK":
            raise RuntimeError("book unavailable")
        return self._book or BinaryBook(ticker=ticker, retrieved_at=NOW)


# ── eligibility ────────────────────────────────────────────────────


def test_keeps_short_dated_machine_readable_markets() -> None:
    v = _FakeVenue({"S": [_market("A", hours=4)]})
    kept = eligible_markets(v, ["S"], max_hours_to_close=48, now=NOW)
    assert [m.ticker for m in kept] == ["A"]


def test_rejects_unparseable_strike() -> None:
    """The claim parser must not fall back to regexing English out of a title."""
    v = _FakeVenue({"S": [_market("A", strike_type="custom", floor=None)]})
    assert eligible_markets(v, ["S"], now=NOW) == []


def test_rejects_beyond_horizon_and_already_closed() -> None:
    v = _FakeVenue({"S": [_market("FAR", hours=200), _market("PAST", hours=-1)]})
    assert eligible_markets(v, ["S"], max_hours_to_close=48, now=NOW) == []


def test_rejects_missing_close_time() -> None:
    m = VenueMarket(ticker="A", title="t", status="open", close_time=None,
                    strike_type="greater_or_equal", floor_strike=Decimal("1"))
    assert eligible_markets(_FakeVenue({"S": [m]}), ["S"], now=NOW) == []


def test_one_broken_series_does_not_stop_the_others() -> None:
    v = _FakeVenue({"BOOM": [], "GOOD": [_market("A")]})
    kept = eligible_markets(v, ["BOOM", "GOOD"], now=NOW)
    assert [m.ticker for m in kept] == ["A"]


# ── book snapshots are state samples, not events ───────────────────


def _book() -> BinaryBook:
    return BinaryBook(
        ticker="A", retrieved_at=NOW,
        yes_bids=[BookLevel(price="0.30", size="10")],
        yes_asks=[BookLevel(price="0.32", size="4")],
    )


def test_identical_consecutive_books_are_both_recorded(tmp_path) -> None:
    """An unchanged book is still a real observation. Collapsing the second one
    would make the tape look like it had a gap."""
    w = TapeWriter(tmp_path)
    v = _FakeVenue({}, book=_book())
    src = book_source(v, ["A"])

    first = poll_source(w, src, now=NOW)
    second = poll_source(w, src, now=NOW + dt.timedelta(minutes=5))
    assert first.append.written == 1
    assert second.append.written == 1  # NOT deduped away
    assert len(w.read("/kalshi/orderbook")) == 2


def test_metadata_is_deduped_because_it_is_near_static(tmp_path) -> None:
    w = TapeWriter(tmp_path)
    v = _FakeVenue({"S": [_market("A")]})
    src = market_metadata_source(v, ["S"])
    poll_source(w, src, now=NOW)
    second = poll_source(w, src, now=NOW + dt.timedelta(minutes=5))
    assert second.append.written == 0  # unchanged terms write nothing


def test_book_row_carries_full_ladders(tmp_path) -> None:
    import json

    w = TapeWriter(tmp_path)
    v = _FakeVenue({}, book=_book())
    poll_source(w, book_source(v, ["A"]), now=NOW)
    payload = json.loads(w.read("/kalshi/orderbook").iloc[0]["payload"])
    # Full depth, so the backtest can walk the book instead of assuming one price.
    assert payload["yes_bids"] == [["0.30", "10"]]
    assert payload["yes_asks"] == [["0.32", "4"]]
    assert payload["best_yes_ask"] == "0.32"


def test_failed_book_does_not_stop_the_sweep(tmp_path) -> None:
    w = TapeWriter(tmp_path)
    v = _FakeVenue({}, book=_book())
    out = poll_source(w, book_source(v, ["BADBOOK", "A"]), now=NOW)
    assert out.append.written == 1  # the healthy market still recorded
    assert out.envelope["failures"] == 1


def test_empty_ticker_list_is_safe(tmp_path) -> None:
    w = TapeWriter(tmp_path)
    out = poll_source(w, book_source(_FakeVenue({}), []), now=NOW)
    assert out.append.written == 0


@pytest.mark.parametrize("dedup_flag,expected", [(True, 0), (False, 1)])
def test_dedup_flag_controls_state_vs_event(tmp_path, dedup_flag, expected) -> None:
    w = TapeWriter(tmp_path)
    row = [{"ticker": "A", "v": 1}]
    w.append("/x", row, now=NOW, dedup=dedup_flag)
    res = w.append("/x", row, now=NOW + dt.timedelta(minutes=1), dedup=dedup_flag)
    assert res.written == expected
