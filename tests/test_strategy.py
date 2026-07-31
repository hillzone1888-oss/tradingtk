"""The strategy contract and the baseline strategy.

The contract tests pin the properties the design depends on: a strategy can only
return a probability, abstention is distinguishable from a coin flip, and a
strategy has no way to fetch data. The baseline tests pin every abstention path,
because a strategy that silently prices a claim off the wrong asset's vol
produces confident wrong numbers forever.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from tradetk.enums import Capability
from tradetk.strategy import (
    BaselineVolStrategy,
    MarketSnapshot,
    StrategyContext,
    StrategyError,
    available_strategies,
    get_strategy,
    run_strategy,
)
from tradetk.strategy.base import BaseStrategy, register_strategy
from tradetk.translation.claims import Claim, ClaimOperator
from tradetk.venues.base import BinaryBook, BookLevel

NOW = dt.datetime(2026, 7, 22, 12, 0, tzinfo=dt.timezone.utc)
LATER = dt.datetime(2026, 7, 22, 18, 0, tzinfo=dt.timezone.utc)


def claim(underlying: str = "BTC", threshold: str = "100000") -> Claim:
    return Claim(
        ticker=f"KX{underlying}D-TEST", series_ticker=f"KX{underlying}D",
        underlying=underlying, operator=ClaimOperator.above, threshold=Decimal(threshold),
        resolution_time=LATER, resolution_source="CF Benchmarks BRTI",
        rules_primary="test rules",
    )


def snapshot(
    symbol: str = "BTC", *, spot: float = 100000.0, sigma: float = 0.6,
    samples: int = 720, as_of: dt.datetime = NOW,
) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol, as_of=as_of, spot=spot, sigma_annual=sigma,
        sigma_source="realized_vol 1h/30d", n_vol_samples=samples,
    )


def context(snap: MarketSnapshot | None = None, now: dt.datetime = NOW) -> StrategyContext:
    book = BinaryBook(
        ticker="KXBTCD-TEST", retrieved_at=now,
        yes_bids=[BookLevel(price="0.40", size="500")],
        yes_asks=[BookLevel(price="0.45", size="500")],
    )
    return StrategyContext(now=now, snapshot=snap or snapshot(), book=book)


# ── the registry ───────────────────────────────────────────────────


def test_baseline_is_registered_under_its_config_name() -> None:
    assert "baseline_vol" in available_strategies()
    assert isinstance(get_strategy("baseline_vol"), BaselineVolStrategy)


def test_unknown_strategy_lists_the_valid_options() -> None:
    with pytest.raises(StrategyError, match="baseline_vol"):
        get_strategy("no_such_strategy")


def test_duplicate_names_are_refused() -> None:
    """Two strategies on one config name would make which-one-ran depend on
    import order, and a result that depends on import order is not a result."""

    class Clashing(BaseStrategy):
        name = "baseline_vol"
        description = "impostor"

        def required_capabilities(self):
            return set()

        def estimate(self, claim, context):
            raise AssertionError("never runs")

    with pytest.raises(StrategyError, match="already registered"):
        register_strategy(Clashing)


def test_a_strategy_without_a_name_cannot_register() -> None:
    class Nameless(BaseStrategy):
        def required_capabilities(self):
            return set()

        def estimate(self, claim, context):
            raise AssertionError("never runs")

    with pytest.raises(StrategyError, match="`name`"):
        register_strategy(Nameless)


# ── the contract ───────────────────────────────────────────────────


def test_strategy_cannot_reach_a_provider_or_a_venue() -> None:
    """A strategy that could fetch could fetch *future* data during a backtest.
    Its whole input surface is a frozen snapshot and a book."""
    ctx = context()
    assert not hasattr(ctx, "provider")
    assert not hasattr(ctx, "venue")
    assert set(vars(ctx)) == {"now", "snapshot", "book"}


def test_abstention_is_not_a_coin_flip() -> None:
    strategy = BaselineVolStrategy()
    opinion = strategy.abstain(claim(), "no data")
    assert opinion.abstained
    assert opinion.estimate is None
    assert opinion.reason == "no data"


def test_baseline_declares_the_capabilities_it_actually_uses() -> None:
    caps = BaselineVolStrategy().required_capabilities()
    assert Capability.REALIZED_VOL in caps
    assert Capability.SPOT_PRICE in caps
    assert Capability.LIQUIDATIONS not in caps


def test_run_strategy_turns_one_bad_market_into_an_abstention() -> None:
    """A single degenerate market must not abort a sweep of hundreds."""

    class Exploding(BaseStrategy):
        name = "exploding_test_strategy"
        description = "raises on demand"

        def required_capabilities(self):
            return set()

        def estimate(self, claim, context):
            if claim.underlying == "BOOM":
                raise ValueError("detonated")
            return self.abstain(claim, "fine")

    strategy = Exploding()
    seen: list[str] = []
    opinions = run_strategy(
        strategy,
        [(claim("BOOM"), context()), (claim("BTC"), context())],
        on_error=lambda c, e: seen.append(c.ticker),
    )
    assert len(opinions) == 2
    assert "ValueError: detonated" in opinions[0].reason
    assert seen == ["KXBOOMD-TEST"]


def test_describe_reports_params_for_the_proposal_trace() -> None:
    described = BaselineVolStrategy(vol_multiplier=1.2).describe()
    assert described["name"] == "baseline_vol"
    assert described["params"]["vol_multiplier"] == 1.2
    assert "realized_vol" in described["required_capabilities"]


# ── the baseline's happy path ──────────────────────────────────────


def test_baseline_produces_a_traced_estimate() -> None:
    opinion = BaselineVolStrategy().estimate(claim(), context())
    assert not opinion.abstained
    assert opinion.strategy == "baseline_vol"
    assert Decimal(0) <= opinion.estimate.p <= Decimal(1)
    assert opinion.estimate.method == "baseline_vol/lognormal"


def test_at_the_money_claim_prices_at_a_half() -> None:
    opinion = BaselineVolStrategy().estimate(
        claim(threshold="100000"), context(snapshot(spot=100000.0))
    )
    assert opinion.estimate.p == Decimal("0.5")


def test_strategy_is_deterministic() -> None:
    """The backtest replays contexts in order; a non-deterministic strategy
    would make its results unreproducible and therefore worthless."""
    strategy = BaselineVolStrategy()
    c, ctx = claim(), context()
    first = strategy.estimate(c, ctx)
    for _ in range(5):
        assert strategy.estimate(c, ctx).estimate.p == first.estimate.p


def test_vol_multiplier_raises_tail_probabilities() -> None:
    """Inflating vol makes the strategy less willing to sell tails, which is
    the lever against its built-in short-vol posture."""
    # ~2 sigma out at this horizon. Much further and both estimates round to
    # zero at 6 dp, which is itself correct: 40% OTM over 6h is ~21 sigma.
    far = claim(threshold="103000")
    plain = BaselineVolStrategy().estimate(far, context()).estimate.p
    inflated = BaselineVolStrategy(vol_multiplier=2.0).estimate(far, context()).estimate.p
    assert 0 < plain < inflated < Decimal("0.5")


def test_vol_multiplier_is_recorded_in_the_method_string() -> None:
    opinion = BaselineVolStrategy(vol_multiplier=1.5).estimate(claim(), context())
    assert "1.5" in opinion.estimate.method


def test_non_positive_vol_multiplier_is_refused() -> None:
    with pytest.raises(ValueError, match="vol_multiplier"):
        BaselineVolStrategy(vol_multiplier=0.0)


# ── every abstention path ──────────────────────────────────────────


def test_abstains_when_the_snapshot_is_for_the_wrong_asset() -> None:
    """Pricing a BTC claim off ETH vol would be confidently, permanently wrong."""
    opinion = BaselineVolStrategy().estimate(claim("BTC"), context(snapshot("ETH")))
    assert opinion.abstained
    assert "ETH" in opinion.reason and "BTC" in opinion.reason


def test_abstains_on_stale_data() -> None:
    old = NOW - dt.timedelta(hours=2)
    opinion = BaselineVolStrategy().estimate(claim(), context(snapshot(as_of=old)))
    assert opinion.abstained
    assert "stale" in opinion.reason


def test_abstains_when_the_vol_sample_is_too_small() -> None:
    opinion = BaselineVolStrategy().estimate(claim(), context(snapshot(samples=5)))
    assert opinion.abstained
    assert "samples" in opinion.reason


def test_abstains_on_non_positive_vol() -> None:
    opinion = BaselineVolStrategy().estimate(claim(), context(snapshot(sigma=0.0)))
    assert opinion.abstained
    assert "volatility" in opinion.reason


def test_abstains_on_non_positive_spot() -> None:
    snap = MarketSnapshot(
        symbol="BTC", as_of=NOW, spot=0.0, sigma_annual=0.6,
        sigma_source="test", n_vol_samples=720,
    )
    opinion = BaselineVolStrategy().estimate(claim(), context(snap))
    assert opinion.abstained
    assert "spot" in opinion.reason


def test_abstains_rather_than_raising_on_an_already_resolved_claim() -> None:
    past = dt.datetime(2026, 7, 22, 6, 0, tzinfo=dt.timezone.utc)
    resolved = Claim(
        ticker="KXBTCD-OLD", series_ticker="KXBTCD", underlying="BTC",
        operator=ClaimOperator.above, threshold=Decimal("100000"),
        resolution_time=past, resolution_source="CF Benchmarks BRTI",
        rules_primary="test rules",
    )
    opinion = BaselineVolStrategy().estimate(resolved, context())
    assert opinion.abstained
    assert "past" in opinion.reason
