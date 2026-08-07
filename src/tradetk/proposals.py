"""The proposal artifact: what `propose` writes and `execute` will consume.

A proposal is FACTS, not judgment: the claim, the decision with its full cost
breakdown, the book as it stood, and the config fingerprint that shaped it.
Whether it is still valid later is `execute`'s re-validation policy (step 17),
deliberately not encoded here.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

#: The config sections that shape a trading decision. venue is included so a
#: demo-minted proposal can never be executed against prod unnoticed.
_FINGERPRINT_SECTIONS = (
    "capital", "edge_gate", "liquidity", "horizon",
    "risk", "orders", "venue", "fees", "strategy",
)


def config_fingerprint(config: Any) -> str:
    """sha256 over the canonical JSON of the decision-shaping config sections."""
    payload = {
        name: getattr(config, name).model_dump(mode="json")
        for name in _FINGERPRINT_SECTIONS
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _book_view(book: Any) -> dict[str, Any]:
    """Top-of-book facts execute will re-validate against."""
    from tradetk.venues.base import Side

    return {
        "best_yes_bid": str(book.best_yes_bid) if book.best_yes_bid is not None else None,
        "best_yes_ask": str(book.best_yes_ask) if book.best_yes_ask is not None else None,
        "best_no_bid": str(book.best_no_bid) if book.best_no_bid is not None else None,
        "best_no_ask": str(book.best_no_ask) if book.best_no_ask is not None else None,
        "yes_depth": str(book.depth(Side.yes)),
        "no_depth": str(book.depth(Side.no)),
        "retrieved_at": book.retrieved_at.isoformat() if book.retrieved_at else None,
    }


def build_proposal(
    *,
    claim: Any,
    assessment: Any,
    book: Any,
    book_state: Any,
    halt: Any,
    overlay_verdict: dict[str, Any],
    candle_age_seconds: Decimal,
    strategy_name: str,
    vol_lookback_days: int,
    created_at: datetime,
    config_fingerprint: str,
    estimate: Any,
) -> dict[str, Any]:
    """Assemble the full decision trace for one admitted trade."""
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": created_at.isoformat(),
        "strategy": {"name": strategy_name, "vol_lookback_days": vol_lookback_days},
        "claim": claim.model_dump(mode="json"),
        "decision": assessment.as_dict(),
        # The probability estimate that fed the decision, inputs included (vol,
        # hours to resolution, spot, z-score) -- a proposal is reviewable only
        # if the number behind it is reconstructable after the fact.
        "estimate": estimate.as_dict(),
        "book": _book_view(book),
        "signals": {"candle_age_seconds": str(candle_age_seconds)},
        "risk": {
            "slots_used": book_state.risk_state().slots_used,
            "capital_deployed": str(book_state.capital_deployed),
            "realized_today": str(book_state.realized_today),
            "drawdown": str(book_state.drawdown),
            "halt": {"admitted": halt.admitted, "reason": halt.reason},
        },
        "overlay": overlay_verdict,
        "config_fingerprint": config_fingerprint,
    }


def write_proposal(
    proposals_dir: str | Path, proposal: dict[str, Any], *,
    created_at: datetime, ticker: str,
) -> Path:
    """Write one proposal file; refuse to replace one a human may be reading."""
    stamp = created_at.strftime("%Y%m%dT%H%M%SZ")
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in ticker)
    path = Path(proposals_dir) / f"{stamp}-{safe}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"proposal already exists, refusing to overwrite: {path}")
    path.write_text(json.dumps(proposal, indent=2, default=str), encoding="utf-8")
    return path
