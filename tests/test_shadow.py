"""Shadow evaluation and calibration.

The scoring tests use forecasters whose correct score is known in closed form —
a perfect forecaster, a coin-flipper, an anti-forecaster — because a scoring bug
does not announce itself. It just quietly reports that the model is better than
it is, which is the one direction of error that never gets questioned.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from tradetk.backtest.marketdata import CandleSeries, MarketDataSet
from tradetk.backtest.settlement import CandleSettlement, RecordedStatusSettlement
from tradetk.shadow.calibration import (
    MIN_SCORED,
    build_report,
    compare,
    score,
    settle_records,
)
from tradetk.shadow.evaluator import latest_per_contract
from tradetk.shadow.records import ShadowRecord, ShadowStore
from tradetk.signals.base import Candle
from tradetk.translation.claims import ClaimOperator

D = Decimal
UTC = dt.timezone.utc
T0 = dt.datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def record(
    ticker: str = "KXBTCD-T100000",
    *,
    p: str = "0.60",
    mid: str | None = "0.50",
    threshold: str = "100000",
    observed_at: dt.datetime = T0,
    resolution_time: dt.datetime | None = None,
    measured: bool = False,
    underlying: str = "BTC",
) -> ShadowRecord:
    return ShadowRecord(
        observed_at=observed_at,
        ticker=ticker,
        series_ticker="KXBTCD",
        underlying=underlying,
        strategy="baseline_vol",
        method="test",
        p=D(p),
        market_mid=D(mid) if mid is not None else None,
        best_yes_bid=D("0.49"),
        best_yes_ask=D("0.51"),
        spread=D("0.02"),
        operator=ClaimOperator.above,
        threshold=D(threshold),
        resolution_time=resolution_time or (T0 + dt.timedelta(hours=2)),
        hours_to_resolution=2.0,
        reference_is_measured=measured,
        resolution_source="CF Benchmarks BRTI",
        rules_primary="test rules",
        gate_decision="reject",
    )


def candles(price: float = 100000.0, n: int = 40) -> list[Candle]:
    out = []
    for i in range(n):
        close_at = T0 - dt.timedelta(hours=10) + dt.timedelta(hours=i)
        out.append(
            Candle(
                symbol="BTC", interval="1h",
                open_ms=int((close_at - dt.timedelta(hours=1)).timestamp() * 1000),
                close_ms=int(close_at.timestamp() * 1000),
                o=price, h=price, l=price, c=price, v=1.0, trades=1,
            )
        )
    return out


def dataset(price: float = 100000.0) -> MarketDataSet:
    return MarketDataSet({"BTC": CandleSeries("BTC", candles(price))})


# ── scoring against known-answer forecasters ───────────────────────


def test_perfect_forecaster_scores_zero() -> None:
    result = score([(1.0, True), (0.0, False), (1.0, True)], label="perfect")
    assert result.brier == 0.0
    assert result.beats_coin_flip


def test_coin_flipper_scores_a_quarter() -> None:
    """The reference every other number is read against."""
    result = score([(0.5, True), (0.5, False)], label="coin")
    assert result.brier == 0.25
    assert not result.beats_coin_flip


def test_anti_forecaster_scores_one() -> None:
    result = score([(0.0, True), (1.0, False)], label="backwards")
    assert result.brier == 1.0


def test_empty_population_scores_nothing_rather_than_zero() -> None:
    result = score([], label="empty")
    assert result.n == 0
    assert result.brier is None
    assert result.beats_coin_flip is None


def test_murphy_decomposition_holds_to_within_binning_error() -> None:
    """Brier = reliability - resolution + uncertainty.

    Approximate by construction: binning continuous forecasts drops a within-bin
    variance term. Exact grouping would make the identity trivial.
    """
    pairs = [(i / 100.0, (i % 3) == 0) for i in range(1, 100)]
    result = score(pairs, label="mixed")
    reconstructed = result.reliability - result.resolution + result.uncertainty
    assert result.brier == pytest.approx(reconstructed, abs=0.02)


def test_always_the_base_rate_has_zero_resolution() -> None:
    """Perfectly calibrated and completely useless — the case the decomposition
    exists to distinguish from a genuinely good forecast."""
    pairs = [(0.5, i % 2 == 0) for i in range(40)]
    result = score(pairs, label="base rate")
    assert result.resolution == pytest.approx(0.0, abs=1e-9)
    assert result.reliability == pytest.approx(0.0, abs=1e-9)


def test_uncertainty_depends_only_on_the_base_rate() -> None:
    lopsided = score([(0.9, True)] * 9 + [(0.9, False)], label="lopsided")
    assert lopsided.uncertainty == pytest.approx(0.09, abs=1e-9)
    assert lopsided.base_rate == pytest.approx(0.9, abs=1e-9)


def test_buckets_group_by_predicted_probability() -> None:
    result = score([(0.15, True), (0.15, False), (0.85, True)], label="b")
    assert len(result.buckets) == 2
    assert result.buckets[0].observed_frequency == 0.5
    assert result.buckets[1].observed_frequency == 1.0


# ── the model-versus-market comparison ─────────────────────────────


def test_comparison_scores_both_on_the_same_contracts() -> None:
    data = dataset(price=101000.0)  # settles above the 100000 strike -> YES
    records = [record(f"T{i}", p="0.90", mid="0.50") for i in range(5)]
    scored, _ = settle_records(records, CandleSettlement(data))
    comparison = compare(scored)
    assert comparison.model.n == comparison.market.n == 5
    assert comparison.n_common == 5


def test_better_model_is_detected() -> None:
    data = dataset(price=101000.0)  # every claim resolves YES
    records = [record(f"T{i}", p="0.95", mid="0.40") for i in range(5)]
    scored, _ = settle_records(records, CandleSettlement(data))
    comparison = compare(scored)
    assert comparison.edge_exists is True
    assert comparison.brier_improvement > 0


def test_worse_model_is_reported_bluntly() -> None:
    """The wording matters: this is the finding that should stop trading."""
    data = dataset(price=101000.0)
    records = [record(f"T{i}", p="0.20", mid="0.95") for i in range(300)]
    scored, _ = settle_records(records, CandleSettlement(data))
    verdict = compare(scored).as_dict()["verdict"]
    assert "market mid forecasts better" in verdict
    assert "Do not trade this" in verdict


def test_small_sample_refuses_to_call_a_winner() -> None:
    data = dataset(price=101000.0)
    records = [record(f"T{i}", p="0.95", mid="0.40") for i in range(5)]
    scored, _ = settle_records(records, CandleSettlement(data))
    verdict = compare(scored).as_dict()["verdict"]
    assert "insufficient sample" in verdict
    assert str(MIN_SCORED) in verdict


def test_records_without_a_market_price_are_excluded_from_comparison() -> None:
    data = dataset(price=101000.0)
    records = [record("A", mid="0.50"), record("B", mid=None)]
    scored, _ = settle_records(records, CandleSettlement(data))
    assert compare(scored).n_common == 1


# ── settlement joining ─────────────────────────────────────────────


def test_unsettled_forecasts_are_dropped_not_counted_as_losses() -> None:
    """Defaulting an unknown outcome to False biases every score downward by
    exactly the amount of missing data, and looks conservative while doing it."""
    empty = MarketDataSet({"BTC": CandleSeries("BTC", [])})
    scored, dropped = settle_records([record()], CandleSettlement(empty))
    assert scored == []
    assert dropped["unsettled"] == 1


def test_recorded_venue_result_is_preferred() -> None:
    data = dataset(price=101000.0)  # proxy would say YES
    scored, _ = settle_records(
        [record("KXBTCD-T100000")],
        CandleSettlement(data),
        recorded=RecordedStatusSettlement({"KXBTCD-T100000": False}),
    )
    assert scored[0].outcome is False


def test_record_rebuilds_its_claim_for_settlement() -> None:
    """Settlement must go through Claim.resolves_yes, not a second definition."""
    claim = record(threshold="100000").to_claim()
    assert claim.operator is ClaimOperator.above
    assert claim.threshold == D("100000")
    assert claim.resolves_yes(D("100001")) is True
    assert claim.resolves_yes(D("99999")) is False


# ── independence ───────────────────────────────────────────────────


def test_latest_per_contract_keeps_the_last_look() -> None:
    early = record("A", p="0.10", observed_at=T0)
    late = record("A", p="0.90", observed_at=T0 + dt.timedelta(minutes=30))
    other = record("B", observed_at=T0)
    kept = latest_per_contract([early, late, other])
    assert len(kept) == 2
    assert {r.ticker for r in kept} == {"A", "B"}
    assert next(r for r in kept if r.ticker == "A").p == D("0.90")


def test_report_separates_observations_from_independent_contracts() -> None:
    """Pooling repeated looks as independent trials inflates apparent sample
    size, which narrows confidence intervals that ought to be wide."""
    data = dataset(price=101000.0)
    records = [
        record("A", observed_at=T0),
        record("A", observed_at=T0 + dt.timedelta(minutes=5)),
        record("B", observed_at=T0),
    ]
    report = build_report(records, CandleSettlement(data))
    assert report.n_observations == 3
    assert report.n_contracts == 2
    assert any("not independent trials" in w for w in report.honesty_warnings())


def test_report_segments_measured_reference_markets_separately() -> None:
    data = dataset(price=101000.0)
    records = [record("A", measured=True), record("B", measured=False)]
    report = build_report(records, CandleSettlement(data))
    assert set(report.by_measured_reference) == {"measured_reference", "fixed_strike"}
    assert any("measured-reference" in w for w in report.honesty_warnings())


def test_report_flags_a_thin_sample() -> None:
    data = dataset(price=101000.0)
    report = build_report([record()], CandleSettlement(data))
    assert any(str(MIN_SCORED) in w for w in report.honesty_warnings())


# ── the store ──────────────────────────────────────────────────────


def test_store_round_trips_a_record(tmp_path) -> None:
    store = ShadowStore(tmp_path)
    store.append([record()])
    loaded = store.read()
    assert len(loaded) == 1
    assert loaded[0].ticker == "KXBTCD-T100000"
    assert loaded[0].p == D("0.60")
    assert loaded[0].operator is ClaimOperator.above


def test_store_writes_are_idempotent(tmp_path) -> None:
    """Re-running the evaluator over one tape must not inflate the sample — the
    failure mode being guarded against produces a better-looking result."""
    store = ShadowStore(tmp_path)
    first = store.append([record(), record("B")])
    second = store.append([record(), record("B")])
    assert first["written"] == 2
    assert second["written"] == 0
    assert second["duplicates"] == 2
    assert len(store.read()) == 2


def test_same_contract_at_different_times_is_two_records(tmp_path) -> None:
    store = ShadowStore(tmp_path)
    store.append([record("A", observed_at=T0)])
    store.append([record("A", observed_at=T0 + dt.timedelta(minutes=5))])
    assert len(store.read()) == 2


def test_empty_store_reads_empty(tmp_path) -> None:
    assert ShadowStore(tmp_path).read() == []
    assert ShadowStore(tmp_path).summary() == {"records": 0}
