"""The probability model.

Tested against closed-form properties rather than remembered numbers: a
driftless lognormal has exact symmetry and exact at-the-money behaviour, and
those are the things that break silently if the time-scaling or the sign of the
log-ratio is wrong.
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal

import pytest

from tradetk.translation.claims import Claim, ClaimOperator
from tradetk.translation.probability import (
    DEEP_TAIL_Z,
    ProbabilityError,
    estimate_claim_probability,
    normal_cdf,
    prob_above,
)

NOW = dt.datetime(2026, 7, 22, 12, 0, tzinfo=dt.timezone.utc)
LATER = dt.datetime(2026, 7, 22, 18, 0, tzinfo=dt.timezone.utc)  # +6h


def make_claim(
    operator: ClaimOperator = ClaimOperator.above,
    *,
    threshold: str | None = "100000",
    lower: str | None = None,
    upper: str | None = None,
    resolution_time: dt.datetime = LATER,
    measured: bool = False,
) -> Claim:
    return Claim(
        ticker="KXBTCD-TEST",
        series_ticker="KXBTCD",
        underlying="BTC",
        operator=operator,
        threshold=Decimal(threshold) if threshold is not None else None,
        lower_bound=Decimal(lower) if lower is not None else None,
        upper_bound=Decimal(upper) if upper is not None else None,
        resolution_time=resolution_time,
        resolution_source="CF Benchmarks BRTI",
        rules_primary="test rules",
        reference_is_measured=measured,
    )


# ── the normal CDF ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "x,expected",
    [(0.0, 0.5), (1.959963985, 0.975), (-1.959963985, 0.025), (1.644853627, 0.95)],
)
def test_normal_cdf_matches_known_quantiles(x, expected) -> None:
    assert normal_cdf(x) == pytest.approx(expected, abs=1e-9)


def test_normal_cdf_is_symmetric() -> None:
    for x in (0.3, 1.0, 2.5, 4.0):
        assert normal_cdf(x) + normal_cdf(-x) == pytest.approx(1.0, abs=1e-12)


# ── prob_above: closed-form properties ─────────────────────────────


def test_at_the_money_is_exactly_half_under_zero_drift() -> None:
    """The whole point of zero *log* drift: the median stays at spot, so a
    strike sitting exactly on spot is a coin flip at every horizon."""
    for years in (0.001, 0.01, 0.5, 2.0):
        p, z = prob_above(100.0, 100.0, 0.6, years)
        assert z == pytest.approx(0.0, abs=1e-12)
        assert p == pytest.approx(0.5, abs=1e-12)


def test_probability_decreases_as_strike_rises() -> None:
    probs = [prob_above(100.0, k, 0.6, 0.01)[0] for k in (90.0, 100.0, 110.0, 130.0)]
    assert probs == sorted(probs, reverse=True)


def test_higher_vol_pulls_probability_toward_a_half() -> None:
    """More uncertainty means less confidence about an out-of-the-money strike."""
    low = prob_above(100.0, 120.0, 0.3, 0.05)[0]
    high = prob_above(100.0, 120.0, 1.2, 0.05)[0]
    assert low < high < 0.5


def test_longer_horizon_pulls_probability_toward_a_half() -> None:
    near = prob_above(100.0, 120.0, 0.6, 0.005)[0]
    far = prob_above(100.0, 120.0, 0.6, 0.5)[0]
    assert near < far < 0.5


def test_time_scaling_is_square_root_of_time() -> None:
    """Doubling vol and quartering time must leave sigma*sqrt(t) — and so the
    probability — unchanged. Catches a linear-in-t scaling bug."""
    a = prob_above(100.0, 115.0, 0.4, 0.08)[0]
    b = prob_above(100.0, 115.0, 0.8, 0.02)[0]
    assert a == pytest.approx(b, abs=1e-12)


def test_z_score_is_the_standardised_log_distance() -> None:
    p, z = prob_above(100.0, 110.0, 0.5, 0.04)
    assert z == pytest.approx(math.log(100.0 / 110.0) / (0.5 * math.sqrt(0.04)), abs=1e-12)


@pytest.mark.parametrize(
    "spot,strike,sigma,years,message",
    [
        (0.0, 100.0, 0.5, 0.1, "spot"),
        (-1.0, 100.0, 0.5, 0.1, "spot"),
        (100.0, 0.0, 0.5, 0.1, "strike"),
        (100.0, 100.0, 0.0, 0.1, "volatility"),
        (100.0, 100.0, -0.5, 0.1, "volatility"),
        (100.0, 100.0, 0.5, 0.0, "time"),
        (100.0, 100.0, 0.5, -1.0, "time"),
    ],
)
def test_degenerate_inputs_raise_rather_than_default(spot, strike, sigma, years, message) -> None:
    with pytest.raises(ProbabilityError, match=message):
        prob_above(spot, strike, sigma, years)


# ── claim-level estimates ──────────────────────────────────────────


def test_above_and_below_are_exact_complements() -> None:
    kw = dict(spot=100000.0, sigma_annual=0.6, now=NOW)
    above = estimate_claim_probability(make_claim(ClaimOperator.above, threshold="105000"), **kw)
    below = estimate_claim_probability(make_claim(ClaimOperator.below, threshold="105000"), **kw)
    assert above.p + below.p == Decimal(1)


def test_above_and_at_or_above_coincide_for_a_continuous_model() -> None:
    kw = dict(spot=100000.0, sigma_annual=0.6, now=NOW)
    a = estimate_claim_probability(make_claim(ClaimOperator.above, threshold="105000"), **kw)
    b = estimate_claim_probability(
        make_claim(ClaimOperator.at_or_above, threshold="105000"), **kw
    )
    assert a.p == b.p


def test_between_equals_the_difference_of_two_tails() -> None:
    kw = dict(spot=100000.0, sigma_annual=0.6, now=NOW)
    band = estimate_claim_probability(
        make_claim(ClaimOperator.between, threshold=None, lower="98000", upper="102000"), **kw
    )
    lo = estimate_claim_probability(make_claim(ClaimOperator.above, threshold="98000"), **kw)
    hi = estimate_claim_probability(make_claim(ClaimOperator.above, threshold="102000"), **kw)
    # Tolerance is two quanta: the band quantises once, the difference of the
    # two tails quantises twice, so they may legitimately differ in the last dp.
    assert float(band.p) == pytest.approx(float(lo.p - hi.p), abs=2e-6)


def test_probability_stays_in_the_unit_interval() -> None:
    for strike in ("1", "50000", "100000", "200000", "100000000"):
        est = estimate_claim_probability(
            make_claim(threshold=strike), spot=100000.0, sigma_annual=0.6, now=NOW
        )
        assert Decimal(0) <= est.p <= Decimal(1)


def test_a_resolved_claim_is_refused_not_guessed() -> None:
    past = dt.datetime(2026, 7, 22, 6, 0, tzinfo=dt.timezone.utc)
    with pytest.raises(ProbabilityError, match="in the past"):
        estimate_claim_probability(
            make_claim(resolution_time=past), spot=100000.0, sigma_annual=0.6, now=NOW
        )


# ── the flags that stop a bad number being trusted ─────────────────


def test_far_strike_is_flagged_as_deep_tail() -> None:
    est = estimate_claim_probability(
        make_claim(threshold="400000"), spot=100000.0, sigma_annual=0.6, now=NOW
    )
    assert est.is_deep_tail
    assert abs(est.z_score) > DEEP_TAIL_Z
    assert any("thin tails" in w for w in est.warnings)


def test_near_strike_is_not_flagged() -> None:
    est = estimate_claim_probability(
        make_claim(threshold="100500"), spot=100000.0, sigma_annual=0.6, now=NOW
    )
    assert not est.is_deep_tail
    assert est.warnings == []


def test_measured_reference_is_flagged_for_calibration() -> None:
    est = estimate_claim_probability(
        make_claim(threshold="100000", measured=True), spot=100000.0, sigma_annual=0.6, now=NOW
    )
    assert any("measured reference" in w for w in est.warnings)


def test_very_short_horizon_is_flagged() -> None:
    soon = NOW + dt.timedelta(minutes=5)
    est = estimate_claim_probability(
        make_claim(resolution_time=soon), spot=100000.0, sigma_annual=0.6, now=NOW
    )
    assert any("short horizon" in w for w in est.warnings)


def test_estimate_carries_its_whole_derivation() -> None:
    est = estimate_claim_probability(
        make_claim(threshold="105000"), spot=100000.0, sigma_annual=0.6, now=NOW
    )
    d = est.as_dict()
    assert d["spot"] == 100000.0
    assert d["sigma_annual"] == 0.6
    assert d["hours_to_resolution"] == pytest.approx(6.0)
    assert d["inputs"]["drift_annual"] == 0.0
    assert d["inputs"]["threshold"] == "105000"
    assert "sigma_over_horizon" in d["inputs"]


def test_drift_is_a_visible_parameter_not_a_hidden_zero() -> None:
    claim = make_claim(threshold="105000")
    flat = estimate_claim_probability(claim, spot=100000.0, sigma_annual=0.6, now=NOW)
    up = estimate_claim_probability(
        claim, spot=100000.0, sigma_annual=0.6, now=NOW, drift_annual=2.0
    )
    assert up.p > flat.p
