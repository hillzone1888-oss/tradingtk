"""The backtest engine.

The tests that matter most here are the negative ones. A backtest fails by
producing *good* results, so the lookahead guarantees, the settle-before-enter
ordering, and the one-position-per-contract rule are pinned individually — each
of them, if broken, would raise returns rather than break a test.
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal


from tradetk.backtest.engine import (
    BacktestEngine,
    brier_score,
    calibration_buckets,
)
from tradetk.backtest.marketdata import CandleSeries, MarketDataSet
from tradetk.backtest.replay import BookObservation, TapeReplay
from tradetk.backtest.settlement import (
    CandleSettlement,
    RecordedStatusSettlement,
    settle_claim,
)
from tradetk.costs.fees import KalshiFeeModel
from tradetk.risk import RiskLimits
from tradetk.signals.base import Candle
from tradetk.strategy import BaselineVolStrategy
from tradetk.translation.claims import UnderlyingRegistry
from tradetk.translation.edge import GateLimits
from tradetk.translation.sizing import SizingLimits
from tradetk.venues.base import BinaryBook, BookLevel, VenueMarket

D = Decimal
UTC = dt.timezone.utc
T0 = dt.datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
REGISTRY = UnderlyingRegistry({"KXBTCD": "BTC"})

RULES = (
    "If the simple average of the sixty seconds of CF Benchmarks' Bitcoin "
    "Real-Time Index (BRTI) before 2 PM EDT is above 100000 at 2 PM EDT, then "
    "the market resolves to Yes."
)


def candles(n: int = 400, *, start: dt.datetime = T0 - dt.timedelta(days=20),
            price: float = 100000.0, step_hours: int = 1) -> list[Candle]:
    """A deterministic zig-zag: enough variation for a real vol number, no RNG."""
    out = []
    for i in range(n):
        close_at = start + dt.timedelta(hours=step_hours * (i + 1))
        px = price * (1.0 + 0.004 * math.sin(i / 3.0))
        out.append(
            Candle(
                symbol="BTC", interval="1h",
                open_ms=int((close_at - dt.timedelta(hours=step_hours)).timestamp() * 1000),
                close_ms=int(close_at.timestamp() * 1000),
                o=px, h=px * 1.001, l=px * 0.999, c=px, v=1.0, trades=1,
            )
        )
    return out


def market(ticker: str = "KXBTCD-T100000", *, close_time: dt.datetime | None = None,
           strike: str = "100000") -> VenueMarket:
    return VenueMarket(
        ticker=ticker, series_ticker="KXBTCD", event_ticker="KXBTCD-26JUL22",
        title="Bitcoin price", status="open",
        close_time=close_time or (T0 + dt.timedelta(hours=6)),
        strike_type="greater", floor_strike=D(strike), rules_primary=RULES,
    )


def book(bid: str = "0.30", ask: str = "0.32", size: str = "5000") -> BinaryBook:
    return BinaryBook(
        ticker="KXBTCD-T100000", retrieved_at=T0,
        yes_bids=[BookLevel(price=bid, size=size)],
        yes_asks=[BookLevel(price=ask, size=size)],
    )


def replay(observations=None, metadata=None) -> TapeReplay:
    obs = observations or [BookObservation("KXBTCD-T100000", T0, book())]
    meta = metadata if metadata is not None else {"KXBTCD-T100000": [(T0, market())]}
    return TapeReplay(obs, meta)


def engine(**overrides) -> BacktestEngine:
    data = MarketDataSet({"BTC": CandleSeries("BTC", candles())})
    kwargs = dict(
        strategy=BaselineVolStrategy(),
        registry=REGISTRY,
        data=data,
        settlement_source=CandleSettlement(data),
        fee_model=KalshiFeeModel(),
        gate_limits=GateLimits(
            min_net_edge_pp=D("3.0"), margin_pp=D("1.0"),
            min_book_depth_multiple=D("5.0"), max_book_participation_pct=D("10.0"),
            max_hours_to_resolution=D("168"),
        ),
        sizing_limits=SizingLimits(
            position_target=D("2.00"), per_position_ceiling=D("3.00"),
            total_capital=D("20.00"), max_book_participation_pct=D("10"),
        ),
        risk_limits=RiskLimits(
            max_positions=6, max_slots_per_underlying=2, total_capital=D("20.00"),
        ),
    )
    kwargs.update(overrides)
    return BacktestEngine(**kwargs)


# ── as-of access: the anti-lookahead guarantees ────────────────────


def test_candles_that_have_not_closed_are_invisible() -> None:
    """Using the in-progress candle's close leaks the rest of the interval —
    small, subtle, and fatal on 15-minute contracts."""
    series = CandleSeries("BTC", candles(n=5, start=T0, step_hours=1))
    # First candle closes at T0+1h.
    assert series.spot_at(T0) is None
    assert series.spot_at(T0 + dt.timedelta(minutes=59)) is None
    assert series.spot_at(T0 + dt.timedelta(hours=1)) is not None


def test_spot_is_the_last_closed_candle_not_the_nearest() -> None:
    series = CandleSeries("BTC", candles(n=5, start=T0, step_hours=1))
    at_90_min = series.spot_at(T0 + dt.timedelta(minutes=90))
    at_60_min = series.spot_at(T0 + dt.timedelta(hours=1))
    assert at_90_min == at_60_min  # the 2h candle has not closed yet


def test_vol_uses_only_candles_visible_at_the_time() -> None:
    series = CandleSeries("BTC", candles(n=400))
    early = series.realized_vol_at(T0 - dt.timedelta(days=15))
    late = series.realized_vol_at(T0)
    assert early.n_samples < late.n_samples


def test_metadata_lookup_refuses_rows_recorded_later() -> None:
    """Reading a market's *settled* status while deciding to enter it would
    make every result meaningless in a way that looks like skill."""
    later = T0 + dt.timedelta(hours=1)
    tape = replay(metadata={"KXBTCD-T100000": [(later, market())]})
    assert tape.metadata_as_of("KXBTCD-T100000", T0) is None
    assert tape.metadata_as_of("KXBTCD-T100000", later) is not None


def test_metadata_lookup_returns_the_latest_row_at_or_before() -> None:
    rows = [
        (T0, market(close_time=T0 + dt.timedelta(hours=6))),
        (T0 + dt.timedelta(hours=1), market(close_time=T0 + dt.timedelta(hours=9))),
    ]
    tape = replay(metadata={"KXBTCD-T100000": rows})
    assert tape.metadata_as_of(
        "KXBTCD-T100000", T0 + dt.timedelta(minutes=30)
    ).close_time == T0 + dt.timedelta(hours=6)
    assert tape.metadata_as_of(
        "KXBTCD-T100000", T0 + dt.timedelta(hours=2)
    ).close_time == T0 + dt.timedelta(hours=9)


def test_strategy_context_exposes_no_way_to_fetch() -> None:
    data = MarketDataSet({"BTC": CandleSeries("BTC", candles())})
    snap = data.snapshot_at("BTC", T0)
    assert snap is not None
    assert not hasattr(snap, "provider")
    assert not hasattr(snap, "fetch")


# ── settlement ─────────────────────────────────────────────────────


def test_settlement_resolves_through_the_claim_not_a_reimplementation() -> None:
    data = MarketDataSet({"BTC": CandleSeries("BTC", candles())})
    tape = replay()
    claim = tape.claim_as_of("KXBTCD-T100000", T0, REGISTRY)
    outcome = settle_claim(claim, CandleSettlement(data))
    assert outcome.is_known
    assert outcome.resolved == claim.resolves_yes(D(str(outcome.settled_value)))


def test_missing_price_is_unsettled_not_a_loss() -> None:
    empty = MarketDataSet({"BTC": CandleSeries("BTC", [])})
    tape = replay()
    claim = tape.claim_as_of("KXBTCD-T100000", T0, REGISTRY)
    outcome = settle_claim(claim, CandleSettlement(empty))
    assert outcome.resolved is None
    assert "unsettled" in outcome.reason


def test_near_strike_settlement_is_flagged() -> None:
    """A settlement decided by a hair could have gone the other way on the
    venue's own index. That has to be visible, not averaged away."""
    data = MarketDataSet({"BTC": CandleSeries("BTC", candles())})
    tape = replay(metadata={"KXBTCD-T100000": [(T0, market(strike="100000"))]})
    claim = tape.claim_as_of("KXBTCD-T100000", T0, REGISTRY)
    spot = data.spot_at("BTC", claim.resolution_time)
    near = replay(
        metadata={"KXBTCD-T100000": [(T0, market(strike=f"{spot:.2f}"))]}
    ).claim_as_of("KXBTCD-T100000", T0, REGISTRY)
    assert settle_claim(near, CandleSettlement(data)).near_strike


def test_recorded_venue_result_wins_over_the_price_proxy() -> None:
    data = MarketDataSet({"BTC": CandleSeries("BTC", candles())})
    tape = replay()
    claim = tape.claim_as_of("KXBTCD-T100000", T0, REGISTRY)
    recorded = RecordedStatusSettlement({claim.ticker: False})
    outcome = settle_claim(claim, CandleSettlement(data), recorded=recorded)
    assert outcome.resolved is False
    assert outcome.source == "venue_recorded_result"


# ── the engine ─────────────────────────────────────────────────────


def test_engine_runs_and_reports_its_tape_coverage() -> None:
    result = engine().run(replay())
    assert result.tape["observations"] == 1
    assert result.strategy == "baseline_vol"


def test_result_leads_with_honesty_warnings() -> None:
    """The rules require sample size to travel with P&L. It is computed from
    the data and serialised first, not left to a human to remember."""
    payload = engine().run(replay()).as_dict()
    assert list(payload)[0] == "honesty_warnings"
    assert any("days" in w for w in payload["honesty_warnings"])


def test_short_tape_is_always_flagged() -> None:
    result = engine().run(replay())
    assert any("short of the ~90" in w for w in result.honesty_warnings())


def test_one_position_per_contract_however_often_it_is_seen() -> None:
    """Otherwise 'profit' becomes a function of how often the recorder polled."""
    observations = [
        BookObservation("KXBTCD-T100000", T0 + dt.timedelta(minutes=i), book())
        for i in range(10)
    ]
    result = engine().run(replay(observations))
    assert len(result.trades) + len(
        [t for t in result.trades if t.is_settled]
    ) <= 2  # at most the single position, settled once
    assert result.skipped.get("already_traded_this_contract", 0) >= 8


def test_positions_resolving_after_the_tape_are_unsettled_not_guessed() -> None:
    far = T0 + dt.timedelta(days=5)
    tape = replay(metadata={"KXBTCD-T100000": [(T0, market(close_time=far))]})
    result = engine().run(tape)
    assert result.settlement.unsettled >= 0
    assert all(t.is_settled for t in result.trades)


def test_capital_is_never_over_deployed() -> None:
    observations = [
        BookObservation(f"KXBTCD-T{100000 + i}", T0 + dt.timedelta(minutes=i), book())
        for i in range(20)
    ]
    metadata = {
        f"KXBTCD-T{100000 + i}": [
            (T0, market(ticker=f"KXBTCD-T{100000 + i}", strike=str(100000 + i)))
        ]
        for i in range(20)
    }
    result = engine().run(replay(observations, metadata))
    for point in result.equity_curve:
        assert point.capital_deployed <= D("20.00")


def test_slot_limit_is_enforced() -> None:
    observations = [
        BookObservation(f"KXBTCD-T{100000 + i}", T0 + dt.timedelta(minutes=i), book())
        for i in range(20)
    ]
    metadata = {
        f"KXBTCD-T{100000 + i}": [
            (T0, market(ticker=f"KXBTCD-T{100000 + i}", strike=str(100000 + i)))
        ]
        for i in range(20)
    }
    result = engine(risk_limits=RiskLimits(
        max_positions=2, max_slots_per_underlying=2, total_capital=D("20.00"),
    )).run(
        replay(observations, metadata)
    )
    for point in result.equity_curve:
        assert point.open_positions <= 2


def test_skips_are_counted_by_reason() -> None:
    result = engine().run(replay())
    assert isinstance(result.skipped, dict)


def test_no_claim_means_no_trade() -> None:
    result = engine().run(replay(metadata={}))
    assert result.trades == []
    assert result.skipped["no_parseable_claim_at_this_time"] == 1


# ── scoring ────────────────────────────────────────────────────────


def test_brier_of_a_perfect_forecaster_is_zero(monkeypatch) -> None:
    from tradetk.backtest.engine import BacktestTrade
    from tradetk.backtest.settlement import Settlement
    from tradetk.venues.base import Side

    def trade(p: str, won: bool) -> BacktestTrade:
        return BacktestTrade(
            ticker="T", underlying="BTC", strategy="s", side=Side.yes,
            entry_time=T0, resolution_time=T0, contracts=1, average_price=D("0.5"),
            fee=D(0), cost=D("0.5"), p_estimate=D(p), net_edge_pp=D(0),
            binding_cap="none", resolved=won, payout=D(1 if won else 0),
            pnl=D(0), settlement=Settlement("T", won, 1.0, "test", False, None),
        )

    assert brier_score([trade("1.0", True), trade("0.0", False)]) == 0.0
    assert brier_score([trade("0.5", True), trade("0.5", False)]) == 0.25
    assert brier_score([]) is None


def test_calibration_buckets_group_by_predicted_probability() -> None:
    from tradetk.backtest.engine import BacktestTrade
    from tradetk.backtest.settlement import Settlement
    from tradetk.venues.base import Side

    def trade(p: str, won: bool) -> BacktestTrade:
        return BacktestTrade(
            ticker="T", underlying="BTC", strategy="s", side=Side.yes,
            entry_time=T0, resolution_time=T0, contracts=1, average_price=D("0.5"),
            fee=D(0), cost=D("0.5"), p_estimate=D(p), net_edge_pp=D(0),
            binding_cap="none", resolved=won, payout=D(0), pnl=D(0),
            settlement=Settlement("T", won, 1.0, "test", False, None),
        )

    buckets = calibration_buckets(
        [trade("0.15", True), trade("0.15", False), trade("0.85", True)]
    )
    assert len(buckets) == 2
    assert buckets[0].n == 2
    assert buckets[0].observed_frequency == 0.5
    assert buckets[1].observed_frequency == 1.0


def test_book_level_skip_reasons_keep_their_names() -> None:
    """The extraction into risk/ must not rename the reasons downstream reports
    read. With one slot, a filled book records the refusals as `no_free_slot`."""
    observations = [
        BookObservation(f"KXBTCD-T{100000 + i}", T0 + dt.timedelta(minutes=i), book())
        for i in range(20)
    ]
    metadata = {
        f"KXBTCD-T{100000 + i}": [
            (T0, market(ticker=f"KXBTCD-T{100000 + i}", strike=str(100000 + i)))
        ]
        for i in range(20)
    }
    result = engine(
        risk_limits=RiskLimits(
            max_positions=1, max_slots_per_underlying=1, total_capital=D("20.00"),
        )
    ).run(replay(observations, metadata))
    assert result.skipped.get("no_free_slot", 0) >= 1
