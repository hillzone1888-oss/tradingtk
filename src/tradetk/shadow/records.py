"""What a shadow observation is, and how it is stored.

The shadow log exists to solve a counting problem. A $20 book holds six
positions; the eligible universe is ~2,100 contracts. Trading your way to a
readable sample size would take months, and the sample you got would be biased
towards exactly the markets that passed the gates — which is the population you
least need evidence about, because you already traded those.

A forecast does not need capital to be scored. So every eligible market gets an
estimate recorded, **including the ones every gate rejected**, and calibration
is computed over all of them. That turns six positions a day into hundreds of
testable predictions, and it measures the model on the whole universe rather
than on its own selection.

**Records are self-contained.** Each row carries the claim's operator, bounds
and resolution time, so calibration can settle it without re-reading the tape or
re-parsing the market. A scoring pipeline that depends on three other files
being in the right state is a scoring pipeline that eventually gets run against
the wrong state.

**The market's own price is recorded next to ours.** This is the single most
important column in the file. Calibration's real question is not "is our model
any good" but "is our model better than the price we would have to pay", and
that comparison is impossible after the fact if the price was not captured at
the moment the estimate was made.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from tradetk.translation.claims import Claim, ClaimOperator

log = logging.getLogger("tradetk.shadow.records")

SHADOW_COLUMNS = [
    "observed_at", "ticker", "series_ticker", "underlying", "strategy", "method",
    "p", "market_mid", "best_yes_bid", "best_yes_ask", "spread",
    "operator", "threshold", "lower_bound", "upper_bound",
    "resolution_time", "hours_to_resolution", "reference_is_measured",
    "resolution_source", "rules_primary",
    "z_score", "deep_tail", "spot", "sigma_annual",
    "gate_decision", "chosen_side", "net_edge_pp", "failures",
]


class ShadowRecord(BaseModel):
    """One scored market at one instant. Never mutated once written."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observed_at: datetime
    ticker: str
    series_ticker: str
    underlying: str
    strategy: str
    method: str

    p: Decimal = Field(ge=0, le=1)
    market_mid: Decimal | None = None
    best_yes_bid: Decimal | None = None
    best_yes_ask: Decimal | None = None
    spread: Decimal | None = None

    operator: ClaimOperator
    threshold: Decimal | None = None
    lower_bound: Decimal | None = None
    upper_bound: Decimal | None = None
    resolution_time: datetime
    hours_to_resolution: float
    reference_is_measured: bool = False
    resolution_source: str = ""
    rules_primary: str = ""

    z_score: float | None = None
    deep_tail: bool = False
    spot: float | None = None
    sigma_annual: float | None = None

    gate_decision: str = "reject"
    chosen_side: str | None = None
    net_edge_pp: Decimal | None = None
    failures: tuple[str, ...] = ()

    def to_claim(self) -> Claim:
        """Rebuild the claim so settlement uses `Claim.resolves_yes`.

        Re-deriving the comparison here instead would create a second definition
        of what a claim means, and the two would diverge the first time an
        operator's semantics were clarified.
        """
        return Claim(
            ticker=self.ticker,
            series_ticker=self.series_ticker,
            underlying=self.underlying,
            operator=self.operator,
            threshold=self.threshold,
            lower_bound=self.lower_bound,
            upper_bound=self.upper_bound,
            resolution_time=self.resolution_time,
            resolution_source=self.resolution_source or "unknown",
            rules_primary=self.rules_primary or "(not retained)",
            reference_is_measured=self.reference_is_measured,
        )

    def as_row(self) -> dict[str, Any]:
        return {
            "observed_at": self.observed_at,
            "ticker": self.ticker,
            "series_ticker": self.series_ticker,
            "underlying": self.underlying,
            "strategy": self.strategy,
            "method": self.method,
            "p": float(self.p),
            "market_mid": float(self.market_mid) if self.market_mid is not None else None,
            "best_yes_bid": (
                float(self.best_yes_bid) if self.best_yes_bid is not None else None
            ),
            "best_yes_ask": (
                float(self.best_yes_ask) if self.best_yes_ask is not None else None
            ),
            "spread": float(self.spread) if self.spread is not None else None,
            "operator": self.operator.value,
            "threshold": float(self.threshold) if self.threshold is not None else None,
            "lower_bound": float(self.lower_bound) if self.lower_bound is not None else None,
            "upper_bound": float(self.upper_bound) if self.upper_bound is not None else None,
            "resolution_time": self.resolution_time,
            "hours_to_resolution": self.hours_to_resolution,
            "reference_is_measured": self.reference_is_measured,
            "resolution_source": self.resolution_source,
            "rules_primary": self.rules_primary,
            "z_score": self.z_score,
            "deep_tail": self.deep_tail,
            "spot": self.spot,
            "sigma_annual": self.sigma_annual,
            "gate_decision": self.gate_decision,
            "chosen_side": self.chosen_side,
            "net_edge_pp": float(self.net_edge_pp) if self.net_edge_pp is not None else None,
            "failures": json.dumps(list(self.failures)),
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "ShadowRecord":
        def dec(value: Any) -> Decimal | None:
            return None if value is None or pd.isna(value) else Decimal(str(value))

        def when(value: Any) -> datetime:
            stamp = pd.Timestamp(value).to_pydatetime()
            return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)

        raw_failures = row.get("failures")
        failures = tuple(json.loads(raw_failures)) if isinstance(raw_failures, str) else ()

        return cls(
            observed_at=when(row["observed_at"]),
            ticker=str(row["ticker"]),
            series_ticker=str(row["series_ticker"]),
            underlying=str(row["underlying"]),
            strategy=str(row["strategy"]),
            method=str(row["method"]),
            p=dec(row["p"]) or Decimal(0),
            market_mid=dec(row.get("market_mid")),
            best_yes_bid=dec(row.get("best_yes_bid")),
            best_yes_ask=dec(row.get("best_yes_ask")),
            spread=dec(row.get("spread")),
            operator=ClaimOperator(row["operator"]),
            threshold=dec(row.get("threshold")),
            lower_bound=dec(row.get("lower_bound")),
            upper_bound=dec(row.get("upper_bound")),
            resolution_time=when(row["resolution_time"]),
            hours_to_resolution=float(row["hours_to_resolution"]),
            reference_is_measured=bool(row.get("reference_is_measured", False)),
            resolution_source=str(row.get("resolution_source") or ""),
            rules_primary=str(row.get("rules_primary") or ""),
            z_score=None if pd.isna(row.get("z_score")) else float(row["z_score"]),
            deep_tail=bool(row.get("deep_tail", False)),
            spot=None if pd.isna(row.get("spot")) else float(row["spot"]),
            sigma_annual=(
                None if pd.isna(row.get("sigma_annual")) else float(row["sigma_annual"])
            ),
            gate_decision=str(row.get("gate_decision") or "reject"),
            chosen_side=(
                None if not row.get("chosen_side") or pd.isna(row.get("chosen_side"))
                else str(row["chosen_side"])
            ),
            net_edge_pp=dec(row.get("net_edge_pp")),
            failures=failures,
        )


class ShadowStore:
    """Append-only parquet log of shadow observations.

    Partitioned by UTC day like the tape, and deduplicated on
    ``(ticker, observed_at, strategy)`` so re-running the evaluator over the same
    tape is idempotent. Without that, every re-run would inflate the sample and
    make calibration look more confident than the evidence supports — the
    failure mode being guarded against is a *better-looking* result.
    """

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    def partition_path(self, when: datetime) -> Path:
        day = when.astimezone(timezone.utc).strftime("%Y-%m-%d")
        return self._dir / f"date={day}.parquet"

    def append(self, records: Iterable[ShadowRecord]) -> dict[str, Any]:
        rows = [r.as_row() for r in records]
        if not rows:
            return {"written": 0, "duplicates": 0, "partitions": 0}

        frame = pd.DataFrame(rows, columns=SHADOW_COLUMNS)
        written = duplicates = 0
        partitions: set[str] = set()

        for day, chunk in frame.groupby(
            frame["observed_at"].dt.tz_convert("UTC").dt.strftime("%Y-%m-%d")
        ):
            path = self._dir / f"date={day}.parquet"
            partitions.add(str(path))
            before = 0
            if path.exists():
                existing = pd.read_parquet(path)
                before = len(existing)
                chunk = pd.concat([existing, chunk], ignore_index=True)
            merged = chunk.drop_duplicates(
                subset=["ticker", "observed_at", "strategy"], keep="first"
            ).reset_index(drop=True)
            merged.to_parquet(path, index=False)
            written += len(merged) - before
            duplicates += len(chunk) - len(merged)

        return {
            "written": written,
            "duplicates": len(rows) - written,
            "partitions": len(partitions),
        }

    def read(self) -> list[ShadowRecord]:
        """Every recorded observation, oldest first."""
        paths = sorted(self._dir.glob("date=*.parquet"))
        if not paths:
            return []
        frame = pd.concat([pd.read_parquet(p) for p in paths], ignore_index=True)
        frame = frame.sort_values("observed_at").reset_index(drop=True)
        out: list[ShadowRecord] = []
        for row in frame.to_dict("records"):
            try:
                out.append(ShadowRecord.from_row(row))
            except Exception as exc:  # noqa: BLE001 - one bad row must not void the log
                log.warning("skipping unreadable shadow row %s: %s", row.get("ticker"), exc)
        return out

    def summary(self) -> dict[str, Any]:
        records = self.read()
        if not records:
            return {"records": 0}
        return {
            "records": len(records),
            "distinct_contracts": len({r.ticker for r in records}),
            "strategies": sorted({r.strategy for r in records}),
            "first": min(r.observed_at for r in records).isoformat(),
            "last": max(r.observed_at for r in records).isoformat(),
        }
