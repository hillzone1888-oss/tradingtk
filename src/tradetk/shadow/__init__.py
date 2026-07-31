"""Shadow evaluation: score the whole universe without trading any of it.

Six slots against thousands of eligible contracts. Forecasts do not need capital
to be scored, so evidence accumulates at the rate the universe moves rather than
the rate capital recycles.

Shadow numbers and live numbers are never blended. They measure different
populations — everything, versus the handful that passed the gates — and
averaging them would answer neither question.
"""

from tradetk.shadow.calibration import (
    CalibrationReport,
    Comparison,
    Score,
    ScoredForecast,
    build_report,
    compare,
    score,
    settle_records,
)
from tradetk.shadow.evaluator import ShadowEvaluator, ShadowRun, latest_per_contract
from tradetk.shadow.records import ShadowRecord, ShadowStore

__all__ = [
    "CalibrationReport",
    "Comparison",
    "Score",
    "ScoredForecast",
    "ShadowEvaluator",
    "ShadowRecord",
    "ShadowRun",
    "ShadowStore",
    "build_report",
    "compare",
    "latest_per_contract",
    "score",
    "settle_records",
]
