"""The backtest engine: replay the tape, gate, fill against the real book, settle.

Free by construction — it walks orderbook depth this project recorded itself,
so there is no data subscription and no platform in the loop. What it costs
instead is time: the tape is exactly as deep as the recorder has been running,
and no amount of code makes a 17-minute tape mean something.

**Fills walk the recorded ladder.** Not the mid, not the top of book. A $2 order
in a thin prediction market routinely clears several levels, and a backtest that
assumes one price is a backtest that reports an edge nobody could have taken.
The same :mod:`tradetk.costs.spread` code prices fills here and live, so the two
cannot drift apart.

**One position per contract per run.** Without this the engine re-enters the
same market at every snapshot and "profit" becomes a function of how often the
recorder polled. That is a sampling artifact wearing a P&L's clothes.

**Positions are held to resolution.** At this size the exit book usually cannot
absorb the position at any sane price, so modelling discretionary exits would be
modelling liquidity that is not there. Payout is $1 or $0, with no exit fee.

**Events are processed in time order.** At each timestamp, positions that have
resolved are settled *first* — freeing their capital — before new entries are
considered. Doing it the other way round silently grants extra buying power and
inflates the trade count.

**What this engine will not tell you.** Whether the strategy has edge. A $20
book with a handful of slots produces P&L dominated by noise, and the operating
rules require that P&L never appear without its sample size and calibration
beside it. :class:`BacktestResult` enforces that structurally: the honesty
warnings are computed from the data, not written by hand, and are part of every
serialisation.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable

from tradetk.backtest.marketdata import MarketDataSet
from tradetk.backtest.replay import TapeReplay
from tradetk.backtest.settlement import (
    RecordedStatusSettlement,
    Settlement,
    SettlementReport,
    SettlementSource,
    settle_claim,
    summarise_settlements,
)
from tradetk.costs.fees import KalshiFeeModel
from tradetk.strategy.base import BaseStrategy, StrategyContext
from tradetk.translation.claims import Claim, UnderlyingRegistry
from tradetk.translation.edge import (
    EdgeAssessment,
    GateLimits,
    assess_side,
    side_depth,
)
from tradetk.translation.sizing import SizingLimits, plan_size
from tradetk.venues.base import BinaryBook, Side

log = logging.getLogger("tradetk.backtest.engine")

#: Thresholds below which a result is not evidence. From the operating rules:
#: flag anything resting on under ~90 days of tape or a few hundred resolved
#: contracts.
MIN_TAPE_DAYS = 90.0
MIN_SETTLED_TRADES = 200


@dataclass(frozen=True)
class BacktestTrade:
    """One simulated position, entry through settlement."""

    ticker: str
    underlying: str
    strategy: str
    side: Side
    entry_time: datetime
    resolution_time: datetime
    contracts: int
    average_price: Decimal
    fee: Decimal
    cost: Decimal
    p_estimate: Decimal
    net_edge_pp: Decimal
    binding_cap: str
    resolved: bool | None
    payout: Decimal
    pnl: Decimal
    settlement: Settlement
    #: The claim's threshold, carried through so a report can draw the line the
    #: trade was actually about. For a range claim this is the lower bound.
    strike: Decimal | None = None
    claim_description: str = ""

    @property
    def is_settled(self) -> bool:
        return self.resolved is not None

    @property
    def won(self) -> bool:
        return bool(self.resolved)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "underlying": self.underlying,
            "strategy": self.strategy,
            "side": self.side.value,
            "entry_time": self.entry_time.isoformat(),
            "resolution_time": self.resolution_time.isoformat(),
            "contracts": self.contracts,
            "average_price": str(self.average_price),
            "fee": str(self.fee),
            "cost": str(self.cost),
            "p_estimate": str(self.p_estimate),
            "net_edge_pp": str(self.net_edge_pp),
            "binding_cap": self.binding_cap,
            "resolved": self.resolved,
            "payout": str(self.payout),
            "pnl": str(self.pnl),
            "strike": str(self.strike) if self.strike is not None else None,
            "claim": self.claim_description,
            "settlement": self.settlement.as_dict(),
        }


@dataclass(frozen=True)
class EquityPoint:
    when: datetime
    realized_pnl: Decimal
    capital_deployed: Decimal
    free_capital: Decimal
    open_positions: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "when": self.when.isoformat(),
            "realized_pnl": str(self.realized_pnl),
            "capital_deployed": str(self.capital_deployed),
            "free_capital": str(self.free_capital),
            "open_positions": self.open_positions,
        }


@dataclass(frozen=True)
class CalibrationBucket:
    """One reliability-diagram bin: what we said vs what happened."""

    lower: float
    upper: float
    n: int
    mean_predicted: float
    observed_frequency: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "bucket": f"{self.lower:.1f}-{self.upper:.1f}",
            "lower": self.lower,
            "upper": self.upper,
            "n": self.n,
            "mean_predicted": round(self.mean_predicted, 4),
            "observed_frequency": round(self.observed_frequency, 4),
            "gap": round(self.observed_frequency - self.mean_predicted, 4),
        }


def calibration_buckets(
    trades: Iterable[BacktestTrade], *, bins: int = 10
) -> list[CalibrationBucket]:
    """Reliability bins over settled trades.

    Scored on the probability actually bet — ``p_side`` of the chosen side —
    because that is the number the money rode on.
    """
    settled = [t for t in trades if t.is_settled]
    out: list[CalibrationBucket] = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        inside = [
            t for t in settled
            if (lo <= float(t.p_estimate) < hi) or (i == bins - 1 and float(t.p_estimate) == 1.0)
        ]
        if not inside:
            continue
        out.append(
            CalibrationBucket(
                lower=lo,
                upper=hi,
                n=len(inside),
                mean_predicted=sum(float(t.p_estimate) for t in inside) / len(inside),
                observed_frequency=sum(1 for t in inside if t.won) / len(inside),
            )
        )
    return out


def brier_score(trades: Iterable[BacktestTrade]) -> float | None:
    """Mean squared error of the probability estimates. Lower is better.

    0.25 is what always saying "50%" scores. A model that cannot beat 0.25 is
    not adding information, whatever its P&L happens to have done.
    """
    settled = [t for t in trades if t.is_settled]
    if not settled:
        return None
    return sum((float(t.p_estimate) - (1.0 if t.won else 0.0)) ** 2 for t in settled) / len(settled)


@dataclass
class _OpenPosition:
    claim: Claim
    assessment: EdgeAssessment
    entry_time: datetime
    contracts: int
    cost: Decimal
    binding_cap: str
    strategy: str


@dataclass(frozen=True)
class BacktestResult:
    """Trades, and everything needed to judge whether they mean anything."""

    strategy: str
    trades: list[BacktestTrade]
    equity_curve: list[EquityPoint]
    settlement: SettlementReport
    skipped: dict[str, int]
    tape: dict[str, Any]
    parameters: dict[str, Any]

    @property
    def settled_trades(self) -> list[BacktestTrade]:
        return [t for t in self.trades if t.is_settled]

    @property
    def realized_pnl(self) -> Decimal:
        return sum((t.pnl for t in self.settled_trades), Decimal(0))

    @property
    def win_rate(self) -> float | None:
        settled = self.settled_trades
        return (sum(1 for t in settled if t.won) / len(settled)) if settled else None

    def honesty_warnings(self) -> list[str]:
        """Computed, not authored. The rules require these to travel with P&L,
        so they are derived from the data rather than remembered by a human."""
        warnings: list[str] = []
        settled = self.settled_trades
        days = float(self.tape.get("tape_span_days") or 0.0)

        if days < MIN_TAPE_DAYS:
            warnings.append(
                f"tape covers {days:.3f} days, far short of the ~{MIN_TAPE_DAYS:.0f} "
                "days needed for a result to be evidence of anything. This is a "
                "correctness check on the pipeline, not a measurement of edge."
            )
        if len(settled) < MIN_SETTLED_TRADES:
            warnings.append(
                f"{len(settled)} settled contracts, below the ~{MIN_SETTLED_TRADES} "
                "needed before a win rate or calibration curve is readable."
            )
        if not settled:
            warnings.append(
                "no contracts resolved within the tape's span, so no P&L exists to "
                "report — only the gating behaviour is exercised here."
            )
        if self.settlement.unsettled:
            warnings.append(
                f"{self.settlement.unsettled} positions could not be settled and are "
                "excluded from P&L rather than counted as losses."
            )
        if self.settlement.near_strike:
            warnings.append(
                f"{self.settlement.near_strike} of {self.settlement.settled} settlements "
                f"({self.settlement.proxy_risk_pct:.1f}%) landed close enough to the "
                "strike that the venue's own index could have resolved them the other "
                "way; the win rate is correspondingly soft."
            )
        return warnings

    def summary(self) -> dict[str, Any]:
        settled = self.settled_trades
        return {
            "strategy": self.strategy,
            "trades_opened": len(self.trades),
            "trades_settled": len(settled),
            "win_rate": round(self.win_rate, 4) if self.win_rate is not None else None,
            "realized_pnl": str(self.realized_pnl),
            "brier_score": (
                round(brier_score(self.trades), 5) if settled else None
            ),
            "brier_reference_always_50pct": 0.25,
            "capital_deployed": str(sum((t.cost for t in self.trades), Decimal(0))),
        }

    def as_dict(self, *, include_trades: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            # Deliberately first: the warnings frame every number below them.
            "honesty_warnings": self.honesty_warnings(),
            "summary": self.summary(),
            "tape": self.tape,
            "parameters": self.parameters,
            "settlement": self.settlement.as_dict(),
            "calibration": [b.as_dict() for b in calibration_buckets(self.trades)],
            "skipped": dict(sorted(self.skipped.items(), key=lambda kv: -kv[1])),
            "equity_curve": [p.as_dict() for p in self.equity_curve],
        }
        if include_trades:
            payload["trades"] = [t.as_dict() for t in self.trades]
        return payload


class BacktestEngine:
    """Replays a tape through a strategy and the full gate stack."""

    def __init__(
        self,
        *,
        strategy: BaseStrategy,
        registry: UnderlyingRegistry,
        data: MarketDataSet,
        settlement_source: SettlementSource,
        fee_model: KalshiFeeModel,
        gate_limits: GateLimits,
        sizing_limits: SizingLimits,
        max_positions: int = 6,
        max_slots_per_underlying: int = 2,
        vol_lookback_days: int = 30,
        recorded_results: RecordedStatusSettlement | None = None,
    ) -> None:
        self.strategy = strategy
        self.registry = registry
        self.data = data
        self.settlement_source = settlement_source
        self.fee_model = fee_model
        self.gate_limits = gate_limits
        self.sizing_limits = sizing_limits
        self.max_positions = max_positions
        self.max_slots_per_underlying = max_slots_per_underlying
        self.vol_lookback_days = vol_lookback_days
        self.recorded_results = recorded_results

        self._skipped: Counter[str] = Counter()

    # -- entry evaluation ---------------------------------------------

    def _best_assessment(
        self, claim: Claim, opinion_estimate, book: BinaryBook, when: datetime,
        capital_in_use: Decimal,
    ) -> tuple[EdgeAssessment | None, str]:
        """Size and assess each side; return the better passing one.

        Sizing has to happen per side because the two sides trade at different
        prices, and the contract count depends on the price. So each side is
        sized against its own book, then gated at that size — never sized at one
        price and gated at another.
        """
        best: EdgeAssessment | None = None
        best_cap = "none"
        for side in (Side.yes, Side.no):
            price = book.best_yes_ask if side is Side.yes else book.best_no_ask
            if price is None:
                continue
            depth = side_depth(book, side)
            plan = plan_size(
                price, self.fee_model, self.sizing_limits,
                book_depth=depth, capital_in_use=capital_in_use,
            )
            if not plan.tradeable:
                self._skipped[f"unsizeable_{plan.binding_cap.value}"] += 1
                continue
            assessment = assess_side(
                claim, opinion_estimate, book, side=side, contracts=plan.contracts,
                fee_model=self.fee_model, limits=self.gate_limits, now=when,
            )
            if not assessment.passed:
                for failure in assessment.failures:
                    self._skipped[f"gate_{failure.gate.value}"] += 1
                continue
            if best is None or assessment.net_edge_pp > best.net_edge_pp:
                best, best_cap = assessment, plan.binding_cap.value
        return best, best_cap

    # -- the replay ---------------------------------------------------

    def run(self, replay: TapeReplay) -> BacktestResult:
        open_positions: dict[str, _OpenPosition] = {}
        traded_tickers: set[str] = set()
        trades: list[BacktestTrade] = []
        equity: list[EquityPoint] = []
        settlements: list[Settlement] = []

        realized = Decimal(0)
        capital_in_use = Decimal(0)
        total_capital = self.sizing_limits.total_capital

        def settle_due(now: datetime) -> None:
            """Close every position whose claim has resolved by `now`.

            Runs before entries at each timestamp: settling second would hand
            the engine capital it did not yet have.
            """
            nonlocal realized, capital_in_use
            for ticker in [
                t for t, p in open_positions.items() if p.claim.resolution_time <= now
            ]:
                position = open_positions.pop(ticker)
                outcome = settle_claim(
                    position.claim, self.settlement_source, recorded=self.recorded_results
                )
                settlements.append(outcome)

                side_won = (
                    None if outcome.resolved is None
                    else (outcome.resolved if position.assessment.side is Side.yes
                          else not outcome.resolved)
                )
                payout = (
                    Decimal(position.contracts) if side_won else Decimal(0)
                ) if side_won is not None else Decimal(0)
                pnl = (payout - position.cost) if side_won is not None else Decimal(0)

                capital_in_use -= position.cost
                if side_won is not None:
                    realized += pnl

                trades.append(
                    BacktestTrade(
                        ticker=ticker,
                        underlying=position.claim.underlying,
                        strategy=position.strategy,
                        side=position.assessment.side,
                        entry_time=position.entry_time,
                        resolution_time=position.claim.resolution_time,
                        contracts=position.contracts,
                        average_price=position.assessment.average_price or Decimal(0),
                        fee=position.assessment.execution.fee
                        if position.assessment.execution else Decimal(0),
                        cost=position.cost,
                        p_estimate=position.assessment.p_side,
                        net_edge_pp=position.assessment.net_edge_pp,
                        binding_cap=position.binding_cap,
                        resolved=side_won,
                        payout=payout,
                        pnl=pnl,
                        settlement=outcome,
                        strike=(
                            position.claim.threshold
                            if position.claim.threshold is not None
                            else position.claim.lower_bound
                        ),
                        claim_description=position.claim.describe(),
                    )
                )
                equity.append(
                    EquityPoint(
                        when=now, realized_pnl=realized, capital_deployed=capital_in_use,
                        free_capital=total_capital + realized - capital_in_use,
                        open_positions=len(open_positions),
                    )
                )

        for observation in replay.observations():
            now = observation.observed_at
            settle_due(now)

            ticker = observation.ticker
            if ticker in traded_tickers:
                self._skipped["already_traded_this_contract"] += 1
                continue

            claim = replay.claim_as_of(ticker, now, self.registry)
            if claim is None:
                self._skipped["no_parseable_claim_at_this_time"] += 1
                continue
            if claim.resolution_time <= now:
                self._skipped["already_resolved"] += 1
                continue

            snapshot = self.data.snapshot_at(
                claim.underlying, now, lookback_days=self.vol_lookback_days
            )
            if snapshot is None:
                self._skipped["no_underlying_data_at_this_time"] += 1
                continue

            opinion = self.strategy.estimate(
                claim, StrategyContext(now=now, snapshot=snapshot, book=observation.book)
            )
            if opinion.abstained:
                self._skipped["strategy_abstained"] += 1
                continue

            # Portfolio limits, checked before sizing so a full book does not
            # burn work — and so the reason is recorded distinctly.
            if len(open_positions) >= self.max_positions:
                self._skipped["no_free_slot"] += 1
                continue
            same_underlying = sum(
                1 for p in open_positions.values() if p.claim.underlying == claim.underlying
            )
            if same_underlying >= self.max_slots_per_underlying:
                self._skipped["underlying_concentration_limit"] += 1
                continue

            assessment, binding_cap = self._best_assessment(
                claim, opinion.estimate, observation.book, now, capital_in_use
            )
            if assessment is None:
                continue

            cost = assessment.capital_at_risk
            if capital_in_use + cost > total_capital:
                self._skipped["insufficient_capital"] += 1
                continue

            open_positions[ticker] = _OpenPosition(
                claim=claim, assessment=assessment, entry_time=now,
                contracts=assessment.contracts_requested, cost=cost,
                binding_cap=binding_cap, strategy=self.strategy.name,
            )
            traded_tickers.add(ticker)
            capital_in_use += cost
            equity.append(
                EquityPoint(
                    when=now, realized_pnl=realized, capital_deployed=capital_in_use,
                    free_capital=total_capital + realized - capital_in_use,
                    open_positions=len(open_positions),
                )
            )

        # Anything still open at the end of the tape resolves in the future we
        # have not recorded. Settle what genuinely resolved; report the rest as
        # unsettled rather than assuming an outcome.
        _, tape_end = replay.span
        settle_due(tape_end)
        for ticker, position in list(open_positions.items()):
            settlements.append(
                Settlement(
                    ticker=ticker, resolved=None, settled_value=None,
                    source="unresolved_at_tape_end", near_strike=False,
                    distance_to_strike_pct=None,
                    reason=(
                        f"resolves {position.claim.resolution_time.isoformat()}, after "
                        "the tape ends; excluded from P&L rather than guessed"
                    ),
                )
            )

        return BacktestResult(
            strategy=self.strategy.name,
            trades=trades,
            equity_curve=equity,
            settlement=summarise_settlements(settlements),
            skipped=dict(self._skipped),
            tape=replay.summary(),
            parameters={
                "strategy": self.strategy.describe(),
                "gate": {
                    "min_net_edge_pp": str(self.gate_limits.min_net_edge_pp),
                    "margin_pp": str(self.gate_limits.margin_pp),
                    "required_edge_pp": str(self.gate_limits.required_edge_pp),
                    "min_book_depth_multiple": str(self.gate_limits.min_book_depth_multiple),
                    "max_book_participation_pct": str(
                        self.gate_limits.max_book_participation_pct
                    ),
                    "max_hours_to_resolution": str(self.gate_limits.max_hours_to_resolution),
                    "reject_deep_tail": self.gate_limits.reject_deep_tail,
                },
                "sizing": {
                    "mode": self.sizing_limits.mode.value,
                    "position_target": str(self.sizing_limits.position_target),
                    "per_position_ceiling": str(self.sizing_limits.per_position_ceiling),
                    "total_capital": str(self.sizing_limits.total_capital),
                    "fixed_contracts": self.sizing_limits.fixed_contracts,
                },
                "portfolio": {
                    "max_positions": self.max_positions,
                    "max_slots_per_underlying": self.max_slots_per_underlying,
                },
                "vol_lookback_days": self.vol_lookback_days,
                "settlement_source": self.settlement_source.name,
                "underlying_coverage": self.data.coverage(),
            },
        )
