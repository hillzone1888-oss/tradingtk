"""Unit tests for the `chart` CLI helpers (no network, no venue)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tradetk.backtest.replay import BookObservation
from tradetk.cli.chart import implied_prob_series
from tradetk.venues.base import BinaryBook, BookLevel


def _obs(ticker: str, minute: int, bid: str | None, ask: str | None) -> BookObservation:
    when = datetime(2026, 8, 4, 12, minute, tzinfo=timezone.utc)
    book = BinaryBook(
        ticker=ticker,
        retrieved_at=when,
        yes_bids=[BookLevel(price=Decimal(bid), size=Decimal("10"))] if bid else [],
        yes_asks=[BookLevel(price=Decimal(ask), size=Decimal("10"))] if ask else [],
    )
    return BookObservation(ticker=ticker, observed_at=when, book=book)


def test_implied_prob_series_filters_ticker_and_computes_mid() -> None:
    obs = [
        _obs("KXBTCD-A", 0, "0.40", "0.42"),   # mid 0.41
        _obs("KXETHD-Z", 1, "0.10", "0.12"),   # other ticker, excluded
        _obs("KXBTCD-A", 2, "0.44", "0.46"),   # mid 0.45
    ]
    series = implied_prob_series(obs, "KXBTCD-A")
    assert [round(p, 2) for _, p in series] == [0.41, 0.45]
    assert [dt.minute for dt, _ in series] == [0, 2]


def test_implied_prob_series_skips_one_sided_books() -> None:
    obs = [
        _obs("KXBTCD-A", 0, "0.40", None),     # no ask -> mid None -> skipped
        _obs("KXBTCD-A", 1, "0.44", "0.46"),   # mid 0.45
    ]
    series = implied_prob_series(obs, "KXBTCD-A")
    assert [round(p, 2) for _, p in series] == [0.45]
