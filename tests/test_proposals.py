import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tradetk.proposals import build_proposal, config_fingerprint, write_proposal

D = Decimal
NOW = datetime(2026, 8, 6, 21, 40, 55, tzinfo=timezone.utc)


class _Cfg:
    """Duck-typed config: sections are pydantic-like with model_dump()."""

    class _Section:
        def __init__(self, **kw):
            self._kw = kw

        def model_dump(self, mode="json"):
            return dict(self._kw)

    def __init__(self, edge=3.0):
        s = self._Section
        self.capital = s(total_capital=20.0, max_positions=6)
        self.edge_gate = s(min_net_edge_pp=edge, margin_pp=1.0)
        self.liquidity = s(min_book_depth_multiple=5.0)
        self.horizon = s(max_hours_to_resolution=168.0)
        self.risk = s(max_daily_loss_dollars=5.0)
        self.orders = s(prefer_maker=True)
        self.venue = s(name="kalshi", environment="demo")
        self.fees = s(maker_fee=0.0)
        self.strategy = s(name="baseline_vol")


def test_fingerprint_is_stable_and_sensitive():
    a, b = config_fingerprint(_Cfg()), config_fingerprint(_Cfg())
    assert a == b and a.startswith("sha256:")
    assert config_fingerprint(_Cfg(edge=4.0)) != a


def test_write_refuses_to_overwrite(tmp_path):
    p = {"schema_version": 1}
    first = write_proposal(tmp_path, p, created_at=NOW, ticker="KXBTC-T99")
    assert first.exists()
    with pytest.raises(FileExistsError):
        write_proposal(tmp_path, p, created_at=NOW, ticker="KXBTC-T99")


def test_filename_is_utc_stamp_and_ticker(tmp_path):
    path = write_proposal(tmp_path, {}, created_at=NOW, ticker="KXETH-06AUG-T3599.99")
    assert path.name == "20260806T214055Z-KXETH-06AUG-T3599.99.json"


def test_money_survives_the_round_trip(tmp_path, proposal_fixture):
    proposal = proposal_fixture  # built via build_proposal with Decimal inputs
    path = write_proposal(tmp_path, proposal, created_at=NOW, ticker="T")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["schema_version"] == 1
    assert isinstance(loaded["decision"]["capital_at_risk"], str)
    assert D(loaded["decision"]["capital_at_risk"]) == D("1.68")
    assert loaded["created_at"] == "2026-08-06T21:40:55+00:00"


def test_proposal_carries_the_estimate_with_its_inputs(proposal_fixture):
    # Spec: the file must carry "the probability estimate with its inputs
    # (vol, hours to resolution)" -- not just the gated decision.
    estimate = proposal_fixture["estimate"]
    assert isinstance(estimate["p"], str)  # money-like: Decimal serialized as str
    assert D(estimate["p"]) >= D("0")  # a real probability, not a placeholder
    assert isinstance(estimate["sigma_annual"], float)
    assert isinstance(estimate["hours_to_resolution"], float)
    assert estimate["method"]
    assert "inputs" in estimate  # the derivation detail (years, z, etc.)


@pytest.fixture
def proposal_fixture():
    """Build a real proposal using builders from conftest and test_backtest."""
    from test_backtest import REGISTRY, T0, book, engine, market
    from tradetk.strategy.base import StrategyContext
    from tradetk.translation.claims import parse_claim
    from tradetk.translation.edge import assess_side
    from tradetk.venues.base import Side
    from tradetk.state.ledger import project
    from tradetk.risk import RiskDecision

    eng = engine()
    claim = parse_claim(market(), REGISTRY)
    b = book()
    snapshot = eng.data.snapshot_at(claim.underlying, T0, lookback_days=eng.vol_lookback_days)
    opinion = eng.strategy.estimate(claim, StrategyContext(now=T0, snapshot=snapshot, book=b))
    assert not opinion.abstained

    # Get the assessment by assessing the YES side with sizing from the engine
    assessment = assess_side(
        claim=claim,
        estimate=opinion.estimate,
        book=b,
        side=Side.yes,
        contracts=5,
        fee_model=eng.fee_model,
        limits=eng.gate_limits,
        now=T0,
        is_maker=False,
    )

    # Build a real paper book state with empty events
    book_state = project([], starting_capital=D("20.00"), today=T0.date())

    # Build with halt=admitted
    halt = RiskDecision(admitted=True, reason=None)
    overlay_verdict = {"ok": False, "note": "no overlay"}

    fingerprint = config_fingerprint(_Cfg())

    proposal = build_proposal(
        claim=claim,
        assessment=assessment,
        book=b,
        book_state=book_state,
        halt=halt,
        overlay_verdict=overlay_verdict,
        candle_age_seconds=D("60"),
        strategy_name="baseline_vol",
        vol_lookback_days=30,
        created_at=NOW,
        config_fingerprint=fingerprint,
        estimate=opinion.estimate,
    )

    # Verify expected keys
    expected_keys = {
        "schema_version", "created_at", "strategy", "claim", "decision",
        "estimate", "book", "signals", "risk", "overlay", "config_fingerprint",
    }
    assert set(proposal.keys()) == expected_keys

    return proposal
