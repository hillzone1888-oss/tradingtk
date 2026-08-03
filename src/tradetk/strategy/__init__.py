"""Strategies: the only thing a strategy produces is a probability.

Importing this package registers every shipped strategy, so `get_strategy(name)`
resolves whatever `strategy.name` in config asks for without the caller needing
to know which module defines it.
"""

from tradetk.strategy.base import (
    BaseStrategy,
    MarketSnapshot,
    StrategyContext,
    StrategyError,
    StrategyOpinion,
    available_strategies,
    get_strategy,
    register_strategy,
    run_strategy,
)

# Imported for the registration side effect. Keep last, and keep the noqa:
# without the import the strategy is invisible to config, and without the noqa
# ruff removes it as unused and silently breaks strategy selection.
from tradetk.strategy.baseline_vol import BaselineVolStrategy  # noqa: E402,F401
from tradetk.strategy.liquidation_skew import LiquidationSkewStrategy  # noqa: E402,F401

__all__ = [
    "BaseStrategy",
    "BaselineVolStrategy",
    "LiquidationSkewStrategy",
    "MarketSnapshot",
    "StrategyContext",
    "StrategyError",
    "StrategyOpinion",
    "available_strategies",
    "get_strategy",
    "register_strategy",
    "run_strategy",
]
