"""Backtest: replay the project's own recorded tape through the full gate stack.

Free because it uses data this project recorded rather than data anyone sells.
Honest because fills walk the recorded ladder and every result carries its own
sample size.
"""

from tradetk.backtest.engine import (
    BacktestEngine,
    BacktestResult,
    BacktestTrade,
    CalibrationBucket,
    brier_score,
    calibration_buckets,
)
from tradetk.backtest.marketdata import CandleSeries, MarketDataSet
from tradetk.backtest.replay import BookObservation, ReplayError, TapeReplay
from tradetk.backtest.settlement import (
    CandleSettlement,
    RecordedStatusSettlement,
    Settlement,
    SettlementSource,
    settle_claim,
)

__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "BacktestTrade",
    "BookObservation",
    "CalibrationBucket",
    "CandleSeries",
    "CandleSettlement",
    "MarketDataSet",
    "RecordedStatusSettlement",
    "ReplayError",
    "Settlement",
    "SettlementSource",
    "TapeReplay",
    "brier_score",
    "calibration_buckets",
    "settle_claim",
]
