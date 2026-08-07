import json
from datetime import datetime, timezone
from decimal import Decimal

from tradetk.cli.propose import run_propose

D = Decimal
NOW = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)

# `propose_env` (tests/conftest.py) assembles: a real config
# (config.example.yaml pattern), registry, one-or-two VenueMarket objects
# whose claims parse, matching BinaryBook dicts, a data/snapshot stub, and a
# stub strategy returning a fixed non-abstaining estimate — the same recipe
# as paper_env, but handing run_propose `markets`/`books` directly.


def test_clean_run_writes_one_valid_proposal(propose_env, tmp_path):
    env = propose_env(proposals_dir=tmp_path / "proposals")
    summary = run_propose(**env, now=NOW)
    assert summary["halted"] is None
    assert len(summary["proposed"]) == 1
    path = summary["proposed"][0]["file"]
    doc = json.loads(open(path, encoding="utf-8").read())
    assert doc["schema_version"] == 1
    assert doc["config_fingerprint"].startswith("sha256:")
    assert isinstance(doc["decision"]["capital_at_risk"], str)
    assert doc["estimate"]  # the probability estimate, with its inputs (vol, hours)


def test_halted_run_writes_nothing(propose_env, tmp_path):
    env = propose_env(proposals_dir=tmp_path / "p", data_age_seconds=D("999999"))
    summary = run_propose(**env, now=NOW)
    assert summary["halted"] == "stale_data_halt"
    assert summary["proposed"] == []
    assert not list((tmp_path / "p").glob("*.json"))


def test_slot_cap_admits_best_edge_first(propose_env, tmp_path):
    # two passing candidates, but the live ledger already holds max_positions-1
    # positions -> exactly ONE file, and it is the higher-net-edge candidate.
    env = propose_env(proposals_dir=tmp_path / "p", two_candidates=True,
                      prefill_open=5)  # config max_positions=6
    summary = run_propose(**env, now=NOW)
    assert len(summary["proposed"]) == 1
    assert summary["skips"].get("no_free_slot", 0) == 1
    assert summary["proposed"][0]["ticker"] == "KXBTCD-T50000"  # higher net edge


def test_blocked_overlay_underlying_yields_no_file(propose_env, tmp_path):
    class _BlockedPolicy:
        blocked = True

    class _Overlay:
        ok = True

        def for_underlying(self, underlying, now):
            return _BlockedPolicy()

        def as_dict(self):
            return {"ok": True}

    env = propose_env(proposals_dir=tmp_path / "p", overlay=_Overlay())
    summary = run_propose(**env, now=NOW)
    assert summary["proposed"] == []
    assert summary["skips"].get("overlay_blocked", 0) >= 1


def test_live_ledger_is_never_written(propose_env, tmp_path):
    ledger = tmp_path / "live.jsonl"
    env = propose_env(proposals_dir=tmp_path / "p", ledger_path=ledger)
    run_propose(**env, now=NOW)
    assert not ledger.exists()  # propose reads it; only execute may append
