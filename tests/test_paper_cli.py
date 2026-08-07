from datetime import datetime, timezone
from decimal import Decimal

from tradetk.cli.paper import run_paper_poll
from tradetk.state.ledger import read_ledger

D = Decimal
NOW = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)

# See conftest fakes (paper_env / engine_case). These tests assume:
#   make_env(...) -> a namespace bundling a fake venue, tape, registry, config,
#   strategy, and MarketDataSet wired so exactly one candidate ("A"/BTC) clears
#   every gate at NOW, resolving 1 day out.


def test_a_clean_poll_opens_one_paper_position(paper_env, tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    summary = run_paper_poll(**paper_env(ledger_path=ledger), now=NOW)
    assert summary["halted"] is None
    assert len(summary["fills"]) == 1 and summary["fills"][0]["ticker"] == "A"
    assert any(e["type"] == "fill" for e in read_ledger(ledger))


def test_stale_data_halts_and_opens_nothing(paper_env, tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    # candle age pushed beyond data_staleness_halt_seconds
    summary = run_paper_poll(**paper_env(ledger_path=ledger, data_age_seconds=10_000), now=NOW)
    assert summary["halted"] == "stale_data_halt"
    assert summary["fills"] == []
    assert any(e["type"] == "halt" for e in read_ledger(ledger))


def test_settlement_runs_before_halt_and_closes_a_resolved_position(paper_env, tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    env = paper_env(ledger_path=ledger, prefill_ticker="Z", prefill_result="no")
    run_paper_poll(**env, now=NOW)
    settles = [e for e in read_ledger(ledger) if e["type"] == "settle"]
    assert settles and settles[0]["ticker"] == "Z"


def test_rerun_of_same_poll_is_idempotent(paper_env, tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    env = paper_env(ledger_path=ledger)
    run_paper_poll(**env, now=NOW)
    run_paper_poll(**paper_env(ledger_path=ledger), now=NOW)
    fills = [e for e in read_ledger(ledger) if e["type"] == "fill"]
    assert len(fills) == 1  # second run added no duplicate


def test_choose_side_matches_engine_best_assessment(engine_case):
    """Paper's per-side choice equals the engine's with the overlay off."""
    from tradetk.cli.paper import choose_side

    claim, estimate, book, when, cap, gate, sizing, fee, engine = engine_case
    paper_pick, _ = choose_side(claim, estimate, book, when, cap,
                                gate_limits=gate, sizing_limits=sizing, fee_model=fee)
    engine_pick, _ = engine._best_assessment(claim, estimate, book, when, cap)
    assert (paper_pick is None) == (engine_pick is None)
    if paper_pick is not None:
        assert paper_pick.side == engine_pick.side
        assert paper_pick.contracts_requested == engine_pick.contracts_requested
        assert paper_pick.net_edge_pp == engine_pick.net_edge_pp
