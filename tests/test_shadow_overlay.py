"""Shadow measures; it must never let a stance narrow what it measures.

The load bearing test here is the anti-filter pin: even when the overlay would
*block* an underlying, the evaluator must still emit a record carrying that
verdict as an annotation. If a stance could suppress records, the calibration
set would quietly become a record of what the stances already believed, and the
evaluator's entire reason for existing would be defeated.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from vaultpost.schema import Catalyst

from tradetk.backtest.marketdata import CandleSeries, MarketDataSet
from tradetk.backtest.replay import BookObservation, TapeReplay
from tradetk.costs.fees import KalshiFeeModel
from tradetk.overlay.loader import VaultOverlay
from tradetk.shadow.evaluator import ShadowEvaluator
from tradetk.shadow.records import ShadowRecord, ShadowStore
from tradetk.strategy import BaselineVolStrategy
from tradetk.translation.claims import ClaimOperator
from tradetk.translation.edge import GateLimits
from tradetk.translation.sizing import SizingLimits

# Reuse the backtest scaffolding that already proves a BTC claim parses, an
# estimate is produced, and an assessment is made — so this test can focus on
# the one thing it exists to pin: a blocked underlying is still recorded.
from test_backtest import REGISTRY, T0, book, candles, market

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


def _record(**overrides) -> ShadowRecord:
    fields = dict(
        observed_at=NOW, ticker="KXBTCD-T", series_ticker="KXBTCD", underlying="BTC",
        strategy="baseline_vol", method="lognormal", p=Decimal("0.4"),
        operator=ClaimOperator.above, threshold=Decimal("100000"),
        resolution_time=NOW, hours_to_resolution=4.0,
    )
    fields.update(overrides)
    return ShadowRecord(**fields)


def _blocking_catalyst() -> Catalyst:
    """An approved catalyst that blocks BTC entries across the observation time."""
    return Catalyst.model_validate({
        "id": "cat-halt", "type": "catalyst", "from_agent": "daily-sweep",
        "created": T0, "status": "approved", "review_by": "2026-12-31",
        "underlyings": ["BTC"], "event": "FOMC blackout",
        "window_start": T0 - timedelta(hours=1),
        "window_end": T0 + timedelta(hours=1),
        "action": "block",
        "evidence": [{
            "class": "event", "claim": "FOMC", "source_tier": "primary",
            "source_url": "https://federalreserve.gov/x",
            "datum": {"value": "x", "unit": "date", "date": "2026-07-22"},
            "observed_at": T0,
        }],
    })


# ── the record carries and persists the annotation ─────────────────


def test_shadow_record_carries_an_overlay_annotation() -> None:
    rec = _record(overlay={"blocked": True, "bias": "bearish"})
    assert rec.overlay["blocked"] is True


def test_overlay_field_defaults_to_none() -> None:
    """Records written before the overlay existed stay valid."""
    assert _record().overlay is None


def test_overlay_survives_the_parquet_round_trip(tmp_path) -> None:
    """The annotation is the whole payoff; if it is dropped on write, the
    calibration comparison it exists to feed can never be run."""
    store = ShadowStore(tmp_path)
    store.append([_record(overlay={"blocked": True, "bias": "bearish", "reasons": ["x"]})])
    loaded = store.read()
    assert len(loaded) == 1
    assert loaded[0].overlay == {"blocked": True, "bias": "bearish", "reasons": ["x"]}


def test_a_record_without_an_overlay_round_trips_as_none(tmp_path) -> None:
    store = ShadowStore(tmp_path)
    store.append([_record()])
    assert store.read()[0].overlay is None


# ── the anti-filter pin: a blocked underlying is still recorded ─────


def test_a_blocked_underlying_is_still_recorded_with_the_block_annotated() -> None:
    """The anti-filter pin.

    Shadow exists to score the whole universe, including what it declines. A
    blocking overlay must annotate the record, never suppress it — otherwise the
    calibration set silently collapses to what the stances already believed.
    """
    overlay = VaultOverlay(
        base_gate=BASE_GATE, base_sizing=BASE_SIZING,
        catalysts={"BTC": [_blocking_catalyst()]}, enabled=True,
    )
    # Sanity: the overlay really does block BTC at the observation instant.
    assert overlay.for_underlying("BTC", T0).blocked is True

    replay = TapeReplay(
        [BookObservation("KXBTCD-T100000", T0, book())],
        {"KXBTCD-T100000": [(T0, market())]},
    )
    evaluator = ShadowEvaluator(
        strategy=BaselineVolStrategy(),
        registry=REGISTRY,
        data=MarketDataSet({"BTC": CandleSeries("BTC", candles())}),
        fee_model=KalshiFeeModel(),
        gate_limits=BASE_GATE,
        sizing_limits=BASE_SIZING,
        overlay=overlay,
    )
    run = evaluator.run(replay)

    btc = [r for r in run.records if r.underlying == "BTC"]
    assert btc, "a blocked underlying must still produce a record"
    assert btc[0].overlay["blocked"] is True
