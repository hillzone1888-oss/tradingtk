"""Fee model and execution costs.

The published fee table is treated as ground truth: every one of its rows is a
test vector. Costs decide whether anything is viable at $2 a position, so these
are checked at the price grid the spec calls out, for both maker and taker.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tradetk.costs.fees import (
    DEFAULT_MAKER_M,
    DEFAULT_TAKER_M,
    PUBLISHED_TABLE_100,
    FeeRounding,
    KalshiFeeModel,
    reconcile_fill,
)
from tradetk.costs.spread import buy_cost, round_trip_cost, spread_pp
from tradetk.venues.base import BinaryBook, BookLevel

D = Decimal
GRID = ["0.05", "0.25", "0.50", "0.75", "0.95"]


@pytest.fixture
def model() -> KalshiFeeModel:
    return KalshiFeeModel()


# ── ground truth: the published table ──────────────────────────────


@pytest.mark.parametrize("price,expected", sorted(PUBLISHED_TABLE_100.items()))
def test_matches_every_published_table_row(model, price, expected) -> None:
    """0.07 x C x P x (1-P), rounded up to the cent, reproduces the schedule."""
    assert model.fee(100, price) == Decimal(expected)


def test_verification_report_is_clean(model) -> None:
    report = model.verify_against_published_table()
    assert report["ok"] is True
    assert report["mismatches"] == []
    assert report["rows_checked"] == len(PUBLISHED_TABLE_100)


def test_fee_peaks_at_the_middle(model) -> None:
    """Quadratic in P: most expensive at 0.50, cheapest at the extremes."""
    fees = {p: model.fee(100, p) for p in ("0.05", "0.25", "0.50", "0.75", "0.95")}
    assert fees["0.50"] == max(fees.values())
    assert fees["0.05"] == fees["0.95"]  # symmetric
    assert fees["0.25"] == fees["0.75"]


# ── the spec's price grid, taker and maker ─────────────────────────


@pytest.mark.parametrize("price", GRID)
def test_taker_fee_grid(model, price) -> None:
    expected = (D("0.07") * 100 * D(price) * (1 - D(price))).quantize(
        D("0.01"), rounding="ROUND_CEILING"
    )
    assert model.fee(100, price) == expected


@pytest.mark.parametrize("price", GRID)
def test_maker_fee_is_zero_by_default(model, price) -> None:
    """Maker multiplier defaults to 0 and no tradeable crypto series overrides it,
    so resting an order costs nothing in fees."""
    assert model.fee(100, price, is_maker=True) == Decimal(0)
    assert model.cost_pct_of_stake(price, is_maker=True) == Decimal(0)


@pytest.mark.parametrize("price", GRID)
def test_maker_fee_when_a_series_does_charge_it(model, price) -> None:
    """Series in the Non-Standard table can carry M=1 for makers."""
    fee = model.fee(100, price, is_maker=True, multiplier=Decimal(1))
    expected = (D("0.0175") * 100 * D(price) * (1 - D(price))).quantize(
        D("0.01"), rounding="ROUND_CEILING"
    )
    assert fee == expected
    assert fee < model.fee(100, price)  # still cheaper than taker


def test_default_multipliers_match_the_schedule() -> None:
    assert DEFAULT_TAKER_M == Decimal(1)
    assert DEFAULT_MAKER_M == Decimal(0)


# ── cost as a fraction of stake: the sizing insight ────────────────


@pytest.mark.parametrize("price", GRID)
def test_cost_pct_of_stake_is_rate_times_one_minus_p(model, price) -> None:
    assert model.cost_pct_of_stake(price) == D("0.07") * (1 - D(price))


def test_longshots_cost_more_per_dollar_staked(model) -> None:
    """The inversion that matters: cheap contracts are the expensive ones."""
    cheap = model.cost_pct_of_stake("0.05")
    favourite = model.cost_pct_of_stake("0.95")
    assert cheap > favourite
    assert cheap == pytest.approx(Decimal("0.0665"))  # 6.65% of stake
    assert favourite == pytest.approx(Decimal("0.0035"))  # 0.35%
    assert cheap / favourite == pytest.approx(Decimal(19))


def test_cost_pct_is_independent_of_stake_size(model) -> None:
    """Because contracts = S/P, the fee scales with S and the ratio does not."""
    pct = model.cost_pct_of_stake("0.40")
    for contracts in (10, 100, 10_000):
        raw = D("0.07") * contracts * D("0.40") * D("0.60")
        assert raw / (Decimal(contracts) * D("0.40")) == pct


def test_cost_pp_of_stake_is_the_gate_unit(model) -> None:
    assert model.cost_pp_of_stake("0.50") == D("3.5")  # 3.5 probability points


# ── rounding: the ambiguity, made explicit ─────────────────────────


def test_rounding_choice_is_material_on_small_orders() -> None:
    """At the 2-4 contracts a $2 position buys, cent vs centicent differ hugely."""
    cent = KalshiFeeModel(rounding=FeeRounding.cent)
    centi = KalshiFeeModel(rounding=FeeRounding.centicent)
    assert cent.fee(1, "0.50") == D("0.02")
    assert centi.fee(1, "0.50") == D("0.0175")
    # Longshot case: nearly 3x apart.
    assert cent.fee(1, "0.05") == D("0.01")
    assert centi.fee(1, "0.05") == D("0.0034")


def test_both_roundings_agree_at_100_contracts() -> None:
    """Which is why the published table cannot settle the question."""
    cent = KalshiFeeModel(rounding=FeeRounding.cent)
    centi = KalshiFeeModel(rounding=FeeRounding.centicent)
    assert cent.fee(100, "0.50") == centi.fee(100, "0.50") == D("1.75")


def test_default_is_the_conservative_rounding(model) -> None:
    """Modelling fees cheaper than reality lets bad trades through the gate."""
    assert model.rounding is FeeRounding.cent
    assert model.fee(1, "0.50") >= KalshiFeeModel(
        rounding=FeeRounding.centicent).fee(1, "0.50")


def test_rounding_penalty_is_reported(model) -> None:
    q = model.quote(1, "0.05")
    assert q.raw_fee == D("0.07") * 1 * D("0.05") * D("0.95")
    assert q.rounding_penalty == q.fee - q.raw_fee
    assert q.rounding_penalty > 0


def test_roundup_never_produces_a_negative_fee(model) -> None:
    assert model.fee(0, "0.50") == Decimal(0)
    assert model.quote(0, "0.50").fee >= 0


# ── quote validation ───────────────────────────────────────────────


def test_price_outside_zero_one_rejected(model) -> None:
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        model.quote(1, "1.5")


def test_negative_contracts_rejected(model) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        model.quote(-1, "0.5")


def test_quote_serialises(model) -> None:
    out = model.quote(3, "0.45").as_dict()
    assert out["contracts"] == 3
    assert out["rounding"] == "cent"
    assert "%" in out["fee_pct_of_stake"]


# ── per-series verification ────────────────────────────────────────


def test_series_with_default_params_verifies_clean(model) -> None:
    r = model.verify_series("quadratic", 1)
    assert r["charges_maker_fees"] is False
    assert r["maker_fee_is_zero"] is True
    assert r["warning"] is None


def test_series_charging_maker_fees_is_flagged(model) -> None:
    r = model.verify_series("quadratic_with_maker_fees", 1)
    assert r["charges_maker_fees"] is True
    assert r["maker_fee_is_zero"] is False


def test_non_default_multiplier_warns(model) -> None:
    r = model.verify_series("quadratic", 0)
    assert r["model_taker_multiplier_matches"] is False
    assert "differs from default" in r["warning"]


# ── fill reconciliation ────────────────────────────────────────────


def test_reconcile_matching_fill(model) -> None:
    r = reconcile_fill(model, 1, "0.50", "0.02")
    assert r["matches"] is True
    assert r["difference"] == "0.00"


def test_reconcile_detects_divergence_and_names_the_cause(model) -> None:
    """A real fill of $0.0175 would prove centicent rounding is the truth."""
    r = reconcile_fill(model, 1, "0.50", "0.0175")
    assert r["matches"] is False
    assert r["alternative_rounding_would_match"] is True
    assert r["rounding_assumed"] == "cent"


# ── spread and slippage ────────────────────────────────────────────


def _book() -> BinaryBook:
    import datetime as dt

    return BinaryBook(
        ticker="T", retrieved_at=dt.datetime(2026, 7, 22, tzinfo=dt.timezone.utc),
        yes_bids=[BookLevel(price="0.30", size="20"), BookLevel(price="0.28", size="100")],
        yes_asks=[BookLevel(price="0.32", size="4"), BookLevel(price="0.36", size="100")],
    )


def test_spread_in_probability_points() -> None:
    assert spread_pp(_book()) == D("2.00")  # 0.32 - 0.30 = 2 pp


def test_one_sided_book_reports_zero_spread() -> None:
    import datetime as dt

    b = BinaryBook(ticker="T", retrieved_at=dt.datetime(2026, 7, 22, tzinfo=dt.timezone.utc),
                   yes_bids=[BookLevel(price="0.30", size="5")])
    assert spread_pp(b) == Decimal(0)


def test_buy_within_top_level_has_no_slippage(model) -> None:
    c = buy_cost(_book(), 4, model)
    assert c.fully_filled is True
    assert c.average_price == D("0.32")
    assert c.slippage_pp == Decimal(0)


def test_walking_the_book_creates_slippage(model) -> None:
    """4 at 0.32 then 6 at 0.36 -> average 0.344, i.e. 2.4 pp worse than best ask."""
    c = buy_cost(_book(), 10, model)
    assert c.average_price == D("0.344")
    assert c.slippage_pp == pytest.approx(D("2.4"))
    assert c.total_cost_pp > c.fee_pp  # slippage is a real additional cost


def test_partial_fill_is_reported_not_hidden(model) -> None:
    c = buy_cost(_book(), 500, model)
    assert c.fully_filled is False
    assert c.contracts_filled == D("104")


def test_maker_fill_pays_no_slippage_and_no_fee(model) -> None:
    c = buy_cost(_book(), 10, model, is_maker=True)
    assert c.slippage_pp == Decimal(0)
    assert c.fee == Decimal(0)
    assert c.total_cost_pp == Decimal(0)


def test_empty_book_costs_nothing_because_nothing_fills(model) -> None:
    import datetime as dt

    empty = BinaryBook(ticker="T", retrieved_at=dt.datetime(2026, 7, 22, tzinfo=dt.timezone.utc))
    c = buy_cost(empty, 10, model)
    assert c.contracts_filled == Decimal(0)
    assert c.fully_filled is False
    assert c.total_cost_pp == Decimal(0)


# ── round trip ─────────────────────────────────────────────────────


def test_hold_to_resolution_is_cheaper_than_round_trip(model) -> None:
    """Settlement pays out with no exit fee; getting out early costs the spread."""
    rt = round_trip_cost(_book(), 4, model)
    assert rt.hold_to_resolution_pp < rt.round_trip_pp
    assert rt.exit_cost_pp > 0


def test_exit_liquidity_shortfall_is_flagged(model) -> None:
    """Never assume you can get out: at $2 positions this is the normal case."""
    import datetime as dt

    thin = BinaryBook(
        ticker="T", retrieved_at=dt.datetime(2026, 7, 22, tzinfo=dt.timezone.utc),
        yes_bids=[BookLevel(price="0.30", size="1")],
        yes_asks=[BookLevel(price="0.32", size="50")],
    )
    rt = round_trip_cost(thin, 10, model)
    assert rt.exit_liquidity_available is False


def test_round_trip_serialises(model) -> None:
    out = round_trip_cost(_book(), 4, model).as_dict()
    assert "entry" in out and "round_trip_pp" in out
    assert out["entry"]["contracts_requested"] == 4
