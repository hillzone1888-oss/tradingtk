"""Shared fixtures: recorded Hyperliquid payloads (locked against live calls)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

L2BOOK_BTC = {
    "coin": "BTC",
    "time": 1784675040902,
    "levels": [
        [  # bids, descending
            {"px": "66246.0", "sz": "8.21803", "n": 25},
            {"px": "66245.0", "sz": "1.10000", "n": 3},
        ],
        [  # asks, ascending
            {"px": "66247.0", "sz": "0.15852", "n": 6},
            {"px": "66248.0", "sz": "2.00000", "n": 4},
        ],
    ],
}

CANDLES_BTC = [
    {"t": 1784653200000, "T": 1784656799999, "s": "BTC", "i": "1h",
     "o": "66510.0", "c": "66189.0", "h": "66536.0", "l": "66100.0", "v": "2326.82222", "n": 19926},
    {"t": 1784656800000, "T": 1784660399999, "s": "BTC", "i": "1h",
     "o": "66189.0", "c": "66246.0", "h": "66300.0", "l": "66050.0", "v": "1500.0", "n": 12000},
]

FUNDING_HISTORY_BTC = [
    {"coin": "BTC", "fundingRate": "0.0000019598", "premium": "-0.0004843219", "time": 1784656800028},
]

META_AND_CTXS = [
    {"universe": [{"szDecimals": 5, "name": "BTC", "maxLeverage": 40, "marginTableId": 56}]},
    [{"funding": "0.0000035984", "openInterest": "36304.05", "prevDayPx": "65100.0",
      "markPx": "66247.0", "midPx": "66246.5", "oraclePx": "66278.5", "premium": "-0.0004616882"}],
]

ALL_MIDS = {"BTC": "66210.5", "ETH": "1917.55"}


@pytest.fixture
def hl_payloads() -> dict:
    return {
        "allMids": ALL_MIDS,
        "l2Book": L2BOOK_BTC,
        "candleSnapshot": CANDLES_BTC,
        "fundingHistory": FUNDING_HISTORY_BTC,
        "metaAndAssetCtxs": META_AND_CTXS,
    }


# ── paper executor (step 15, task 4) fixtures ──────────────────────────

D = Decimal
_PAPER_NOW = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)

_PAPER_RULES = (
    "If the simple average of the sixty seconds of CF Benchmarks' Bitcoin "
    "Real-Time Index (BRTI) before 2 PM EDT is above 100000 at 2 PM EDT, then "
    "the market resolves to Yes."
)


class _FakeVenue:
    """Read-only: only .market(ticker) is used, for settlement."""

    def __init__(self, results):  # results: {ticker: (status, result)}
        self._results = results

    def market(self, ticker):
        from tradetk.venues.base import VenueMarket

        status, result = self._results.get(ticker, ("open", None))
        return VenueMarket(ticker=ticker, title="x", status=status, result=result)


def _paper_market_a():
    from tradetk.venues.base import VenueMarket

    return VenueMarket(
        ticker="A", series_ticker="KXBTCD", event_ticker="KXBTCD-EVT",
        title="Bitcoin price", status="open",
        close_time=_PAPER_NOW + timedelta(days=1),
        strike_type="greater", floor_strike=D("50000"), rules_primary=_PAPER_RULES,
    )


def _paper_book_a():
    from tradetk.venues.base import BinaryBook, BookLevel

    return BinaryBook(
        ticker="A", retrieved_at=_PAPER_NOW,
        yes_bids=[BookLevel(price=D("0.38"), size=D("5000"))],
        yes_asks=[BookLevel(price=D("0.40"), size=D("5000"))],
    )


class _FixedStrategy:
    """A strategy stub whose estimate never abstains — the gate stack is what
    `paper_env` exercises, not the vol model."""

    name = "fixed_stub"

    def estimate(self, claim, context):
        from tradetk.strategy.base import StrategyOpinion
        from tradetk.translation.probability import ProbabilityEstimate

        est = ProbabilityEstimate(
            ticker=claim.ticker, underlying=claim.underlying, p=D("0.55"),
            method="test_stub", computed_at=context.now, spot=50000.0,
            sigma_annual=0.5, hours_to_resolution=claim.hours_to_resolution(context.now),
            z_score=0.5,
        )
        return StrategyOpinion(strategy=self.name, ticker=claim.ticker, estimate=est)


class _FixedData:
    """Stands in for `MarketDataSet`: the stub strategy never inspects the
    snapshot, so any non-None value satisfies `run_paper_poll`'s guard."""

    def snapshot_at(self, underlying, when, *, lookback_days=30):
        return object()


@pytest.fixture
def paper_env(monkeypatch):
    """Return a callable producing kwargs for run_paper_poll, with sane fakes.

    Builds a real `TapeReplay` in memory (one candidate, "A"/BTC, clearing
    every gate at `_PAPER_NOW` with a $0.40 yes-ask book 5000 deep, resolving 1
    day out) and monkeypatches `TapeReplay.from_tape` to return it regardless
    of `tape_dir`, so no tape file ever touches disk. `data_age_seconds`
    controls the staleness input directly; `prefill_ticker`/`prefill_result`
    seed an open position via a fill event so settlement has something to
    close. `market_resolution_time` overrides "A"'s `close_time` (and so its
    claim's `resolution_time`), e.g. to exercise the already-resolved skip.
    `strategy` overrides the default `_FixedStrategy`, e.g. to raise for a
    candidate and exercise per-candidate error isolation.
    """
    from tradetk.backtest.replay import BookObservation, TapeReplay
    from tradetk.config.loader import load_config
    from tradetk.state.ledger import append_events, fill_event
    from tradetk.translation.claims import UnderlyingRegistry

    registry = UnderlyingRegistry({"KXBTCD": "BTC"})
    config = load_config("config/config.example.yaml")

    def _make(*, ledger_path, data_age_seconds=D("0"), prefill_ticker=None, prefill_result=None,
              market_resolution_time=None, strategy=None):
        market = _paper_market_a()
        if market_resolution_time is not None:
            market = market.model_copy(update={"close_time": market_resolution_time})
        replay = TapeReplay(
            observations=[BookObservation("A", _PAPER_NOW, _paper_book_a())],
            metadata={"A": [(_PAPER_NOW, market)]},
        )
        monkeypatch.setattr(TapeReplay, "from_tape", classmethod(lambda cls, tape_dir: replay))

        venue_results = {}
        if prefill_ticker:
            prefill = fill_event(
                ticker=prefill_ticker, underlying="BTC", side="yes", contracts=5,
                assumed_price=D("0.40"), fee=D("0.10"), cost=D("2.10"),
                resolution_time=_PAPER_NOW + timedelta(days=1),
                ts=_PAPER_NOW - timedelta(hours=1),
            )
            append_events(ledger_path, [prefill])
            venue_results[prefill_ticker] = ("finalized", prefill_result)

        return {
            "tape_dir": "unused",
            "registry": registry,
            "config": config,
            "ledger_path": ledger_path,
            "venue": _FakeVenue(venue_results),
            "strategy": strategy if strategy is not None else _FixedStrategy(),
            "data": _FixedData(),
            "data_age_seconds": data_age_seconds,
        }

    return _make


@pytest.fixture
def engine_case():
    """One claim/estimate/book that trades, plus a `BacktestEngine` (overlay
    off) built with the same limits — for the choose_side <-> engine
    cross-check (invariant #3). Reuses the builders already in test_backtest.py
    so the two code paths are compared on identical fixtures."""
    from test_backtest import REGISTRY, T0, book, engine, market

    from tradetk.strategy.base import StrategyContext
    from tradetk.translation.claims import parse_claim

    eng = engine()
    claim = parse_claim(market(), REGISTRY)
    b = book()
    snapshot = eng.data.snapshot_at(claim.underlying, T0, lookback_days=eng.vol_lookback_days)
    opinion = eng.strategy.estimate(claim, StrategyContext(now=T0, snapshot=snapshot, book=b))
    assert not opinion.abstained
    cap = D("0")
    return (claim, opinion.estimate, b, T0, cap, eng.gate_limits, eng.sizing_limits,
            eng.fee_model, eng)


# ── propose command (step 16, task 3) fixtures ─────────────────────────

_PROPOSE_NOW = _PAPER_NOW  # same instant test_propose_cli.py's NOW uses


def _propose_market(ticker: str, series_ticker: str, floor_strike: str, *,
                    close_time=None):
    from tradetk.venues.base import VenueMarket

    return VenueMarket(
        ticker=ticker, series_ticker=series_ticker, event_ticker=f"{series_ticker}-EVT",
        title="price", status="open",
        close_time=close_time or (_PROPOSE_NOW + timedelta(days=1)),
        strike_type="greater", floor_strike=D(floor_strike), rules_primary=_PAPER_RULES,
    )


def _propose_book(ticker: str, *, ask: str, bid: str, size: str = "5000"):
    from tradetk.venues.base import BinaryBook, BookLevel

    return BinaryBook(
        ticker=ticker, retrieved_at=_PROPOSE_NOW,
        yes_bids=[BookLevel(price=D(bid), size=D(size))],
        yes_asks=[BookLevel(price=D(ask), size=D(size))],
    )


class _ProposeStrategy:
    """Fixed p=0.55 opinion, same recipe as `_FixedStrategy` — the gate stack
    is what `propose_env` exercises, not the vol model."""

    name = "fixed_stub"

    def estimate(self, claim, context):
        from tradetk.strategy.base import StrategyOpinion
        from tradetk.translation.probability import ProbabilityEstimate

        est = ProbabilityEstimate(
            ticker=claim.ticker, underlying=claim.underlying, p=D("0.55"),
            method="test_stub", computed_at=context.now, spot=50000.0,
            sigma_annual=0.5, hours_to_resolution=claim.hours_to_resolution(context.now),
            z_score=0.5,
        )
        return StrategyOpinion(strategy=self.name, ticker=claim.ticker, estimate=est)


class _ProposeData:
    """Stands in for `MarketDataSet`. `snapshot_at` returns a non-None stub
    (the stub strategy never inspects it); `coverage()` backs the `_data_age`
    fallback so a caller that omits `data_age_seconds` still gets a real
    computation rather than a hardcoded number."""

    def snapshot_at(self, underlying, when, *, lookback_days=30):
        return object()

    def coverage(self):
        return [{"symbol": "BTC", "last_close": _PROPOSE_NOW.isoformat()}]


@pytest.fixture
def propose_env(tmp_path):
    """Return a callable producing kwargs for run_propose, with sane fakes.

    One candidate ("KXBTCD-T50000"/BTC, $0.35 yes-ask, 5000 deep) clears every
    gate at `_PROPOSE_NOW`, resolving 1 day out — the same recipe as
    `paper_env`, but handing `run_propose` `markets`/`books` directly instead
    of a tape. `two_candidates=True` adds a second ("KXETHD-T3000"/ETH, $0.45
    yes-ask) with a smaller net edge, so the best-edge-first ranking has
    something to prove — and it is listed BEFORE the BTC candidate in
    `markets`, so input order and edge order disagree: a test asserting
    best-edge-first only passes if the ranking sort actually runs.
    `prefill_open=N` seeds N open fill events into the
    live ledger under N distinct tickers/underlyings (so the per-underlying
    concentration cap cannot bind before the slot cap does), exercising
    `no_free_slot` specifically when `N == max_positions - 1`. `overlay` is
    passed straight through to `assess_candidate`.
    """
    from tradetk.config.loader import load_config
    from tradetk.state.ledger import append_events, fill_event
    from tradetk.translation.claims import UnderlyingRegistry

    registry = UnderlyingRegistry({"KXBTCD": "BTC", "KXETHD": "ETH"})
    config = load_config("config/config.example.yaml")

    def _make(*, proposals_dir, ledger_path=None, data_age_seconds=D("0"),
              two_candidates=False, prefill_open=0, overlay=None):
        ledger = ledger_path if ledger_path is not None else tmp_path / "propose-ledger.jsonl"

        market_a = _propose_market("KXBTCD-T50000", "KXBTCD", "50000")
        book_a = _propose_book("KXBTCD-T50000", ask="0.35", bid="0.33")
        markets = [market_a]
        books = {market_a.ticker: book_a}

        if two_candidates:
            # Lower net edge than BTC (narrower gross edge at the same p=0.55
            # stub estimate) AND listed first, so input order disagrees with
            # edge order — a test that only checks the admitted ticker cannot
            # pass by accident if `passing.sort(...)` is ever deleted.
            market_b = _propose_market("KXETHD-T3000", "KXETHD", "3000")
            book_b = _propose_book("KXETHD-T3000", ask="0.45", bid="0.43")
            markets = [market_b, market_a]
            books[market_b.ticker] = book_b

        if prefill_open:
            events = [
                fill_event(
                    ticker=f"PRE-{i}", underlying=f"PREU{i}", side="yes", contracts=5,
                    assumed_price=D("0.40"), fee=D("0.10"), cost=D("2.00"),
                    resolution_time=_PROPOSE_NOW + timedelta(days=1),
                    ts=_PROPOSE_NOW - timedelta(hours=1),
                )
                for i in range(prefill_open)
            ]
            append_events(ledger, events)

        return {
            "config": config,
            "registry": registry,
            "ledger_path": ledger,
            "proposals_dir": proposals_dir,
            "markets": markets,
            "books": books,
            "data": _ProposeData(),
            "overlay": overlay,
            "strategy": _ProposeStrategy(),
            "vol_lookback_days": 30,
            "data_age_seconds": data_age_seconds,
        }

    return _make
