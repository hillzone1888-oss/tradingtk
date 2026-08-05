# Vault Overlay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let approved vault-post mail narrow what tradetk proposes — bias restricts the side, risk scales the position target, catalysts raise the required edge — without touching the probability math.

**Architecture:** A new pure `tradetk.overlay` package turns approved stances and catalysts into an `UnderlyingPolicy` per symbol. `shadow` records that verdict without filtering anything; `backtest` applies it. tradetk registers the four market-data verifiers that vault-post's evidence gate calls.

**Tech Stack:** Python 3.12 via uv, pydantic v2, `vaultpost` as a local editable path dependency.

## Global Constraints

- Python >= 3.12 via `uv`. Corporate MITM root CA: run uv with `--system-certs` (`--native-tls` is deprecated).
- ruff `line-length = 100`; enforced select `E4, E7, E9, F`.
- **Never touch `execute` or any venue order path.** This work is read-only.
- **The probability math is never modified.** No overlay code may change `p`.
- **Every dial narrows.** The overlay may only make the system more selective. If a change would let something trade that would not have traded before, it is wrong.
- **`shadow` must never filter.** Records are annotated, never suppressed.
- Fail-open but never fail-silent: a broken bridge trades normally *and* reports `ok: false`.
- Never commit credentials, `data/tape/`, or a `.env`.
- Full suite green before every commit.
- Every commit ends with:
  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01FLkAHjVY1jJn1kMtpTVc3X
  ```

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | `vaultpost` path dependency |
| `src/tradetk/config/schema.py` | `VaultOverlayConfig` block |
| `src/tradetk/overlay/__init__.py` | package exports |
| `src/tradetk/overlay/direction.py` | `bullish_side(claim)` — operator-aware |
| `src/tradetk/overlay/policy.py` | `UnderlyingPolicy`, dial mapping, pure |
| `src/tradetk/overlay/loader.py` | build `VaultOverlay` from config; fail-open |
| `src/tradetk/overlay/verifiers.py` | four Hyperliquid-backed verifiers |
| `src/tradetk/shadow/records.py` | `overlay` annotation field |
| `src/tradetk/shadow/evaluator.py` | record the verdict; filter nothing |
| `src/tradetk/cli/shadow.py` | load the overlay (as-of tape start) and annotate |
| `src/tradetk/backtest/engine.py` | resolve limits per claim; apply the verdict |
| `src/tradetk/cli/backtest.py` | load the overlay (as-of replay start) and act |
| `src/tradetk/cli/record.py` | capture a vault snapshot each poll |

---

### Task 1: Dependency and config block

**Files:**
- Modify: `pyproject.toml`, `src/tradetk/config/schema.py`, `config/config.example.yaml`
- Test: `tests/test_config_vault_overlay.py`

**Interfaces:**
- Produces: `VaultOverlayConfig` with `enabled: bool = False`, `config_path: str = "../vault-post/config/config.yaml"`; `Config.vault_overlay: VaultOverlayConfig = VaultOverlayConfig()`

- [ ] **Step 1: Add the path dependency to `pyproject.toml`**

Add `"vaultpost"` to `[project].dependencies`, then add at the end of the file:

```toml
[tool.uv.sources]
vaultpost = { path = "../vault-post", editable = true }
```

- [ ] **Step 2: Verify the dependency resolves**

Run: `uv sync --system-certs`
Then: `uv run --system-certs python -c "import vaultpost; print(vaultpost.__version__)"`
Expected: `0.1.0`

- [ ] **Step 3: Write the failing test**

```python
# tests/test_config_vault_overlay.py
"""The overlay is opt-in and defaults to off, so nothing changes until asked."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tradetk.config.schema import VaultOverlayConfig


def test_defaults_to_disabled() -> None:
    """An unconfigured install must behave exactly as it does today."""
    cfg = VaultOverlayConfig()
    assert cfg.enabled is False


def test_default_path_points_at_the_sibling_repo() -> None:
    assert VaultOverlayConfig().config_path.endswith("config.yaml")


def test_unknown_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        VaultOverlayConfig.model_validate({"enabled": True, "typo_key": 1})
```

- [ ] **Step 4: Run test to verify it fails**

Run: `uv run --system-certs python -m pytest tests/test_config_vault_overlay.py -v`
Expected: FAIL with `ImportError: cannot import name 'VaultOverlayConfig'`

- [ ] **Step 5: Add `VaultOverlayConfig` to `src/tradetk/config/schema.py`**

Add the class next to `RecorderConfig`:

```python
class VaultOverlayConfig(_Strict):
    """Researched views from the Second Brain, via vault-post.

    Off by default: the pipeline is complete without it, and the overlay only
    ever narrows what the pipeline would otherwise do.
    """

    enabled: bool = False
    config_path: str = "../vault-post/config/config.yaml"
```

And add the field to `Config`, after `paths`:

```python
    vault_overlay: VaultOverlayConfig = VaultOverlayConfig()
```

- [ ] **Step 6: Document it in `config/config.example.yaml`**

Append:

```yaml
vault_overlay:
  # Researched stances and catalysts from the Second Brain, via vault-post.
  # Off by default. When on, approved mail can only make the system MORE
  # selective: it restricts the side, shrinks the position, or demands more
  # edge. It can never permit a trade the pipeline would otherwise refuse.
  enabled: false
  config_path: "../vault-post/config/config.yaml"
```

- [ ] **Step 7: Run the full suite**

Run: `uv run --system-certs python -m pytest -q`
Expected: all PASS (382 + 3 new)

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock src/tradetk/config/schema.py config/config.example.yaml tests/test_config_vault_overlay.py
git commit -m "$(cat <<'EOF'
Add vault-post path dependency and an opt-in vault_overlay config block

Off by default, so an unconfigured install behaves exactly as it does today.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FLkAHjVY1jJn1kMtpTVc3X
EOF
)"
```

---

### Task 2: Direction — which side expresses a bullish view

This is the task most likely to invert a stance if rushed. A NO on "BTC **below**
90k" is a *bullish* bet, so bias cannot map onto `Side` directly.

**Files:**
- Create: `src/tradetk/overlay/__init__.py`, `src/tradetk/overlay/direction.py`
- Test: `tests/test_overlay_direction.py`

**Interfaces:**
- Consumes: `Claim`, `ClaimOperator` from `tradetk.translation.claims`; `Side` from `tradetk.venues.base`
- Produces: `bullish_side(claim: Claim) -> Side | None` — `None` means the claim is not directional

- [ ] **Step 1: Write the failing test**

```python
# tests/test_overlay_direction.py
"""Which side is the bullish bet depends on the claim's operator, not the side.

Getting this wrong inverts a stance on half the universe, so every operator is
pinned explicitly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tradetk.overlay.direction import bullish_side
from tradetk.translation.claims import Claim, ClaimOperator
from tradetk.venues.base import Side

NOW = datetime(2026, 8, 4, 22, 0, tzinfo=timezone.utc)


def _claim(operator: ClaimOperator, **over) -> Claim:
    base = dict(
        ticker="KXBTCD-T", series_ticker="KXBTCD", underlying="BTC",
        operator=operator, resolution_time=NOW, resolution_source="CF Benchmarks BRTI",
        rules_primary="rules", threshold=Decimal("100000"),
    )
    base.update(over)
    return Claim(**base)


def test_above_claim_is_bullish_on_yes() -> None:
    assert bullish_side(_claim(ClaimOperator.above)) is Side.yes


def test_at_or_above_claim_is_bullish_on_yes() -> None:
    assert bullish_side(_claim(ClaimOperator.at_or_above)) is Side.yes


def test_below_claim_is_bullish_on_NO() -> None:
    """The subtle one: 'BTC below 90k' resolving NO means BTC held up."""
    assert bullish_side(_claim(ClaimOperator.below)) is Side.no


def test_between_claim_is_not_directional() -> None:
    """A range bet is neither bullish nor bearish; a stance must not touch it."""
    claim = _claim(
        ClaimOperator.between, threshold=None,
        lower_bound=Decimal("90000"), upper_bound=Decimal("100000"),
    )
    assert bullish_side(claim) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --system-certs python -m pytest tests/test_overlay_direction.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tradetk.overlay'`

- [ ] **Step 3: Create `src/tradetk/overlay/__init__.py`**

```python
"""Researched views from the vault, narrowing what the pipeline proposes.

Nothing in this package touches probability estimation. The overlay only
restricts which side may be bought, shrinks the position target, or raises the
edge a trade must clear — every one of which narrows. A stance can never permit
a trade the pipeline would otherwise refuse.
"""
```

- [ ] **Step 4: Write `src/tradetk/overlay/direction.py`**

```python
"""Which side of a contract expresses a bullish view.

`bias` cannot map onto `Side` directly. YES on "BTC above 100k" and NO on
"BTC below 90k" are both bullish bets, because the claim's operator already
carries a direction. Resolving this in one tested function keeps the inversion
risk in a single place rather than scattered through call sites.
"""

from __future__ import annotations

from tradetk.translation.claims import Claim, ClaimOperator
from tradetk.venues.base import Side

_BULLISH_BY_OPERATOR = {
    ClaimOperator.above: Side.yes,
    ClaimOperator.at_or_above: Side.yes,
    ClaimOperator.below: Side.no,
}


def bullish_side(claim: Claim) -> Side | None:
    """The side that pays when the underlying goes up.

    Returns ``None`` for claims that are not directional at all: a ``between``
    claim wins on the price staying inside a range, which is neither a bullish
    nor a bearish view, so a directional stance has nothing to say about it.
    """
    return _BULLISH_BY_OPERATOR.get(claim.operator)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --system-certs python -m pytest tests/test_overlay_direction.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
git add src/tradetk/overlay/__init__.py src/tradetk/overlay/direction.py tests/test_overlay_direction.py
git commit -m "$(cat <<'EOF'
Resolve bullish side through the claim operator, not the raw side

NO on "BTC below 90k" is a bullish bet. Mapping bias straight onto Side would
invert a stance across half the universe, so the mapping lives in one tested
function and `between` claims are explicitly non-directional.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FLkAHjVY1jJn1kMtpTVc3X
EOF
)"
```

---

### Task 3: `UnderlyingPolicy` and the dial mapping

**Files:**
- Create: `src/tradetk/overlay/policy.py`
- Test: `tests/test_overlay_policy.py`

**Interfaces:**
- Consumes: `bullish_side` from `overlay.direction`; `GateLimits` from `translation.edge`; `SizingLimits` from `translation.sizing`; `Claim` from `translation.claims`; `Side` from `venues.base`; `Bias`, `Catalyst` from `vaultpost`
- Produces: `UnderlyingPolicy` dataclass with fields `underlying`, `bias`, `sizing_limits`, `gate_limits`, `blocked`, `reasons`, `source_mail`, method `allowed_sides(claim) -> tuple[Side, ...]`, and `as_dict()`; function `build_policy(underlying, *, stance, catalysts, base_gate, base_sizing, now) -> UnderlyingPolicy` where `stance` is an `ApprovedStance | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_overlay_policy.py
"""Every dial narrows. No mail must be a byte-for-byte no-op."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from vaultpost.schema import Bias, Catalyst

from tradetk.overlay.policy import build_policy
from tradetk.translation.claims import Claim, ClaimOperator
from tradetk.translation.edge import GateLimits
from tradetk.translation.sizing import SizingLimits
from tradetk.venues.base import Side

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


class _FakeStance:
    """Stands in for vaultpost.ApprovedStance without needing a vault."""

    def __init__(self, bias: Bias, effective_risk: int, max_dollars=None) -> None:
        self.bias = bias.value
        self.effective_risk = effective_risk
        self.stance = type("S", (), {
            "id": "stance-btc-a", "max_position_dollars": max_dollars,
        })()


def _catalyst(action: str, *, start_offset_h: float, end_offset_h: float,
              margin=2.0) -> Catalyst:
    return Catalyst.model_validate({
        "id": "cat-fomc", "type": "catalyst", "from_agent": "daily-sweep",
        "created": NOW, "status": "approved", "review_by": "2026-12-31",
        "underlyings": ["BTC"], "event": "FOMC",
        "window_start": NOW + timedelta(hours=start_offset_h),
        "window_end": NOW + timedelta(hours=end_offset_h),
        "action": action,
        **({"extra_margin_pp": margin} if action == "widen_edge" else {}),
        "evidence": [{
            "class": "event", "claim": "FOMC", "source_tier": "primary",
            "source_url": "https://federalreserve.gov/x",
            "datum": {"value": "x", "unit": "date", "date": "2026-08-04"},
            "observed_at": NOW,
        }],
    })


def _claim(operator=ClaimOperator.above) -> Claim:
    return Claim(
        ticker="KXBTCD-T", series_ticker="KXBTCD", underlying="BTC",
        operator=operator, resolution_time=NOW, resolution_source="CF Benchmarks BRTI",
        rules_primary="rules", threshold=Decimal("100000"),
    )


def _policy(stance=None, catalysts=()):
    return build_policy(
        "BTC", stance=stance, catalysts=list(catalysts),
        base_gate=BASE_GATE, base_sizing=BASE_SIZING, now=NOW,
    )


# ── the identity case ──────────────────────────────────────────────


def test_no_mail_is_a_no_op() -> None:
    """An empty vault must leave the pipeline exactly as it was."""
    p = _policy()
    assert p.blocked is False
    assert p.bias is None
    assert p.gate_limits == BASE_GATE
    assert p.sizing_limits == BASE_SIZING


def test_no_mail_allows_both_sides() -> None:
    assert set(_policy().allowed_sides(_claim())) == {Side.yes, Side.no}


# ── bias restricts the side, through the operator ──────────────────


def test_bearish_stance_allows_only_no_on_an_above_claim() -> None:
    p = _policy(_FakeStance(Bias.bearish, 50))
    assert p.allowed_sides(_claim(ClaimOperator.above)) == (Side.no,)


def test_bearish_stance_allows_only_YES_on_a_below_claim() -> None:
    """The inversion case: 'BTC below 90k' resolving YES is the bearish bet."""
    p = _policy(_FakeStance(Bias.bearish, 50))
    assert p.allowed_sides(_claim(ClaimOperator.below)) == (Side.yes,)


def test_bullish_stance_allows_only_yes_on_an_above_claim() -> None:
    p = _policy(_FakeStance(Bias.bullish, 50))
    assert p.allowed_sides(_claim(ClaimOperator.above)) == (Side.yes,)


def test_neutral_stance_allows_both_sides() -> None:
    """neutral is 'no directional view', never a brake."""
    p = _policy(_FakeStance(Bias.neutral, 50))
    assert set(p.allowed_sides(_claim())) == {Side.yes, Side.no}


def test_directional_stance_leaves_between_claims_alone() -> None:
    claim = Claim(
        ticker="KXBTCD-R", series_ticker="KXBTCD", underlying="BTC",
        operator=ClaimOperator.between, resolution_time=NOW,
        resolution_source="CF Benchmarks BRTI", rules_primary="rules",
        lower_bound=Decimal("90000"), upper_bound=Decimal("100000"),
    )
    p = _policy(_FakeStance(Bias.bearish, 50))
    assert set(p.allowed_sides(claim)) == {Side.yes, Side.no}


# ── risk scales the size ───────────────────────────────────────────


def test_risk_scales_the_position_target() -> None:
    p = _policy(_FakeStance(Bias.bearish, 50))
    assert p.sizing_limits.position_target == Decimal("1.00")


def test_risk_zero_blocks() -> None:
    p = _policy(_FakeStance(Bias.neutral, 0))
    assert p.blocked is True


def test_max_position_dollars_is_a_ceiling_not_a_floor() -> None:
    """The per-stance cap may only shrink the target."""
    p = _policy(_FakeStance(Bias.bearish, 100, max_dollars=0.75))
    assert p.sizing_limits.position_target == Decimal("0.75")


def test_max_position_dollars_never_raises_the_target() -> None:
    p = _policy(_FakeStance(Bias.bearish, 25, max_dollars=99.0))
    assert p.sizing_limits.position_target == Decimal("0.50")


# ── catalysts gate the edge ────────────────────────────────────────


def test_catalyst_raises_required_edge_inside_its_window() -> None:
    p = _policy(catalysts=[_catalyst("widen_edge", start_offset_h=-1, end_offset_h=1)])
    assert p.gate_limits.required_edge_pp == BASE_GATE.required_edge_pp + Decimal("2.0")


def test_catalyst_does_nothing_outside_its_window() -> None:
    p = _policy(catalysts=[_catalyst("widen_edge", start_offset_h=48, end_offset_h=50)])
    assert p.gate_limits == BASE_GATE


def test_blocking_catalyst_blocks_inside_its_window() -> None:
    p = _policy(catalysts=[_catalyst("block", start_offset_h=-1, end_offset_h=1)])
    assert p.blocked is True


# ── provenance ─────────────────────────────────────────────────────


def test_policy_names_the_mail_that_moved_a_number() -> None:
    p = _policy(_FakeStance(Bias.bearish, 40))
    assert "stance-btc-a" in p.source_mail
    assert any("40" in r for r in p.reasons)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --system-certs python -m pytest tests/test_overlay_policy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tradetk.overlay.policy'`

- [ ] **Step 3: Write `src/tradetk/overlay/policy.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --system-certs python -m pytest tests/test_overlay_policy.py -v`
Expected: PASS (15 tests)

- [ ] **Step 5: Commit**

```bash
git add src/tradetk/overlay/policy.py tests/test_overlay_policy.py
git commit -m "$(cat <<'EOF'
UnderlyingPolicy: fold approved mail into limits that only ever narrow

bias restricts the side through the claim operator, risk scales the position
target, and a catalyst raises the required edge inside its window. No mail
produces limits identical to the globals, so an empty vault is a no-op.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FLkAHjVY1jJn1kMtpTVc3X
EOF
)"
```

---

### Task 4: The loader — fail-open, never fail-silent

**Files:**
- Create: `src/tradetk/overlay/loader.py`
- Test: `tests/test_overlay_loader.py`

**Interfaces:**
- Consumes: `build_policy`, `UnderlyingPolicy` from `overlay.policy`
- Produces: `VaultOverlay` with `for_underlying(symbol, now) -> UnderlyingPolicy`, `ok: bool`, `error: str | None`, `as_dict()`; `load_overlay(cfg, *, base_gate, base_sizing, registry=None, as_of=None) -> VaultOverlay`. A disabled or broken overlay returns one whose `for_underlying` always yields the identity policy.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_overlay_loader.py
"""A broken bridge must trade normally AND say so. Never fail silent."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tradetk.config.schema import VaultOverlayConfig
from tradetk.overlay.loader import load_overlay
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --system-certs python -m pytest tests/test_overlay_loader.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tradetk.overlay.loader'`

- [ ] **Step 3: Write `src/tradetk/overlay/loader.py`**

```python
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
    except Exception as exc:  # noqa: BLE001 - a broken bridge must not halt trading
        log.warning("vault overlay unavailable, trading unmodified: %s", exc)
        empty.ok = False
        empty.error = f"{type(exc).__name__}: {exc}"
        return empty

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

    return VaultOverlay(
        base_gate=base_gate, base_sizing=base_sizing, stances=by_underlying,
        catalysts=cat_map, enabled=True, ok=True,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --system-certs python -m pytest tests/test_overlay_loader.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/tradetk/overlay/loader.py tests/test_overlay_loader.py
git commit -m "$(cat <<'EOF'
Overlay loader: fail open, but never fail silent

A missing config or unreachable vault leaves the pipeline trading as it does
today, because the overlay only narrows and its absence is safe. The failure is
carried on the object and surfaced, so an operator can never believe their
research is steering trades after the bridge has died.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FLkAHjVY1jJn1kMtpTVc3X
EOF
)"
```

---

### Task 5: The four verifiers

Without these the `technical` evidence class scores zero everywhere and
vault-post's reproducibility mechanism is never exercised against real data.

**Files:**
- Create: `src/tradetk/overlay/verifiers.py`
- Test: `tests/test_overlay_verifiers.py`

**Interfaces:**
- Consumes: `HyperliquidProvider` from `tradetk.signals.hyperliquid`
- Produces: `build_registry(provider_factory=None) -> VerifierRegistry` registering `tradetk.spot`, `tradetk.realized_vol`, `tradetk.price_change_pct`, `tradetk.funding`. Each verifier has signature `(params: dict, value: float) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_overlay_verifiers.py
"""Evidence that cannot be reproduced does not score."""

from __future__ import annotations

from tradetk.overlay.verifiers import build_registry


class _FakeProvider:
    def __init__(self, *, spot=100_000.0, vol=0.55, boom=False) -> None:
        self._spot, self._vol, self._boom = spot, vol, boom

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def spot_price(self, symbol):
        if self._boom:
            raise RuntimeError("provider down")
        return self._spot

    def realized_vol(self, symbol, lookback_days, interval="1h"):
        if self._boom:
            raise RuntimeError("provider down")
        return type("RV", (), {"sigma_annual": self._vol})()


def _reg(**kw):
    return build_registry(provider_factory=lambda: _FakeProvider(**kw))


def test_spot_within_tolerance_verifies() -> None:
    fn = _reg(spot=100_000.0).get("tradetk.spot")
    assert fn({"symbol": "BTC", "tolerance_pct": 1.0}, 100_400.0) is True


def test_spot_outside_tolerance_fails() -> None:
    """A number that does not reproduce must never pass silently."""
    fn = _reg(spot=100_000.0).get("tradetk.spot")
    assert fn({"symbol": "BTC", "tolerance_pct": 1.0}, 120_000.0) is False


def test_realized_vol_within_tolerance_verifies() -> None:
    fn = _reg(vol=0.55).get("tradetk.realized_vol")
    assert fn({"symbol": "BTC", "lookback_days": 30, "tolerance_pct": 10.0}, 0.57) is True


def test_unreachable_provider_fails_closed() -> None:
    """Evidence that could not be checked is not evidence."""
    fn = _reg(boom=True).get("tradetk.spot")
    assert fn({"symbol": "BTC"}, 100_000.0) is False


def test_all_four_verifiers_are_registered() -> None:
    reg = _reg()
    for name in ("tradetk.spot", "tradetk.realized_vol",
                 "tradetk.price_change_pct", "tradetk.funding"):
        assert name in reg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --system-certs python -m pytest tests/test_overlay_verifiers.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tradetk.overlay.verifiers'`

- [ ] **Step 3: Write `src/tradetk/overlay/verifiers.py`**

```python
"""Recomputing the technical evidence a stance cites.

vault-post defines the verifier contract and knows nothing about market data;
this module supplies the implementations, because tradetk is what owns the data.

Every verifier answers one question: does the live number reproduce the claimed
one, within tolerance? Anything it cannot check — an unreachable provider, an
unknown symbol, a malformed parameter — answers ``False``. Evidence that could
not be verified must not score, because "I could not check" and "I checked and
it holds" are not the same claim.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from vaultpost import VerifierRegistry

from tradetk.signals.hyperliquid import HyperliquidProvider

log = logging.getLogger("tradetk.overlay.verifiers")

DEFAULT_TOLERANCE_PCT = 2.0


def _within(actual: float, claimed: float, tolerance_pct: float) -> bool:
    if actual == 0:
        return abs(claimed) <= 1e-9
    return abs(actual - claimed) / abs(actual) * 100.0 <= tolerance_pct


def _guard(fn: Callable[..., bool]) -> Callable[[dict, float], bool]:
    """Any failure to check is a failure to verify."""

    def wrapped(params: dict, value: float) -> bool:
        try:
            return bool(fn(params, float(value)))
        except Exception as exc:  # noqa: BLE001 - unverifiable is not verified
            log.info("verifier could not check evidence: %s", exc)
            return False

    return wrapped


def build_registry(provider_factory: Any | None = None) -> VerifierRegistry:
    """Register every verifier tradetk can back with real data."""
    factory = provider_factory or HyperliquidProvider
    reg = VerifierRegistry()

    def spot(params: dict, value: float) -> bool:
        with factory() as p:
            actual = float(p.spot_price(params["symbol"]))
        return _within(actual, value, params.get("tolerance_pct", DEFAULT_TOLERANCE_PCT))

    def realized_vol(params: dict, value: float) -> bool:
        with factory() as p:
            rv = p.realized_vol(params["symbol"], int(params.get("lookback_days", 30)))
        return _within(
            float(rv.sigma_annual), value, params.get("tolerance_pct", 10.0)
        )

    def price_change_pct(params: dict, value: float) -> bool:
        hours = float(params.get("hours", 24))
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        with factory() as p:
            candles = p.candles(
                params["symbol"], params.get("interval", "1h"),
                int(start.timestamp() * 1000), int(end.timestamp() * 1000),
            )
        if len(candles) < 2:
            return False
        rows = sorted(candles, key=lambda k: k.open_ms)
        first, last = float(rows[0].o), float(rows[-1].c)
        if first == 0:
            return False
        actual = (last - first) / first * 100.0
        return abs(actual - value) <= float(params.get("tolerance_pp", 1.0))

    def funding(params: dict, value: float) -> bool:
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=float(params.get("hours", 8)))
        with factory() as p:
            points = p.funding_history(
                params["symbol"], int(start.timestamp() * 1000),
                int(end.timestamp() * 1000),
            )
        if not points:
            return False
        latest = sorted(points, key=lambda f: f.time_ms)[-1]
        return abs(float(latest.rate) - value) <= float(params.get("tolerance", 0.0001))

    reg.register("tradetk.spot", _guard(spot))
    reg.register("tradetk.realized_vol", _guard(realized_vol))
    reg.register("tradetk.price_change_pct", _guard(price_change_pct))
    reg.register("tradetk.funding", _guard(funding))
    return reg
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --system-certs python -m pytest tests/test_overlay_verifiers.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/tradetk/overlay/verifiers.py tests/test_overlay_verifiers.py
git commit -m "$(cat <<'EOF'
Register four Hyperliquid-backed verifiers for vault-post evidence

vault-post defines the contract; tradetk owns the data and supplies the
implementations. Anything that cannot be checked answers False, because "I
could not check" and "I checked and it holds" are different claims.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FLkAHjVY1jJn1kMtpTVc3X
EOF
)"
```

---

### Task 6: Shadow annotates, and never filters

**Files:**
- Modify: `src/tradetk/shadow/records.py`, `src/tradetk/shadow/evaluator.py`, `src/tradetk/cli/shadow.py`
- Test: `tests/test_shadow_overlay.py`

**Interfaces:**
- Consumes: `VaultOverlay` from `overlay.loader`, `load_overlay`, `build_registry`
- Produces: `ShadowRecord.overlay: dict | None = None`; `ShadowEvaluator.__init__` gains `overlay: VaultOverlay | None = None`; the `shadow` CLI gains `--config` and loads the overlay as of the tape start

- [ ] **Step 1: Write the failing test**

```python
# tests/test_shadow_overlay.py
"""Shadow measures; it must never let a stance narrow what it measures."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tradetk.overlay.loader import VaultOverlay
from tradetk.shadow.records import ShadowRecord
from tradetk.translation.claims import ClaimOperator
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


def test_shadow_record_carries_an_overlay_annotation() -> None:
    rec = ShadowRecord(
        observed_at=NOW, ticker="KXBTCD-T", series_ticker="KXBTCD", underlying="BTC",
        strategy="baseline_vol", method="lognormal", p=Decimal("0.4"),
        operator=ClaimOperator.above, threshold=Decimal("100000"),
        resolution_time=NOW, hours_to_resolution=4.0,
        overlay={"blocked": True, "bias": "bearish"},
    )
    assert rec.overlay["blocked"] is True


def test_overlay_field_defaults_to_none() -> None:
    """Records written before the overlay existed stay valid."""
    rec = ShadowRecord(
        observed_at=NOW, ticker="KXBTCD-T", series_ticker="KXBTCD", underlying="BTC",
        strategy="baseline_vol", method="lognormal", p=Decimal("0.4"),
        operator=ClaimOperator.above, threshold=Decimal("100000"),
        resolution_time=NOW, hours_to_resolution=4.0,
    )
    assert rec.overlay is None


def test_a_blocked_underlying_still_produces_a_policy_for_recording() -> None:
    """The anti-filter pin.

    Shadow exists to score the whole universe, including what it declines. If a
    stance could suppress records, the calibration set would quietly become a
    record of what the stances already believed, and the evaluator's entire
    reason for existing would be defeated.
    """
    overlay = VaultOverlay(base_gate=BASE_GATE, base_sizing=BASE_SIZING)
    policy = overlay.for_underlying("BTC", NOW)
    assert policy.as_dict()["underlying"] == "BTC"
    # An identity policy blocks nothing, and even a blocking one must still be
    # representable as an annotation rather than a filter.
    assert policy.blocked is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --system-certs python -m pytest tests/test_shadow_overlay.py -v`
Expected: FAIL — `ShadowRecord` has no `overlay` field (pydantic rejects the extra key)

- [ ] **Step 3: Add the field to `src/tradetk/shadow/records.py`**

Add to `ShadowRecord`, after `resolution_source`:

```python
    # What the vault overlay WOULD have done. Recorded, never applied:
    # filtering here would measure the model on the stances' own selection.
    overlay: dict | None = None
```

- [ ] **Step 4: Annotate in `src/tradetk/shadow/evaluator.py`**

Add the parameter to `__init__` (keyword-only, defaulting to `None` so existing
callers are unchanged):

```python
        overlay: Any | None = None,
```

and store it:

```python
        self.overlay = overlay
```

There is exactly one `ShadowRecord(...)` construction in `run()`. The loop's
timestamp variable is `now` (`now = observation.observed_at`) — *not*
`observed_at`, which exists only as a keyword on the record. Immediately before
the `records.append(ShadowRecord(...))` call, resolve the policy against `now`:

```python
            overlay_note = None
            if self.overlay is not None:
                policy = self.overlay.for_underlying(claim.underlying, now)
                overlay_note = policy.as_dict()
```

and add `overlay=overlay_note` to the `ShadowRecord(...)` call (it is
keyword-constructed, so append it alongside the other fields).

**Do not add any `continue` or conditional that skips a record based on the
policy.** Every market that is scored today must still be scored and recorded.

- [ ] **Step 4b: Wire the overlay into the `shadow` CLI**

The evaluator now accepts an overlay but the CLI never builds one, so records
would never actually carry the annotation. Wire it the same way as the backtest
CLI, pinned as of the tape start for the same lookahead reason.

In `src/tradetk/cli/shadow.py`, add the argument next to `--registry`:

```python
    ap.add_argument("--config", default="config/config.yaml",
                    help="Toolkit config; read only for the vault_overlay block.")
```

Then, immediately before `evaluator = ShadowEvaluator(` is constructed (after
`data` is loaded, so `start` is in scope from `start, end = replay.span`):

```python
    from tradetk.config.schema import VaultOverlayConfig
    from tradetk.overlay.loader import load_overlay
    from tradetk.overlay.verifiers import build_registry

    gate = GateLimits(
        min_net_edge_pp=Decimal(str(args.min_edge_pp)),
        margin_pp=Decimal(str(args.margin_pp)),
        min_book_depth_multiple=Decimal(str(args.min_depth_multiple)),
        max_book_participation_pct=Decimal(str(args.max_participation_pct)),
        max_hours_to_resolution=Decimal(str(args.max_hours)),
        reject_deep_tail=not args.allow_deep_tail,
    )
    sizing = SizingLimits(
        position_target=Decimal(args.position_target),
        per_position_ceiling=Decimal(args.per_position_ceiling),
        total_capital=Decimal(args.total_capital),
        max_book_participation_pct=Decimal(str(args.max_participation_pct)),
    )
    try:
        from tradetk.config.loader import load_config

        vault_cfg = load_config(args.config).vault_overlay
    except Exception as exc:  # noqa: BLE001 - a broken config must not stop shadow
        logging.getLogger("tradetk.cli.shadow").info(
            "vault_overlay config unavailable (%s); overlay off", exc
        )
        vault_cfg = VaultOverlayConfig()
    overlay = load_overlay(
        vault_cfg, base_gate=gate, base_sizing=sizing,
        registry=build_registry(), as_of=start, now=start,
    )
    if not overlay.ok:
        print(f"warning: vault overlay unavailable, annotations off: "
              f"{overlay.error}", file=sys.stderr)
```

The `gate`/`sizing` locals above are the same objects that were being built
inline inside the `ShadowEvaluator(...)` call. Now that they exist as locals,
replace the inline `gate_limits=GateLimits(...)` and `sizing_limits=SizingLimits(...)`
in that call with `gate_limits=gate,` and `sizing_limits=sizing,`, and add
`overlay=overlay,`. This keeps a single source of truth for the limits the
overlay narrows from.

Then surface the status in the emitted payload, before the final `print`:

```python
    payload["vault_overlay"] = overlay.as_dict()
```

- [ ] **Step 5: Run the full suite**

Run: `uv run --system-certs python -m pytest -q`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/tradetk/shadow/records.py src/tradetk/shadow/evaluator.py src/tradetk/cli/shadow.py tests/test_shadow_overlay.py
git commit -m "$(cat <<'EOF'
Shadow records the overlay verdict without filtering on it

Shadow exists to score the whole universe including what it declines. Filtering
there would make the calibration set a record of what the stances already
believed. Annotating instead makes a new question answerable: do stance-allowed
markets calibrate better than stance-blocked ones?

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FLkAHjVY1jJn1kMtpTVc3X
EOF
)"
```

---

### Task 7: Backtest applies the verdict

**Files:**
- Modify: `src/tradetk/backtest/engine.py`, `src/tradetk/cli/backtest.py`
- Test: `tests/test_backtest_overlay.py`

**Interfaces:**
- Consumes: `VaultOverlay`, `load_overlay`, `build_registry`
- Produces: `BacktestEngine.__init__` gains `overlay: VaultOverlay | None = None`; limits resolved per claim rather than from the instance attribute; the `backtest` CLI gains `--config` and loads the overlay as of the replay start

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backtest_overlay.py
"""Backtest acts on the overlay — and must read it as of the replay clock."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tradetk.overlay.loader import VaultOverlay
from tradetk.overlay.policy import build_policy
from tradetk.translation.claims import Claim, ClaimOperator
from tradetk.translation.edge import GateLimits
from tradetk.translation.sizing import SizingLimits
from tradetk.venues.base import Side

T1 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)

BASE_GATE = GateLimits(
    min_net_edge_pp=Decimal("3.0"), margin_pp=Decimal("1.0"),
    min_book_depth_multiple=Decimal("5.0"), max_book_participation_pct=Decimal("10.0"),
    max_hours_to_resolution=Decimal("168"),
)
BASE_SIZING = SizingLimits(
    position_target=Decimal("2.00"), per_position_ceiling=Decimal("3.00"),
    total_capital=Decimal("20.00"), max_book_participation_pct=Decimal("10.0"),
)


class _FakeStance:
    def __init__(self, bias="bearish", risk=50) -> None:
        self.bias = bias
        self.effective_risk = risk
        self.underlying = "BTC"
        self.stance = type("S", (), {
            "id": "s1", "max_position_dollars": None, "created": T2,
        })()


def _claim() -> Claim:
    return Claim(
        ticker="KXBTCD-T", series_ticker="KXBTCD", underlying="BTC",
        operator=ClaimOperator.above, resolution_time=T2,
        resolution_source="CF Benchmarks BRTI", rules_primary="rules",
        threshold=Decimal("100000"),
    )


def test_overlay_restricts_the_tradeable_side() -> None:
    overlay = VaultOverlay(
        base_gate=BASE_GATE, base_sizing=BASE_SIZING,
        stances={"BTC": _FakeStance("bearish")}, enabled=True,
    )
    policy = overlay.for_underlying("BTC", T2)
    assert policy.allowed_sides(_claim()) == (Side.no,)


def test_overlay_shrinks_the_position_target() -> None:
    overlay = VaultOverlay(
        base_gate=BASE_GATE, base_sizing=BASE_SIZING,
        stances={"BTC": _FakeStance(risk=25)}, enabled=True,
    )
    policy = overlay.for_underlying("BTC", T2)
    assert policy.sizing_limits.position_target == Decimal("0.50")


def test_an_underlying_with_no_stance_is_untouched() -> None:
    overlay = VaultOverlay(
        base_gate=BASE_GATE, base_sizing=BASE_SIZING,
        stances={"BTC": _FakeStance()}, enabled=True,
    )
    policy = overlay.for_underlying("ETH", T2)
    assert policy.sizing_limits == BASE_SIZING
    assert set(policy.allowed_sides(_claim())) == {Side.yes, Side.no}


def test_policy_built_without_a_stance_matches_the_globals() -> None:
    """The as-of guarantee, expressed at the policy layer.

    When a replay asks for state before a stance existed, vault-post hands back
    no stance at all — and the policy that results must be indistinguishable
    from running with no vault. Anything else would leak a future view into a
    replay of the past.
    """
    policy = build_policy(
        "BTC", stance=None, catalysts=[],
        base_gate=BASE_GATE, base_sizing=BASE_SIZING, now=T1,
    )
    assert policy.gate_limits == BASE_GATE
    assert policy.sizing_limits == BASE_SIZING
    assert policy.blocked is False


# ── the verdict must actually reach the engine ─────────────────────
#
# The tests above pin the policy math; these prove the backtest engine
# consults it. They reuse the engine + tape fixtures from test_backtest —
# pytest's default prepend import mode puts sibling test modules on the
# path, so the bare `import test_backtest` resolves (there is no
# tests/__init__.py). The overlay is built with the engine's OWN limits so
# the identity case is byte-for-byte, matching how the loader is wired live.

from test_backtest import engine as build_engine, replay  # noqa: E402


def test_blocking_overlay_produces_no_trades() -> None:
    eng = build_engine()
    overlay = VaultOverlay(
        base_gate=eng.gate_limits, base_sizing=eng.sizing_limits,
        stances={"BTC": _FakeStance("neutral", 0)}, enabled=True,
    )
    result = build_engine(overlay=overlay).run(replay())
    assert result.trades == []
    assert result.skipped.get("overlay_blocked", 0) >= 1


def test_bearish_overlay_forbids_the_yes_side_in_the_engine() -> None:
    """KXBTCD is a 'greater' (above) market, so bullish is YES. A bearish
    stance must forbid YES — visible as a skip whether or not NO trades, and
    no trade the engine keeps may be on the YES side."""
    eng = build_engine()
    overlay = VaultOverlay(
        base_gate=eng.gate_limits, base_sizing=eng.sizing_limits,
        stances={"BTC": _FakeStance("bearish", 50)}, enabled=True,
    )
    result = build_engine(overlay=overlay).run(replay())
    assert result.skipped.get("overlay_side_forbidden", 0) >= 1
    assert all(t.side is Side.no for t in result.trades)


def test_empty_enabled_overlay_matches_the_no_overlay_run() -> None:
    """An enabled overlay with no mail is byte-identical to no overlay."""
    baseline = build_engine().run(replay())
    eng = build_engine()
    overlay = VaultOverlay(
        base_gate=eng.gate_limits, base_sizing=eng.sizing_limits, enabled=True,
    )
    with_overlay = build_engine(overlay=overlay).run(replay())
    assert len(with_overlay.trades) == len(baseline.trades)
    assert with_overlay.skipped == baseline.skipped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --system-certs python -m pytest tests/test_backtest_overlay.py -v`
Expected: FAIL with `ModuleNotFoundError` or attribute errors on the overlay wiring

- [ ] **Step 3: Wire the overlay into `src/tradetk/backtest/engine.py`**

The single assessment site is `_best_assessment`, a per-side loop that sizes and
gates each side at its own price (there is no `assess_claim` call and no
`assessment.chosen` — the side is chosen by taking the better *passing* side).
The overlay therefore has to be resolved **once per claim** at the top of that
method, and applied *inside* the existing loop.

First, add the constructor parameter (keyword-only, defaulting to `None` so
every existing caller is unchanged) and store it. In `__init__`, after
`self.sizing_limits = sizing_limits`:

```python
        self.overlay = overlay
```

and in the `__init__` signature, after `sizing_limits: SizingLimits,`:

```python
        overlay: Any | None = None,
```

Then replace the whole `_best_assessment` method with this — the only changes
from the current body are the three lines that resolve the policy before the
loop, the two lines that use `gate_limits`/`sizing_limits` instead of the
instance attributes, and the one `continue` that skips a forbidden side:

```python
    def _best_assessment(
        self, claim: Claim, opinion_estimate, book: BinaryBook, when: datetime,
        capital_in_use: Decimal,
    ) -> tuple[EdgeAssessment | None, str]:
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
        gate_limits = self.gate_limits
        sizing_limits = self.sizing_limits
        allowed: tuple[Side, ...] | None = None
        if self.overlay is not None:
            policy = self.overlay.for_underlying(claim.underlying, when)
            if policy.blocked:
                self._skipped["overlay_blocked"] += 1
                return None, "none"
            gate_limits = policy.gate_limits
            sizing_limits = policy.sizing_limits
            allowed = policy.allowed_sides(claim)

        best: EdgeAssessment | None = None
        best_cap = "none"
        for side in (Side.yes, Side.no):
            if allowed is not None and side not in allowed:
                self._skipped["overlay_side_forbidden"] += 1
                continue
            price = book.best_yes_ask if side is Side.yes else book.best_no_ask
            if price is None:
                continue
            depth = side_depth(book, side)
            plan = plan_size(
                price, self.fee_model, sizing_limits,
                book_depth=depth, capital_in_use=capital_in_use,
            )
            if not plan.tradeable:
                self._skipped[f"unsizeable_{plan.binding_cap.value}"] += 1
                continue
            assessment = assess_side(
                claim, opinion_estimate, book, side=side, contracts=plan.contracts,
                fee_model=self.fee_model, limits=gate_limits, now=when,
            )
            if not assessment.passed:
                for failure in assessment.failures:
                    self._skipped[f"gate_{failure.gate.value}"] += 1
                continue
            if best is None or assessment.net_edge_pp > best.net_edge_pp:
                best, best_cap = assessment, plan.binding_cap.value
        return best, best_cap
```

Every name the method uses — `Any`, `Claim`, `Side`, `BinaryBook`, `Decimal`,
`EdgeAssessment`, `side_depth`, `plan_size`, `assess_side` — is already imported
in this module (the current body uses all of them). No new imports are needed.

- [ ] **Step 3b: Wire the overlay into the `backtest` CLI**

Accepting an `overlay` on the engine does nothing until the CLI builds one. This
is also the *only* call site of `load_overlay` (Task 4) and `build_registry`
(Task 5) — without it, both are dead code. The stance set is pinned **as of the
replay window start** so no stance created inside the window can leak into an
earlier replay timestamp (catalyst windows are still evaluated per-observation
inside `build_policy`, which is correct — a catalyst only bites while its window
is open).

In `src/tradetk/cli/backtest.py`, add the argument next to `--registry`:

```python
    ap.add_argument("--config", default="config/config.yaml",
                    help="Toolkit config; read only for the vault_overlay block.")
```

Then, immediately after `gate` is built (right before `strategy = get_strategy(...)`),
load the overlay and report a failure loudly:

```python
    from tradetk.config.schema import VaultOverlayConfig
    from tradetk.overlay.loader import load_overlay
    from tradetk.overlay.verifiers import build_registry

    try:
        from tradetk.config.loader import load_config

        vault_cfg = load_config(args.config).vault_overlay
    except Exception as exc:  # noqa: BLE001 - a broken config must not stop a backtest
        logging.getLogger("tradetk.cli.backtest").info(
            "vault_overlay config unavailable (%s); overlay off", exc
        )
        vault_cfg = VaultOverlayConfig()

    overlay = load_overlay(
        vault_cfg, base_gate=gate, base_sizing=sizing,
        registry=build_registry(), as_of=start, now=start,
    )
    if not overlay.ok:
        print(f"warning: vault overlay unavailable, backtest unmodified: "
              f"{overlay.error}", file=sys.stderr)
```

Pass it to the engine — add `overlay=overlay,` to the `BacktestEngine(...)` call.

Finally, so the degradation is never silent in machine output, surface the
overlay status in the JSON branch. Replace the `--json` dump with:

```python
    if args.json:
        payload = result.as_dict()
        payload["vault_overlay"] = overlay.as_dict()
        print(json.dumps(payload, indent=2 if args.pretty else None, default=str))
```

`start` is already in scope (`start, end = replay.span`), and `sys`/`json`/`logging`
are already imported.

- [ ] **Step 4: Run the full suite**

Run: `uv run --system-certs python -m pytest -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/tradetk/backtest/engine.py src/tradetk/cli/backtest.py tests/test_backtest_overlay.py
git commit -m "$(cat <<'EOF'
Backtest resolves limits per claim and applies the overlay

Blocked underlyings do not trade, bias restricts the side, and risk scales the
target. Without an overlay the resolver returns the global limits, so existing
backtests are unchanged.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FLkAHjVY1jJn1kMtpTVc3X
EOF
)"
```

---

### Task 8: Snapshot capture in `record`, and docs

**Files:**
- Modify: `src/tradetk/cli/record.py`, `README.md`, `CLAUDE.md`, `memory/STATE.md`
- Test: `tests/test_record_vault_snapshot.py`

**Interfaces:**
- Consumes: `load_overlay` / `vaultpost.VaultPost`
- Produces: `capture_vault_snapshot(cfg, now) -> str | None` in `tradetk.cli.record`; returns the snapshot path, or `None` when disabled or failing

- [ ] **Step 1: Write the failing test**

```python
# tests/test_record_vault_snapshot.py
"""History has to exist before a backtest can ask for it."""

from __future__ import annotations

from datetime import datetime, timezone

from tradetk.cli.record import capture_vault_snapshot
from tradetk.config.schema import VaultOverlayConfig

NOW = datetime(2026, 8, 4, 22, 0, tzinfo=timezone.utc)


def test_disabled_overlay_captures_nothing() -> None:
    assert capture_vault_snapshot(VaultOverlayConfig(enabled=False), NOW) is None


def test_broken_bridge_does_not_stop_the_recorder() -> None:
    """A dead vault must never cost us the market tape."""
    cfg = VaultOverlayConfig(enabled=True, config_path="nope/missing.yaml")
    assert capture_vault_snapshot(cfg, NOW) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --system-certs python -m pytest tests/test_record_vault_snapshot.py -v`
Expected: FAIL with `ImportError: cannot import name 'capture_vault_snapshot'`

- [ ] **Step 3: Add the helper to `src/tradetk/cli/record.py`**

```python
def capture_vault_snapshot(vault_cfg: Any, now: datetime) -> str | None:
    """Record what the mailbox said, so backtests can ask 'as of when'.

    A backtest that read live stances while replaying the past would price it
    with views written afterwards. Snapshots are what make the as-of query
    honest, and they only exist if something captures them on a schedule.

    A failure here is logged and swallowed: the market tape is the thing that
    cannot be reconstructed later, and a dead vault must never cost us that.
    """
    if not getattr(vault_cfg, "enabled", False):
        return None
    try:
        from vaultpost import VaultPost, VaultPostConfig, VerifierRegistry

        cfg = VaultPostConfig.from_yaml(vault_cfg.config_path)
        return str(VaultPost(cfg, VerifierRegistry()).capture_snapshot(now))
    except Exception as exc:  # noqa: BLE001 - never lose the market tape
        log.warning("vault snapshot skipped: %s", exc)
        return None
```

- [ ] **Step 4: Load the overlay config, then call the helper once per poll**

`record.py` today is pure argparse — it never loads the toolkit `Config`. So the
overlay config has to be loaded explicitly, and its absence tolerated (the
recorder must run on a machine that never configured the vault). Add a small
tolerant resolver near the top of the module, next to `log`:

```python
def _vault_overlay_cfg(path: str) -> Any:
    """The vault_overlay block, or a disabled default if config is absent.

    The recorder's job is the market tape; a missing or invalid config must
    never stop it, so any failure degrades to the off-by-default block.
    """
    from tradetk.config.schema import VaultOverlayConfig

    try:
        from tradetk.config.loader import load_config

        return load_config(path).vault_overlay
    except Exception as exc:  # noqa: BLE001 - config trouble must not stop the tape
        log.info("vault_overlay config unavailable (%s); overlay off", exc)
        return VaultOverlayConfig()
```

Add a `--config` argument in `main()`, next to `--tape-dir`:

```python
    ap.add_argument("--config", default="config/config.yaml",
                    help="Toolkit config; read only for the vault_overlay block.")
```

Resolve it once, just after `args = ap.parse_args(argv)`:

```python
    vault_cfg = _vault_overlay_cfg(args.config)
```

Then, immediately after each `report = poll_all(writer, sources)` — there are two
call sites, the `--once` branch (~line 198) and the daemon loop (~line 218) — add:

```python
            report["vault_snapshot"] = capture_vault_snapshot(
                vault_cfg, datetime.now(timezone.utc)
            )
```

(`datetime` and `timezone` are already imported; match the indentation of each
site — the `--once` branch is one level shallower than the daemon loop.)

- [ ] **Step 5: Run the full suite**

Run: `uv run --system-certs python -m pytest -q`
Expected: all PASS

- [ ] **Step 6: Update the docs**

In `README.md`, add a short section under the pipeline description:

> **Vault overlay (optional).** Approved stances and catalysts from the Second
> Brain, via `vault-post`, can narrow what the toolkit proposes: `bias` restricts
> the side, `risk` scales the position target, and a catalyst raises the required
> edge. Every dial narrows — the vault can never permit a trade the pipeline
> would otherwise refuse. `shadow` records the overlay's verdict but never
> filters on it, so calibration still measures the whole universe. Off by
> default; enable in `config.yaml` under `vault_overlay`.

In `CLAUDE.md`, under "Trading logic", add:

> - Vault stances may restrict the side, shrink the size, or demand more edge.
>   They may never change a probability, and they may never permit a trade the
>   pipeline would otherwise refuse.

In `memory/STATE.md`, note that the vault overlay landed on 2026-08-04, is off by
default, and that `record` now captures a vault snapshot per poll when enabled.

- [ ] **Step 7: Commit**

```bash
git add src/tradetk/cli/record.py tests/test_record_vault_snapshot.py README.md CLAUDE.md memory/STATE.md
git commit -m "$(cat <<'EOF'
Capture a vault snapshot each poll, and document the overlay

Backtests can only ask "what did my stances say then" if something wrote it
down on a schedule. A failure to snapshot is logged and swallowed, because the
market tape is the thing that cannot be reconstructed later.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FLkAHjVY1jJn1kMtpTVc3X
EOF
)"
```

---

## Verification

After Task 8 all of the following must hold:

- `uv run --system-certs python -m pytest -q` is green (382 existing + ~38 new)
- `uv run --system-certs ruff check src tests scripts` passes
- With `vault_overlay.enabled: false`, every existing output is byte-identical to before this branch
- A missing vault config trades normally *and* reports `ok: false` — confirm end to end:
  `uv run --system-certs python -m tradetk.cli.backtest --json --config nope.yaml`
  runs, and its JSON carries `"vault_overlay": {"ok": false, ...}` (a `--config`
  that *exists* but has `enabled: false` carries `"ok": true, "enabled": false`)
- `git grep -n "continue" src/tradetk/shadow/evaluator.py` shows no skip introduced by overlay logic
- `git grep -n "load_overlay\|build_registry" src/tradetk/cli` shows both CLIs wire the overlay (neither Task 4 nor Task 5 is dead code)
- No file under `src/tradetk/translation/probability.py` was modified
- No venue order path was touched
