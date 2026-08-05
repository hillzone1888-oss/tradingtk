"""One underlying's policy: what the vault permits, and why.

Three dials, all of which narrow. `bias` restricts which side may be bought,
`risk` shrinks the position target, and a catalyst raises the edge a trade must
clear. Nothing here can widen a limit or permit a trade the pipeline would
otherwise refuse — an overlay that could grant permission would be a way for a
note to override a tested gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from tradetk.overlay.direction import bullish_side
from tradetk.translation.claims import Claim
from tradetk.translation.edge import GateLimits
from tradetk.translation.sizing import SizingLimits
from tradetk.venues.base import Side

BOTH_SIDES = (Side.yes, Side.no)
PP = Decimal(100)


@dataclass(frozen=True)
class UnderlyingPolicy:
    """What the vault permits for one underlying, with its provenance."""

    underlying: str
    bias: str | None
    sizing_limits: SizingLimits
    gate_limits: GateLimits
    blocked: bool
    reasons: tuple[str, ...] = ()
    source_mail: tuple[str, ...] = ()

    def allowed_sides(self, claim: Claim) -> tuple[Side, ...]:
        """Sides permitted for one specific claim.

        Resolved per claim rather than stored, because which side expresses a
        bullish view depends on the claim's operator.
        """
        if self.blocked:
            return ()
        if self.bias is None or self.bias == "neutral":
            return BOTH_SIDES
        up = bullish_side(claim)
        if up is None:
            return BOTH_SIDES  # not a directional claim; a stance says nothing
        down = Side.no if up is Side.yes else Side.yes
        return (up,) if self.bias == "bullish" else (down,)

    def as_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "bias": self.bias,
            "blocked": self.blocked,
            "position_target": str(self.sizing_limits.position_target),
            "required_edge_pp": str(self.gate_limits.required_edge_pp),
            "reasons": list(self.reasons),
            "source_mail": list(self.source_mail),
        }


def build_policy(
    underlying: str,
    *,
    stance: Any | None,
    catalysts: list[Any],
    base_gate: GateLimits,
    base_sizing: SizingLimits,
    now: datetime,
) -> UnderlyingPolicy:
    """Fold approved mail into one policy. No mail means no change at all."""
    reasons: list[str] = []
    source: list[str] = []
    blocked = False
    sizing = base_sizing
    gate = base_gate
    bias: str | None = None

    if stance is not None:
        bias = stance.bias
        risk = int(stance.effective_risk)
        source.append(stance.stance.id)
        if risk <= 0:
            blocked = True
            reasons.append(f"{stance.stance.id}: risk 0 — stand aside")
        else:
            target = base_sizing.position_target * Decimal(risk) / PP
            cap = getattr(stance.stance, "max_position_dollars", None)
            if cap is not None:
                # Every dial narrows: the cap may only shrink the target.
                target = min(target, Decimal(str(cap)))
            sizing = replace_target(base_sizing, target)
            reasons.append(
                f"{stance.stance.id}: {bias}, effective risk {risk} -> "
                f"target {target}"
            )

    for cat in catalysts:
        if not (cat.window_start <= now <= cat.window_end):
            continue
        source.append(cat.id)
        if cat.action.value == "block":
            blocked = True
            reasons.append(f"{cat.id}: {cat.event} — entries blocked in window")
        else:
            extra = Decimal(str(cat.extra_margin_pp or 0))
            gate = replace_margin(gate, gate.margin_pp + extra)
            reasons.append(
                f"{cat.id}: {cat.event} — +{extra}pp edge required in window"
            )

    return UnderlyingPolicy(
        underlying=underlying, bias=bias, sizing_limits=sizing, gate_limits=gate,
        blocked=blocked, reasons=tuple(reasons), source_mail=tuple(source),
    )


def replace_target(limits: SizingLimits, target: Decimal) -> SizingLimits:
    """A copy with a new position target; every other cap is untouched."""
    return SizingLimits(
        position_target=target,
        per_position_ceiling=limits.per_position_ceiling,
        total_capital=limits.total_capital,
        max_book_participation_pct=limits.max_book_participation_pct,
        min_order_contracts=limits.min_order_contracts,
        mode=limits.mode,
        fixed_contracts=limits.fixed_contracts,
    )


def replace_margin(limits: GateLimits, margin_pp: Decimal) -> GateLimits:
    """A copy with a larger cushion; every other threshold is untouched."""
    return GateLimits(
        min_net_edge_pp=limits.min_net_edge_pp,
        margin_pp=margin_pp,
        min_book_depth_multiple=limits.min_book_depth_multiple,
        max_book_participation_pct=limits.max_book_participation_pct,
        max_hours_to_resolution=limits.max_hours_to_resolution,
        reject_deep_tail=limits.reject_deep_tail,
    )
