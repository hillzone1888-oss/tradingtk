"""Eligible-market discovery and orderbook snapshotting for the tape.

Prediction-market **book depth** history is essentially unavailable for purchase
at any price, so — like the whale log — it exists only if we record it. This
module picks which markets are worth the storage and turns each into a tape
source.

Environment note: market data is read from **production** while execution
targets **demo**. Kalshi's demo environment carries neither structured strike
fields nor meaningful depth (verified: 0 contracts of book, ``strike_type`` is
``None`` on the same series that production reports as ``greater_or_equal``), so
recording demo books would archive nothing. Reading production market data needs
no credentials and touches no order path — the venue adapter has none.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from tradetk.signals.recorder import TapeSource
from tradetk.venues.kalshi import KalshiVenue

log = logging.getLogger("tradetk.venues.books")

BOOK_ENDPOINT = "/kalshi/orderbook"
MARKET_ENDPOINT = "/kalshi/markets"

# Frequencies worth recording for a small book: capital lockup is the binding
# constraint, so short-dated contracts buy far more observations per dollar.
SHORT_FREQUENCIES = {"five_min", "fifteen_min", "hourly", "minutely", "daily"}


def crypto_series(venue: KalshiVenue, *, short_dated_only: bool = True) -> list[dict[str, Any]]:
    """Crypto series, optionally restricted to short-dated ones.

    Selects on the venue's own ``category`` field rather than substring-matching
    tickers — matching "ETH" as a substring pulls in markets like "Election
    METHod Amendment".
    """
    payload = venue._get("/series")
    series = payload.get("series") or []
    out = [s for s in series if str(s.get("category", "")).lower() == "crypto"]
    if short_dated_only:
        out = [s for s in out if s.get("frequency") in SHORT_FREQUENCIES]
    return out


def eligible_markets(
    venue: KalshiVenue,
    series_tickers: list[str],
    *,
    max_hours_to_close: float = 48.0,
    require_machine_readable_strike: bool = True,
    now: dt.datetime | None = None,
) -> list[Any]:
    """Open markets worth recording, with the reason for every exclusion logged.

    Markets whose resolution criteria cannot be parsed mechanically are dropped:
    the claim parser must not fall back to regexing English out of a title.
    """
    ref = now or dt.datetime.now(dt.timezone.utc)
    kept: list[Any] = []
    excluded: dict[str, int] = {}

    def drop(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    for ticker in series_tickers:
        try:
            markets = venue.markets(series_ticker=ticker, status="open", limit=200)
        except Exception as exc:  # noqa: BLE001 - one dead series must not stop the rest
            log.warning("series %s failed: %s", ticker, exc)
            drop("series_fetch_failed")
            continue

        for market in markets:
            if require_machine_readable_strike and not market.has_machine_readable_strike:
                drop("no_machine_readable_strike")
                continue
            if market.close_time is None:
                drop("no_close_time")
                continue
            hours = (market.close_time - ref).total_seconds() / 3600.0
            if hours < 0:
                drop("already_closed")
                continue
            if hours > max_hours_to_close:
                drop("beyond_horizon")
                continue
            kept.append(market)

    log.info("eligible markets: %d kept, excluded: %s", len(kept), excluded or "{}")
    return kept


def book_source(venue: KalshiVenue, tickers: list[str], *, depth: int = 10) -> TapeSource:
    """A tape source snapshotting the books of `tickers`.

    ``dedup=False``: a book is a state sample, not an event. Two identical
    consecutive snapshots are two genuine observations, and collapsing them
    would make the tape look like it had a gap.
    """

    def fetch() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        failures = 0
        for ticker in tickers:
            try:
                book = venue.orderbook(ticker, depth=depth)
            except Exception as exc:  # noqa: BLE001 - a thin market must not stop the sweep
                log.warning("book snapshot failed for %s: %s", ticker, exc)
                failures += 1
                continue
            rows.append(
                {
                    "ticker": ticker,
                    "retrieved_at": book.retrieved_at.isoformat(),
                    "best_yes_bid": str(book.best_yes_bid) if book.best_yes_bid else None,
                    "best_yes_ask": str(book.best_yes_ask) if book.best_yes_ask else None,
                    "spread": str(book.spread) if book.spread else None,
                    # Full ladders, so the backtest can walk the book rather than
                    # assuming a single price.
                    "yes_bids": [[str(lv.price), str(lv.size)] for lv in book.yes_bids],
                    "yes_asks": [[str(lv.price), str(lv.size)] for lv in book.yes_asks],
                }
            )
        return rows, {"tickers_requested": len(tickers), "failures": failures}

    return TapeSource(
        name="kalshi_books", endpoint=BOOK_ENDPOINT, fetch=fetch, event_ts_field=None,
        row_cap=None, dedup=False,
    )


def market_metadata_source(venue: KalshiVenue, series_tickers: list[str]) -> TapeSource:
    """Records market metadata (strike, close time, rules) alongside the books.

    Resolution happens against these terms, and they can change; without them on
    tape the backtest cannot tell what a recorded book was even a book *for*.
    """

    def fetch() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for ticker in series_tickers:
            try:
                for m in venue.markets(series_ticker=ticker, status="open", limit=200):
                    rows.append(
                        {
                            "ticker": m.ticker,
                            "series_ticker": ticker,
                            "event_ticker": m.event_ticker,
                            "title": m.title,
                            "status": m.status,
                            "close_time": m.close_time.isoformat() if m.close_time else None,
                            "expiration_time": (
                                m.expiration_time.isoformat() if m.expiration_time else None
                            ),
                            "strike_type": m.strike_type,
                            "floor_strike": str(m.floor_strike) if m.floor_strike else None,
                            "cap_strike": str(m.cap_strike) if m.cap_strike else None,
                            "rules_primary": m.rules_primary,
                            "volume": str(m.volume) if m.volume else None,
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("metadata fetch failed for %s: %s", ticker, exc)
        return rows, {"series_requested": len(series_tickers)}

    # Metadata is near-static, so content-dedup keeps the tape small; a genuine
    # change to a market's terms writes a new row.
    return TapeSource(
        name="kalshi_markets", endpoint=MARKET_ENDPOINT, fetch=fetch, event_ts_field=None,
        row_cap=None, dedup=True,
    )
