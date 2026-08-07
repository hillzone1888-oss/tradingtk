# src/tradetk/translation/assessment.py
"""The per-side assessment loop, shared by backtest, paper, and propose.

Size each side against its own book, gate it at that size, keep the better
passing side. Extracted from ``BacktestEngine._best_assessment`` when propose
became the third consumer; the backtest suite is the behaviour oracle.

Pure: instead of incrementing a skip counter, the loop returns the ordered
tuple of skip-reason strings it produced, and each caller folds them into its
own accounting. ``overlay`` is duck-typed (``for_underlying(underlying, when)``
returning a policy with ``blocked``/``gate_limits``/``sizing_limits``/
``allowed_sides``) so this module never imports ``overlay/`` and stays a leaf.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from tradetk.translation.edge import EdgeAssessment, GateLimits, assess_side, side_depth
from tradetk.translation.sizing import SizingLimits, plan_size
from tradetk.venues.base import BinaryBook, Side


@dataclass(frozen=True)
class CandidateOutcome:
    """What the loop decided, and every reason it said no along the way."""

    assessment: EdgeAssessment | None
    binding_cap: str
    skips: tuple[str, ...]


def assess_candidate(
    claim: Any, estimate: Any, book: BinaryBook, when: datetime,
    capital_in_use: Decimal, *,
    gate_limits: GateLimits, sizing_limits: SizingLimits, fee_model: Any,
    overlay: Any = None,
) -> CandidateOutcome:
    """Size and assess each side; return the better passing one.

    Sizing has to happen per side because the two sides trade at different
    prices, and the contract count depends on the price. So each side is
    sized against its own book, then gated at that size — never sized at one
    price and gated at another.

    When a vault overlay is present, its verdict is resolved once for this
    claim and then *narrows* the loop: a blocked underlying assesses no
    side, a bias forbids the side that contradicts it, risk shrinks the
    sizing target, and a catalyst raises the gate. With no overlay every
    value below is exactly the global limit, so the behaviour is unchanged.
    """
    skips: list[str] = []
    allowed: tuple[Side, ...] | None = None
    if overlay is not None:
        policy = overlay.for_underlying(claim.underlying, when)
        if policy.blocked:
            skips.append("overlay_blocked")
            return CandidateOutcome(None, "none", tuple(skips))
        gate_limits = policy.gate_limits
        sizing_limits = policy.sizing_limits
        allowed = policy.allowed_sides(claim)

    best: EdgeAssessment | None = None
    best_cap = "none"
    for side in (Side.yes, Side.no):
        if allowed is not None and side not in allowed:
            skips.append("overlay_side_forbidden")
            continue
        price = book.best_yes_ask if side is Side.yes else book.best_no_ask
        if price is None:
            continue
        depth = side_depth(book, side)
        plan = plan_size(
            price, fee_model, sizing_limits,
            book_depth=depth, capital_in_use=capital_in_use,
        )
        if not plan.tradeable:
            skips.append(f"unsizeable_{plan.binding_cap.value}")
            continue
        assessment = assess_side(
            claim, estimate, book, side=side, contracts=plan.contracts,
            fee_model=fee_model, limits=gate_limits, now=when,
        )
        if not assessment.passed:
            for failure in assessment.failures:
                skips.append(f"gate_{failure.gate.value}")
            continue
        if best is None or assessment.net_edge_pp > best.net_edge_pp:
            best, best_cap = assessment, plan.binding_cap.value
    return CandidateOutcome(best, best_cap, tuple(skips))
