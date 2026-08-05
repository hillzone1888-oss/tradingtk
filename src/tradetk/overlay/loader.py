"""Building the overlay from config, and degrading honestly when it cannot.

A missing config, an unreachable vault, or unparseable mail leaves the pipeline
trading exactly as it does today — the overlay only ever narrows, so its absence
is safe. What is *not* safe is that failure being invisible: an operator who
believes their research is steering trades, when the bridge silently died a week
ago, is making decisions on a false premise. Every failure is therefore carried
on the object and surfaced in output.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tradetk.overlay.policy import UnderlyingPolicy, build_policy
from tradetk.translation.edge import GateLimits
from tradetk.translation.sizing import SizingLimits

log = logging.getLogger("tradetk.overlay")


@dataclass
class VaultOverlay:
    """Approved mail, indexed by underlying. Degrades to a no-op."""

    base_gate: GateLimits
    base_sizing: SizingLimits
    stances: dict[str, Any] = field(default_factory=dict)
    catalysts: dict[str, list[Any]] = field(default_factory=dict)
    enabled: bool = False
    ok: bool = True
    error: str | None = None

    def for_underlying(self, underlying: str, now: datetime) -> UnderlyingPolicy:
        key = underlying.upper()
        return build_policy(
            key,
            stance=self.stances.get(key),
            catalysts=self.catalysts.get(key, []),
            base_gate=self.base_gate,
            base_sizing=self.base_sizing,
            now=now,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "ok": self.ok,
            "error": self.error,
            "stances": sorted(self.stances),
            "catalyst_underlyings": sorted(self.catalysts),
        }


def _index_mail(
    stances: list[Any], catalysts: list[Any]
) -> tuple[dict[str, Any], dict[str, list[Any]]]:
    """Fold flat mail lists into per-underlying lookups.

    Pure and exception-prone by nature (mail shape is not this module's to
    control), so the caller must run this *inside* its fail-open boundary —
    a malformed timestamp or a future attribute change here must still leave
    the pipeline trading, not raise out of ``load_overlay``.
    """
    by_underlying: dict[str, Any] = {}
    for s in stances:
        key = s.underlying.upper()
        # Deterministic tie-break: the most recently created stance wins.
        current = by_underlying.get(key)
        if current is None or s.stance.created > current.stance.created:
            by_underlying[key] = s

    cat_map: dict[str, list[Any]] = {}
    for c in catalysts:
        for sym in c.underlyings:
            cat_map.setdefault(sym.upper(), []).append(c)

    return by_underlying, cat_map


def load_overlay(
    cfg: Any,
    *,
    base_gate: GateLimits,
    base_sizing: SizingLimits,
    registry: Any | None = None,
    as_of: datetime | None = None,
    now: datetime | None = None,
) -> VaultOverlay:
    """Read approved mail, or return a reporting no-op if anything fails."""
    empty = VaultOverlay(base_gate=base_gate, base_sizing=base_sizing)
    if not getattr(cfg, "enabled", False):
        return empty

    empty.enabled = True
    try:
        from vaultpost import VaultPost, VaultPostConfig, VerifierRegistry

        vp_cfg = VaultPostConfig.from_yaml(cfg.config_path)
        post = VaultPost(vp_cfg, registry or VerifierRegistry())
        ref = now or datetime.now(tz=as_of.tzinfo if as_of else None)
        stances = post.read_stances(now=ref, as_of=as_of)
        catalysts = post.read_catalysts(now=ref, as_of=as_of)
        # Indexing runs inside the boundary too: a malformed record must fail
        # open, not raise out of a function whose whole purpose is not to.
        by_underlying, cat_map = _index_mail(stances, catalysts)
    except Exception as exc:  # noqa: BLE001 - a broken bridge must not halt trading
        log.warning("vault overlay unavailable, trading unmodified: %s", exc)
        empty.ok = False
        empty.error = f"{type(exc).__name__}: {exc}"
        return empty

    return VaultOverlay(
        base_gate=base_gate, base_sizing=base_sizing, stances=by_underlying,
        catalysts=cat_map, enabled=True, ok=True,
    )
