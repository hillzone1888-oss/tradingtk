"""Forced-liquidation flow and the strategy built on it.

Two things carry most of the risk here and get most of the tests.

*The side convention.* `long` means longs were force-sold — downward pressure. A
feed that means the opposite would invert every trade this strategy makes, and
nothing in the numbers would look wrong, so the sign is pinned end to end: from
the event, through the profile's imbalance, to the direction the probability
moves.

*The abstention set.* This strategy is supposed to say nothing far more often
than it speaks. Every gate is tested for the abstention rather than for a
fallback, because a gate that quietly degrades to the baseline would make its
calibration score the baseline's score under another name.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from tradetk.enums import Capability
from tradetk.signals.liquidations import (
    LiquidationDataError,
    LiquidationEvent,
    LiquidationProfile,
    LiquidationSide,
    build_liquidation_profile,
)
from tradetk.strategy import (
    BaselineVolStrategy,
    LiquidationSkewStrategy,
    MarketSnapshot,
    StrategyContext,
    available_strategies,
    get_strategy,
)
from tradetk.strategy.liquidation_skew import PROFILE_KEY
from tradetk.translation.claims import Claim, ClaimOperator
from tradetk.venues.base import BinaryBook, BookLevel

NOW = dt.datetime(2026, 7, 22, 12, 0, tzinfo=dt.timezone.utc)
SOON = dt.datetime(2026, 7, 22, 18, 0, tzinfo=dt.timezone.utc)  # 6h out
FAR = dt.datetime(2026, 7, 30, 12, 0, tzinfo=dt.timezone.utc)  # 8d out


def _ms(when: dt.datetime) -> int:
    return int(when.timestamp() * 1000)


def event(
    *,
    side: LiquidationSide = LiquidationSide.long,
    minutes_ago: float = 5.0,
    notional: float = 50_000.0,
    symbol: str = "BTC",
    price: float = 100_000.0,
) -> LiquidationEvent:
    return LiquidationEvent(
        symbol=symbol,
        time_ms=_ms(NOW - dt.timedelta(minutes=minutes_ago)),
        side=side,
        price=price,
        notional_usd=notional,
    )


def profile(
    *,
    long_usd: float = 100_000.0,
    short_usd: float = 900_000.0,
    n_events: int = 40,
    largest: float = 60_000.0,
    symbol: str = "BTC",
    as_of: dt.datetime = NOW,
    window: int = 60,
) -> LiquidationProfile:
    return LiquidationProfile(
        symbol=symbol,
        as_of=as_of,
        window_minutes=window,
        n_events=n_events,
        long_notional_usd=long_usd,
        short_notional_usd=short_usd,
        largest_event_usd=largest,
    )


def claim(
    underlying: str = "BTC",
    threshold: str = "100000",
    *,
    operator: ClaimOperator = ClaimOperator.above,
    resolves: dt.datetime = SOON,
) -> Claim:
    return Claim(
        ticker=f"KX{underlying}D-TEST",
        series_ticker=f"KX{underlying}D",
        underlying=underlying,
        operator=operator,
        threshold=Decimal(threshold),
        resolution_time=resolves,
        resolution_source="CF Benchmarks BRTI",
        rules_primary="test rules",
    )


def snapshot(
    *,
    symbol: str = "BTC",
    spot: float = 100_000.0,
    sigma: float = 0.6,
    samples: int = 720,
    as_of: dt.datetime = NOW,
    extras: dict | None = None,
) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        as_of=as_of,
        spot=spot,
        sigma_annual=sigma,
        sigma_source="realized_vol 1h/30d",
        n_vol_samples=samples,
        extras=extras if extras is not None else {PROFILE_KEY: profile()},
    )


def context(snap: MarketSnapshot | None = None, now: dt.datetime = NOW) -> StrategyContext:
    book = BinaryBook(
        ticker="KXBTCD-TEST",
        retrieved_at=now,
        yes_bids=[BookLevel(price="0.40", size="500")],
        yes_asks=[BookLevel(price="0.45", size="500")],
    )
    return StrategyContext(now=now, snapshot=snap or snapshot(), book=book)


# ── the window statistic ───────────────────────────────────────────


def test_side_convention_is_pinned_to_pressure_direction() -> None:
    """`long` = longs force-SOLD = down. Getting this backwards inverts everything."""
    longs = build_liquidation_profile(
        [event(side=LiquidationSide.long)], symbol="BTC", as_of=NOW
    )
    shorts = build_liquidation_profile(
        [event(side=LiquidationSide.short)], symbol="BTC", as_of=NOW
    )
    assert longs.imbalance == -1.0  # forced selling
    assert shorts.imbalance == 1.0  # forced buying


def test_events_outside_the_window_are_excluded() -> None:
    p = build_liquidation_profile(
        [event(minutes_ago=5), event(minutes_ago=90)],
        symbol="BTC",
        as_of=NOW,
        window_minutes=60,
    )
    assert p.n_events == 1
    assert p.total_notional_usd == 50_000.0


def test_the_window_is_half_open_so_replays_do_not_double_count() -> None:
    """An event exactly `window` old belongs to the previous window, not this one."""
    p = build_liquidation_profile(
        [event(minutes_ago=60)], symbol="BTC", as_of=NOW, window_minutes=60
    )
    assert p.n_events == 0


def test_future_liquidations_are_refused_not_dropped() -> None:
    """Silently dropping them would make a look-ahead bug look like a quiet hour."""
    with pytest.raises(LiquidationDataError, match="after as_of"):
        build_liquidation_profile([event(minutes_ago=-1)], symbol="BTC", as_of=NOW)


def test_another_asset_in_the_stream_is_refused() -> None:
    with pytest.raises(LiquidationDataError, match="ETH"):
        build_liquidation_profile([event(symbol="ETH")], symbol="BTC", as_of=NOW)


def test_empty_window_is_zero_imbalance_and_zero_events() -> None:
    """0.0 imbalance means "no direction"; n_events is what says "no data"."""
    p = build_liquidation_profile([], symbol="BTC", as_of=NOW)
    assert p.n_events == 0
    assert p.imbalance == 0.0
    assert p.concentration == 0.0


def test_concentration_separates_one_whale_from_a_cascade() -> None:
    whale = build_liquidation_profile(
        [event(notional=900_000), *(event(notional=10_000) for _ in range(10))],
        symbol="BTC",
        as_of=NOW,
    )
    cascade = build_liquidation_profile(
        [event(notional=50_000) for _ in range(20)], symbol="BTC", as_of=NOW
    )
    assert whale.concentration == pytest.approx(0.9)
    assert cascade.concentration == pytest.approx(0.05)
    # Same direction, same order of magnitude, entirely different evidence.
    assert whale.imbalance == cascade.imbalance == -1.0


def test_profile_serialises_the_raw_sums_not_only_the_ratio() -> None:
    d = profile().as_dict()
    assert d["long_notional_usd"] == 100_000.0
    assert d["short_notional_usd"] == 900_000.0
    assert d["imbalance"] == pytest.approx(0.8)


# ── registry + contract ────────────────────────────────────────────


def test_registered_under_its_config_name() -> None:
    assert "liquidation_skew" in available_strategies()
    assert isinstance(get_strategy("liquidation_skew"), LiquidationSkewStrategy)


def test_declares_the_liquidations_capability_it_cannot_run_without() -> None:
    caps = LiquidationSkewStrategy().required_capabilities()
    assert Capability.LIQUIDATIONS in caps
    # And the baseline's, since it still prices through the same lognormal.
    assert caps > BaselineVolStrategy().required_capabilities()


def test_an_unknown_regime_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="regime"):
        LiquidationSkewStrategy(regime="whatever")


@pytest.mark.parametrize(
    "params", [{"max_drift_sigma": -0.1}, {"vol_bump": 0.0}, {"max_concentration": 0.0}]
)
def test_out_of_range_parameters_are_refused(params: dict) -> None:
    with pytest.raises(ValueError):
        LiquidationSkewStrategy(**params)


def test_the_regime_and_tilt_are_recorded_in_the_method_string() -> None:
    opinion = LiquidationSkewStrategy().estimate(claim(), context())
    assert not opinion.abstained
    assert "continuation" in opinion.estimate.method
    assert "sigma" in opinion.estimate.method


def test_the_full_derivation_travels_with_the_estimate() -> None:
    opinion = LiquidationSkewStrategy().estimate(claim(), context())
    inputs = opinion.estimate.inputs
    assert inputs["regime"] == "continuation"
    assert inputs["liquidations"]["imbalance"] == pytest.approx(0.8)
    assert inputs["shift_sigma"] == pytest.approx(0.25 * 0.8)
    # The lognormal's own trace is not clobbered by the tilt's.
    assert "sigma_over_horizon" in inputs


def test_is_deterministic() -> None:
    strat, c = LiquidationSkewStrategy(), context()
    first = strat.estimate(claim(), c)
    second = strat.estimate(claim(), c)
    assert first.estimate.p == second.estimate.p


# ── the tilt ───────────────────────────────────────────────────────


def _p(strategy: LiquidationSkewStrategy, prof: LiquidationProfile) -> float:
    opinion = strategy.estimate(
        claim(), context(snapshot(extras={PROFILE_KEY: prof}))
    )
    assert not opinion.abstained, opinion.reason
    return opinion.estimate.p_float


def test_forced_buying_raises_p_above_and_forced_selling_lowers_it() -> None:
    strat = LiquidationSkewStrategy()
    balanced = _p(strat, profile(long_usd=500_000, short_usd=500_000))
    forced_buying = _p(strat, profile(long_usd=100_000, short_usd=900_000))
    forced_selling = _p(strat, profile(long_usd=900_000, short_usd=100_000))
    assert forced_selling < balanced < forced_buying


def test_a_balanced_window_reproduces_the_baseline_exactly() -> None:
    """With no imbalance there is no term, so the two strategies must agree."""
    balanced = profile(long_usd=500_000, short_usd=500_000)
    tilted = LiquidationSkewStrategy().estimate(
        claim(), context(snapshot(extras={PROFILE_KEY: balanced}))
    )
    base = BaselineVolStrategy().estimate(claim(), context())
    assert tilted.estimate.p == base.estimate.p


def test_reversion_is_the_mirror_of_continuation() -> None:
    prof = profile(long_usd=100_000, short_usd=900_000)
    up = _p(LiquidationSkewStrategy(regime="continuation"), prof)
    down = _p(LiquidationSkewStrategy(regime="reversion"), prof)
    base = _p(
        LiquidationSkewStrategy(max_drift_sigma=0.0),
        prof,
    )
    assert down < base < up
    assert (up - base) == pytest.approx(base - down, abs=2e-3)


def test_the_tilt_is_bounded_by_the_cap_however_extreme_the_flow() -> None:
    """A one-sided window is the most this may ever move; there is no runaway."""
    strat = LiquidationSkewStrategy()
    one_sided = _p(strat, profile(long_usd=0.0, short_usd=1_000_000))
    at_cap = strat._shift_sigma(profile(long_usd=0.0, short_usd=1_000_000))
    assert at_cap == pytest.approx(strat.max_drift_sigma)
    # ...and a bigger cap moves it strictly further, so the cap is what binds.
    bigger = _p(LiquidationSkewStrategy(max_drift_sigma=0.5), profile(long_usd=0.0, short_usd=1_000_000))
    assert bigger > one_sided


def test_hitting_the_cap_is_reported_as_a_warning() -> None:
    opinion = LiquidationSkewStrategy().estimate(
        claim(), context(snapshot(extras={PROFILE_KEY: profile(long_usd=0.0, short_usd=1e6)}))
    )
    assert any("drift cap" in w for w in opinion.estimate.warnings)


def test_the_tilt_is_scaled_to_the_claims_own_horizon() -> None:
    """0.25 sigma means 0.25 sigma at 2h and at 20h — otherwise the two are
    incomparable and the parameter means nothing."""
    strat = LiquidationSkewStrategy()
    prof = profile(long_usd=0.0, short_usd=1_000_000)
    z_shifts = []
    for hours in (2, 20):
        resolves = NOW + dt.timedelta(hours=hours)
        tilted = strat.estimate(
            claim(resolves=resolves), context(snapshot(extras={PROFILE_KEY: prof}))
        )
        flat = BaselineVolStrategy().estimate(claim(resolves=resolves), context())
        z_shifts.append(tilted.estimate.z_score - flat.estimate.z_score)
    assert z_shifts[0] == pytest.approx(z_shifts[1], abs=1e-6)
    assert z_shifts[0] == pytest.approx(0.25, abs=1e-6)


def test_the_tilt_applies_to_below_claims_in_the_opposite_direction() -> None:
    prof = profile(long_usd=0.0, short_usd=1_000_000)  # forced buying
    strat = LiquidationSkewStrategy()
    up = strat.estimate(
        claim(operator=ClaimOperator.below), context(snapshot(extras={PROFILE_KEY: prof}))
    )
    base = BaselineVolStrategy().estimate(claim(operator=ClaimOperator.below), context())
    assert up.estimate.p < base.estimate.p


def test_vol_bump_is_off_by_default_and_flagged_when_used() -> None:
    default = LiquidationSkewStrategy()
    assert default.vol_bump == 1.0
    bumped = LiquidationSkewStrategy(vol_bump=1.2).estimate(claim(), context())
    assert any("scaled by 1.2" in w for w in bumped.estimate.warnings)


# ── abstentions: it says nothing far more often than it speaks ─────


def test_abstains_without_liquidation_data_rather_than_becoming_the_baseline() -> None:
    opinion = LiquidationSkewStrategy().estimate(claim(), context(snapshot(extras={})))
    assert opinion.abstained
    assert PROFILE_KEY in opinion.reason


def test_abstains_on_an_untyped_liquidation_payload() -> None:
    opinion = LiquidationSkewStrategy().estimate(
        claim(), context(snapshot(extras={PROFILE_KEY: {"imbalance": 0.9}}))
    )
    assert opinion.abstained
    assert "LiquidationProfile" in opinion.reason


def test_abstains_when_the_profile_is_for_another_asset() -> None:
    opinion = LiquidationSkewStrategy().estimate(
        claim("BTC"), context(snapshot(extras={PROFILE_KEY: profile(symbol="ETH")}))
    )
    assert opinion.abstained
    assert "ETH" in opinion.reason


def test_abstains_when_the_profile_window_is_not_the_configured_one() -> None:
    opinion = LiquidationSkewStrategy(window_minutes=60).estimate(
        claim(), context(snapshot(extras={PROFILE_KEY: profile(window=15)}))
    )
    assert opinion.abstained
    assert "15min" in opinion.reason


def test_abstains_on_a_stale_profile() -> None:
    old = NOW - dt.timedelta(minutes=30)
    opinion = LiquidationSkewStrategy().estimate(
        claim(), context(snapshot(extras={PROFILE_KEY: profile(as_of=old)}))
    )
    assert opinion.abstained
    assert "stale liquidation profile" in opinion.reason


def test_abstains_on_a_thin_window() -> None:
    opinion = LiquidationSkewStrategy().estimate(
        claim(), context(snapshot(extras={PROFILE_KEY: profile(n_events=3)}))
    )
    assert opinion.abstained
    assert "below the minimum" in opinion.reason


def test_abstains_when_the_notional_is_too_small_to_be_evidence() -> None:
    opinion = LiquidationSkewStrategy().estimate(
        claim(),
        context(snapshot(extras={PROFILE_KEY: profile(long_usd=1_000, short_usd=9_000)})),
    )
    assert opinion.abstained
    assert "forced flow" in opinion.reason


def test_abstains_when_one_liquidation_dominates_the_window() -> None:
    opinion = LiquidationSkewStrategy().estimate(
        claim(),
        context(snapshot(extras={PROFILE_KEY: profile(largest=900_000)})),
    )
    assert opinion.abstained
    assert "margin call" in opinion.reason


def test_abstains_on_claims_resolving_beyond_the_signals_horizon() -> None:
    opinion = LiquidationSkewStrategy().estimate(claim(resolves=FAR), context())
    assert opinion.abstained
    assert "horizon" in opinion.reason


def test_inherits_every_snapshot_guard_from_the_baseline() -> None:
    """The shared guards are shared, not reimplemented — same reasons, same gates."""
    strat = LiquidationSkewStrategy()
    wrong_asset = strat.estimate(claim("BTC"), context(snapshot(symbol="ETH")))
    thin_vol = strat.estimate(claim(), context(snapshot(samples=5)))
    stale = strat.estimate(claim(), context(snapshot(as_of=NOW - dt.timedelta(hours=2))))
    assert wrong_asset.abstained and "ETH" in wrong_asset.reason
    assert thin_vol.abstained and "samples" in thin_vol.reason
    assert stale.abstained and "stale market data" in stale.reason


def test_abstains_rather_than_raising_on_an_already_resolved_claim() -> None:
    past = NOW - dt.timedelta(hours=1)
    opinion = LiquidationSkewStrategy().estimate(claim(resolves=past), context())
    assert opinion.abstained
    assert "past" in opinion.reason
