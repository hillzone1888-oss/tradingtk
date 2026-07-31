"""The strategy contract.

**A strategy's only job is to produce a probability.** It does not size, it does
not decide whether to trade, it never sees the fee model, and it cannot reach an
order path. Everything after the estimate — costs, the edge gate, sizing, risk —
is identical for every strategy and lives downstream in the translation layer.

That split is the whole design, and it buys three things:

*Strategies become cheap.* A new strategy is one function returning a number
plus a declaration of what data it needs. It inherits the entire gate stack for
free and cannot accidentally opt out of any of it.

*Gates cannot be bypassed.* A strategy that could return "trade this" would be a
strategy that could return "trade this" *around* the edge gate. Returning only a
probability makes that structurally impossible rather than merely discouraged.

*Strategies stay comparable.* Two strategies that disagree, disagree about one
number, on the same contract, under the same costs. Step 10's calibration can
then score them against each other directly, which is the only honest way to
rank them on a book this small.

**Abstention is a first-class answer.** ``None`` means "no opinion", which is
categorically different from ``p = 0.5`` ("a coin flip"). The first supplies no
edge and produces no trade; the second is a strong claim that the market is
mispriced whenever it is not at 50c. Conflating them manufactures trades out of
missing data, so the return type distinguishes them and the reason is recorded.

**Data comes in as a snapshot, not a provider.** A strategy is handed a
:class:`MarketSnapshot` rather than a live :class:`DataProvider`, so the same
code runs unchanged against live data and against the recorded tape. A strategy
that could fetch could also fetch *future* data during a backtest — the most
common way a backtest becomes fiction. It cannot, here, because it has nothing
to fetch with.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable

from tradetk.enums import Capability
from tradetk.signals.base import DataProvider, require_capabilities
from tradetk.translation.claims import Claim
from tradetk.translation.probability import ProbabilityEstimate
from tradetk.venues.base import BinaryBook


class StrategyError(Exception):
    """A strategy could not be constructed or configured."""


@dataclass(frozen=True)
class MarketSnapshot:
    """Everything known about an underlying at one instant.

    Deliberately a value object with no ability to fetch. `extras` carries
    strategy-specific signals (liquidation clusters, funding skew) so a new
    strategy does not force a change to this type or to every call site that
    builds one.
    """

    symbol: str
    as_of: datetime
    spot: float
    sigma_annual: float
    sigma_source: str
    n_vol_samples: int
    funding_rate: float | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "as_of": self.as_of.isoformat(),
            "spot": self.spot,
            "sigma_annual": self.sigma_annual,
            "sigma_source": self.sigma_source,
            "n_vol_samples": self.n_vol_samples,
            "funding_rate": self.funding_rate,
            "extras": self.extras,
        }


@dataclass(frozen=True)
class StrategyContext:
    """One decision point: a moment, a book, and the underlying's state."""

    now: datetime
    snapshot: MarketSnapshot
    book: BinaryBook


@dataclass(frozen=True)
class StrategyOpinion:
    """A strategy's answer, including the answer "I don't know".

    Abstentions are counted in every report. On a universe this size the
    abstention log says more about a strategy than its trade log: a strategy
    that abstains on 95% of markets is not broken, it is selective, and one that
    never abstains is not confident, it is unconditional.
    """

    strategy: str
    ticker: str
    estimate: ProbabilityEstimate | None
    reason: str | None = None

    @property
    def abstained(self) -> bool:
        return self.estimate is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "ticker": self.ticker,
            "abstained": self.abstained,
            "reason": self.reason,
            "estimate": self.estimate.as_dict() if self.estimate else None,
        }


class BaseStrategy(ABC):
    """Base class for every strategy.

    Subclasses set :attr:`name`, declare :meth:`required_capabilities`, and
    implement :meth:`estimate`. Nothing else is required, and nothing else is
    permitted to reach the venue.
    """

    #: Registry key and the value of `strategy.name` in config.
    name: str = ""

    #: Human-readable one-liner for proposals and reports.
    description: str = ""

    def __init__(self, **params: Any) -> None:
        if not self.name:
            raise StrategyError(
                f"{type(self).__name__} must set a class-level `name` to be "
                "selectable from config"
            )
        self.params = params

    @abstractmethod
    def required_capabilities(self) -> set[Capability]:
        """Signals this strategy needs. Checked against the provider at startup.

        A missing capability halts. It is never silently substituted with zeros
        or stale values — a strategy running on absent data is not a degraded
        strategy, it is a different one, and the operator has not agreed to it.
        """

    @abstractmethod
    def estimate(self, claim: Claim, context: StrategyContext) -> StrategyOpinion:
        """Produce ``P(claim resolves YES)`` or abstain with a reason.

        Must be deterministic: the same claim and context always yield the same
        opinion. The backtest replays contexts in order and a non-deterministic
        strategy would make its results unreproducible and therefore worthless.
        """

    def validate_against(self, provider: DataProvider) -> None:
        """Fail loudly at startup if the provider cannot feed this strategy."""
        require_capabilities(provider, self.required_capabilities())

    def abstain(self, claim: Claim, reason: str) -> StrategyOpinion:
        """Helper for the common "no opinion, and here is why" return."""
        return StrategyOpinion(
            strategy=self.name, ticker=claim.ticker, estimate=None, reason=reason
        )

    def opine(self, estimate: ProbabilityEstimate) -> StrategyOpinion:
        return StrategyOpinion(
            strategy=self.name, ticker=estimate.ticker, estimate=estimate
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "required_capabilities": sorted(c.value for c in self.required_capabilities()),
            "params": self.params,
        }


# ── registry ───────────────────────────────────────────────────────

_REGISTRY: dict[str, type[BaseStrategy]] = {}


def register_strategy(cls: type[BaseStrategy]) -> type[BaseStrategy]:
    """Class decorator making a strategy selectable by `strategy.name` in config.

    Refuses to overwrite an existing name: two strategies answering to one config
    value would make which-one-ran depend on import order, and a backtest whose
    identity depends on import order is not a result.
    """
    if not cls.name:
        raise StrategyError(f"{cls.__name__} must set a class-level `name` before registering")
    existing = _REGISTRY.get(cls.name)
    if existing is not None and existing is not cls:
        raise StrategyError(
            f"strategy name {cls.name!r} is already registered to "
            f"{existing.__name__}; names must be unique"
        )
    _REGISTRY[cls.name] = cls
    return cls


def get_strategy(name: str, **params: Any) -> BaseStrategy:
    """Construct a registered strategy by name, or fail with the valid options."""
    try:
        cls = _REGISTRY[name]
    except KeyError:
        options = ", ".join(sorted(_REGISTRY)) or "(none registered)"
        raise StrategyError(
            f"unknown strategy {name!r}; registered strategies: {options}"
        ) from None
    return cls(**params)


def available_strategies() -> dict[str, str]:
    """Registered name -> description, for `--help` and reports."""
    return {name: cls.description for name, cls in sorted(_REGISTRY.items())}


def run_strategy(
    strategy: BaseStrategy,
    pairs: Iterable[tuple[Claim, StrategyContext]],
    *,
    on_error: Callable[[Claim, Exception], None] | None = None,
) -> list[StrategyOpinion]:
    """Run a strategy across many claims, turning errors into abstentions.

    One market with a degenerate input must not abort a sweep of hundreds. The
    failure becomes an abstention carrying the exception text, so it is visible
    in the report rather than silently absent from it.
    """
    out: list[StrategyOpinion] = []
    for claim, context in pairs:
        try:
            out.append(strategy.estimate(claim, context))
        except Exception as exc:  # noqa: BLE001 - one bad market must not stop the sweep
            if on_error is not None:
                on_error(claim, exc)
            out.append(strategy.abstain(claim, f"{type(exc).__name__}: {exc}"))
    return out
