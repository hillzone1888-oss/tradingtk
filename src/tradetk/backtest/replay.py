"""Reading the recorded tape back as a time-ordered stream of books.

This is what makes the backtest free: it replays orderbook depth this project
recorded itself. No data vendor sells prediction-market book history at any
useful price, so a backtest that models fills honestly can only be built on a
tape you own. The cost of that is patience — the tape is exactly as deep as the
recorder has been running.

**Every lookup is as-of, and that is enforced here rather than trusted.** Both
:meth:`TapeReplay.metadata_as_of` and :meth:`TapeReplay.claim_as_of` take the
replay timestamp and refuse to return anything recorded after it. The engine
therefore cannot see a market's later state — a revised close time, a settled
status — while deciding whether to trade it. Lookahead in a backtest does not
announce itself; it just quietly produces good results.

**Observation time is the book's own ``retrieved_at``, not the tape's
``recorded_at``.** The second includes however long the writer took to get
around to the row, which is our latency and not the market's.
"""

from __future__ import annotations

import json
import logging
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from tradetk.signals.recorder import TapeWriter
from tradetk.translation.claims import (
    Claim,
    ClaimParseError,
    UnderlyingRegistry,
    parse_claim,
)
from tradetk.venues.base import BinaryBook, BookLevel, VenueMarket

log = logging.getLogger("tradetk.backtest.replay")

BOOK_ENDPOINT = "/kalshi/orderbook"
MARKET_ENDPOINT = "/kalshi/markets"


class ReplayError(Exception):
    """The tape could not be read, or holds nothing to replay."""


@dataclass(frozen=True)
class BookObservation:
    """One recorded book, at the moment the venue reported it."""

    ticker: str
    observed_at: datetime
    book: BinaryBook


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _levels(raw: Any) -> list[BookLevel]:
    """``[["0.49", "81.01"], ...]`` -> typed levels, keeping Decimal exactness."""
    out: list[BookLevel] = []
    for entry in raw or []:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        out.append(BookLevel(price=Decimal(str(entry[0])), size=Decimal(str(entry[1]))))
    return out


def book_from_payload(payload: dict[str, Any]) -> BookObservation | None:
    """Rebuild a :class:`BinaryBook` from one recorded orderbook row.

    Returns ``None`` for a row that cannot be rebuilt, rather than raising: one
    malformed row must not destroy a tape that took weeks to accumulate.
    """
    ticker = payload.get("ticker")
    observed = _parse_ts(payload.get("retrieved_at"))
    if not ticker or observed is None:
        return None
    return BookObservation(
        ticker=str(ticker),
        observed_at=observed,
        book=BinaryBook(
            ticker=str(ticker),
            retrieved_at=observed,
            yes_bids=_levels(payload.get("yes_bids")),
            yes_asks=_levels(payload.get("yes_asks")),
        ),
    )


def market_from_payload(payload: dict[str, Any]) -> VenueMarket | None:
    """Rebuild a :class:`VenueMarket` from one recorded metadata row."""
    if not payload.get("ticker"):
        return None
    try:
        return VenueMarket(
            ticker=payload["ticker"],
            series_ticker=payload.get("series_ticker"),
            event_ticker=payload.get("event_ticker"),
            title=payload.get("title") or "",
            status=payload.get("status") or "unknown",
            close_time=_parse_ts(payload.get("close_time")),
            expiration_time=_parse_ts(payload.get("expiration_time")),
            strike_type=payload.get("strike_type"),
            floor_strike=(
                Decimal(str(payload["floor_strike"]))
                if payload.get("floor_strike") is not None else None
            ),
            cap_strike=(
                Decimal(str(payload["cap_strike"]))
                if payload.get("cap_strike") is not None else None
            ),
            rules_primary=payload.get("rules_primary"),
            volume=(
                Decimal(str(payload["volume"])) if payload.get("volume") is not None else None
            ),
        )
    except Exception as exc:  # noqa: BLE001 - a bad row is skipped, never fatal
        log.warning("skipping unparseable market row %s: %s", payload.get("ticker"), exc)
        return None


class TapeReplay:
    """A recorded tape, queryable as a time-ordered replay."""

    def __init__(
        self,
        observations: list[BookObservation],
        metadata: dict[str, list[tuple[datetime, VenueMarket]]],
    ) -> None:
        self._observations = sorted(observations, key=lambda o: (o.observed_at, o.ticker))
        # Each ticker's metadata sorted by time, with a parallel key list so
        # as-of lookup is a bisect rather than a scan.
        self._metadata = {
            ticker: sorted(rows, key=lambda r: r[0]) for ticker, rows in metadata.items()
        }
        self._meta_keys = {
            ticker: [row[0] for row in rows] for ticker, rows in self._metadata.items()
        }

    @classmethod
    def from_tape(cls, tape_dir: str | Path) -> "TapeReplay":
        """Load every recorded book and market-metadata row from `tape_dir`."""
        writer = TapeWriter(tape_dir)

        books_frame = writer.read(BOOK_ENDPOINT)
        observations: list[BookObservation] = []
        for payload in books_frame.get("payload", []):
            observation = book_from_payload(json.loads(payload))
            if observation is not None:
                observations.append(observation)

        meta_frame = writer.read(MARKET_ENDPOINT)
        metadata: dict[str, list[tuple[datetime, VenueMarket]]] = {}
        for recorded_at, payload in zip(
            meta_frame.get("recorded_at", []), meta_frame.get("payload", [])
        ):
            market = market_from_payload(json.loads(payload))
            if market is None:
                continue
            when = recorded_at.to_pydatetime() if hasattr(recorded_at, "to_pydatetime") else recorded_at
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            metadata.setdefault(market.ticker, []).append((when, market))

        if not observations:
            raise ReplayError(
                f"no orderbook observations on the tape at {tape_dir}. Run "
                "`record` for a while first — the backtest can only replay what "
                "was recorded, and nobody sells this data."
            )
        return cls(observations, metadata)

    # -- reading ------------------------------------------------------

    def observations(self) -> Iterator[BookObservation]:
        """Every recorded book, oldest first. The replay's clock."""
        yield from self._observations

    @property
    def tickers(self) -> set[str]:
        return {o.ticker for o in self._observations}

    @property
    def span(self) -> tuple[datetime, datetime]:
        return self._observations[0].observed_at, self._observations[-1].observed_at

    def metadata_as_of(self, ticker: str, when: datetime) -> VenueMarket | None:
        """Latest metadata for `ticker` recorded at or before `when`.

        Refusing to return later rows is the anti-lookahead guarantee. Without
        it the engine could read a market's *settled* status while deciding
        whether to enter it, which would make every result meaningless in a way
        that looks like skill.
        """
        keys = self._meta_keys.get(ticker)
        if not keys:
            return None
        index = bisect_right(keys, when)
        if index == 0:
            return None
        return self._metadata[ticker][index - 1][1]

    def claim_as_of(
        self, ticker: str, when: datetime, registry: UnderlyingRegistry
    ) -> Claim | None:
        """Parsed claim for `ticker` as it was understood at `when`."""
        market = self.metadata_as_of(ticker, when)
        if market is None:
            return None
        try:
            return parse_claim(market, registry)
        except ClaimParseError:
            return None

    def summary(self) -> dict[str, Any]:
        """Coverage, stated plainly. Read this before reading any result."""
        start, end = self.span
        hours = (end - start).total_seconds() / 3600.0
        distinct = len({o.observed_at for o in self._observations})
        return {
            "observations": len(self._observations),
            "distinct_snapshot_times": distinct,
            "tickers": len(self.tickers),
            "tape_start": start.isoformat(),
            "tape_end": end.isoformat(),
            "tape_span_hours": round(hours, 3),
            "tape_span_days": round(hours / 24.0, 4),
            "markets_with_metadata": len(self._metadata),
        }
