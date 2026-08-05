# tests/test_overlay_loader.py
"""A broken bridge must trade normally AND say so. Never fail silent."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tradetk.config.schema import VaultOverlayConfig
from tradetk.overlay.loader import _index_mail, load_overlay
from tradetk.translation.edge import GateLimits
from tradetk.translation.sizing import SizingLimits

NOW = datetime(2026, 8, 4, 22, 0, tzinfo=timezone.utc)

BASE_GATE = GateLimits(
    min_net_edge_pp=Decimal("3.0"), margin_pp=Decimal("1.0"),
    min_book_depth_multiple=Decimal("5.0"), max_book_participation_pct=Decimal("10.0"),
    max_hours_to_resolution=Decimal("168"),
)
BASE_SIZING = SizingLimits(
    position_target=Decimal("2.00"), per_position_ceiling=Decimal("3.00"),
    total_capital=Decimal("20.00"), max_book_participation_pct=Decimal("10.0"),
)


def _load(cfg: VaultOverlayConfig):
    return load_overlay(cfg, base_gate=BASE_GATE, base_sizing=BASE_SIZING)


def test_disabled_overlay_is_a_no_op() -> None:
    overlay = _load(VaultOverlayConfig(enabled=False))
    policy = overlay.for_underlying("BTC", NOW)
    assert policy.blocked is False
    assert policy.sizing_limits == BASE_SIZING
    assert overlay.ok is True  # disabled on purpose is not a failure


def test_missing_config_fails_open() -> None:
    """A broken bridge must not stop trading — the pipeline is safe alone."""
    overlay = _load(VaultOverlayConfig(enabled=True, config_path="nope/missing.yaml"))
    policy = overlay.for_underlying("BTC", NOW)
    assert policy.blocked is False
    assert policy.sizing_limits == BASE_SIZING


def test_missing_config_is_reported_loudly() -> None:
    """Fail-open, never fail-silent: the operator must be able to see it."""
    overlay = _load(VaultOverlayConfig(enabled=True, config_path="nope/missing.yaml"))
    assert overlay.ok is False
    assert overlay.error
    assert overlay.as_dict()["ok"] is False


# ── _index_mail: dedup tie-break and catalyst fan-out ──────────────


class _FakeStance:
    """A stand-in for vaultpost.ApprovedStance: only .underlying and
    .stance.created are read by _index_mail."""

    def __init__(self, underlying: str, created: datetime, id_: str) -> None:
        self.underlying = underlying
        self.stance = type("S", (), {"created": created, "id": id_})()


class _FakeCatalyst:
    """A stand-in for vaultpost.Catalyst: only .underlyings and .id are
    read by _index_mail."""

    def __init__(self, underlyings: list[str], id_: str) -> None:
        self.underlyings = underlyings
        self.id = id_


def test_index_mail_dedup_keeps_the_most_recently_created_stance() -> None:
    older = _FakeStance("BTC", datetime(2026, 8, 1, tzinfo=timezone.utc), "stance-old")
    newer = _FakeStance("BTC", datetime(2026, 8, 4, tzinfo=timezone.utc), "stance-new")
    by_underlying, _ = _index_mail([older, newer], [])
    assert by_underlying["BTC"] is newer

    # Order of arrival must not matter — only .stance.created decides.
    by_underlying_reversed, _ = _index_mail([newer, older], [])
    assert by_underlying_reversed["BTC"] is newer


def test_index_mail_dedup_is_per_underlying() -> None:
    btc = _FakeStance("BTC", datetime(2026, 8, 1, tzinfo=timezone.utc), "stance-btc")
    eth = _FakeStance("ETH", datetime(2026, 8, 1, tzinfo=timezone.utc), "stance-eth")
    by_underlying, _ = _index_mail([btc, eth], [])
    assert by_underlying == {"BTC": btc, "ETH": eth}


def test_index_mail_dedup_tie_break_on_equal_timestamps_keeps_first_seen() -> None:
    """Pinned so the tie-break behavior cannot silently change: with a strict
    '>' comparison, an exactly-equal .stance.created does not displace the
    incumbent, so the first one encountered in iteration order wins."""
    same_time = datetime(2026, 8, 1, tzinfo=timezone.utc)
    first = _FakeStance("BTC", same_time, "stance-first")
    second = _FakeStance("BTC", same_time, "stance-second")
    by_underlying, _ = _index_mail([first, second], [])
    assert by_underlying["BTC"] is first


def test_index_mail_catalyst_indexed_under_every_listed_underlying() -> None:
    cat = _FakeCatalyst(["BTC", "ETH"], "cat-fomc")
    _, cat_map = _index_mail([], [cat])
    assert cat_map == {"BTC": [cat], "ETH": [cat]}


def test_index_mail_multiple_catalysts_accumulate_per_underlying() -> None:
    cat_a = _FakeCatalyst(["BTC"], "cat-a")
    cat_b = _FakeCatalyst(["BTC"], "cat-b")
    _, cat_map = _index_mail([], [cat_a, cat_b])
    assert cat_map["BTC"] == [cat_a, cat_b]


def test_index_mail_uppercases_underlying_keys() -> None:
    stance = _FakeStance("btc", datetime(2026, 8, 1, tzinfo=timezone.utc), "stance-lower")
    cat = _FakeCatalyst(["eth"], "cat-lower")
    by_underlying, cat_map = _index_mail([stance], [cat])
    assert "BTC" in by_underlying
    assert "ETH" in cat_map
