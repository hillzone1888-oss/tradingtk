"""Stage 1 of the translation layer: contract -> typed :class:`Claim`.

A prediction market is an English sentence with a price attached. Everything
downstream — probability, edge, sizing — needs a *structured* proposition
instead, and it must be the same structure regardless of venue.

Two rules govern this module:

**Structured fields beat prose.** Kalshi publishes ``strike_type``,
``floor_strike`` and ``cap_strike``; those are used, and the title is never
regexed for a number. Markets whose ``strike_type`` is ``custom`` therefore get
rejected even when a human can plainly read the threshold out of the rules text
— 304 of 2,428 markets observed. That is deliberate. A regex that is right 95%
of the time silently mis-prices the other 5%, and this is real money.

**Fail closed.** An unmapped series, an absent strike, a missing resolution time
or an unidentifiable settlement source is a rejection with a reason, never a
guess and never a default. Rejections are counted and reported, because at this
size the rejection log is more informative than the trade log.

A note on relative markets: the "up in next 15 mins" series compare a future
measurement against one taken at window open. Their ``floor_strike`` is that
measured reference (verified stable across snapshots, so it is fixed once
published), which makes them ordinary threshold claims for pricing purposes —
but they are flagged, since a threshold set by measurement is ~50/50 by
construction and must not be pooled with round-number strikes when calibrating.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from tradetk.venues.base import VenueMarket

log = logging.getLogger("tradetk.translation.claims")


class ClaimOperator(str, Enum):
    """How the underlying's value is compared to the claim's bound(s)."""

    above = "above"  # value > threshold
    at_or_above = "at_or_above"  # value >= threshold
    below = "below"  # value < threshold
    between = "between"  # lower <= value <= upper


# Kalshi strike_type -> operator. `custom` is absent on purpose: it means the
# venue itself declined to publish a structured strike.
_STRIKE_TYPE_TO_OPERATOR = {
    "greater": ClaimOperator.above,
    "greater_or_equal": ClaimOperator.at_or_above,
    "less": ClaimOperator.below,
    "between": ClaimOperator.between,
}


class RejectReason(str, Enum):
    """Why a market is not eligible. Grouped and counted in every report."""

    unmapped_series = "unmapped_series"
    unsupported_strike_type = "unsupported_strike_type"
    missing_strike_values = "missing_strike_values"
    inconsistent_bounds = "inconsistent_bounds"
    missing_resolution_time = "missing_resolution_time"
    missing_rules = "missing_rules"
    unidentified_resolution_source = "unidentified_resolution_source"


class ClaimParseError(Exception):
    """A market could not be turned into a claim. Carries a machine-readable reason."""

    def __init__(self, reason: RejectReason, detail: str, ticker: str | None = None) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail
        self.ticker = ticker


class Claim(BaseModel):
    """A structured, testable proposition that resolves YES or NO.

    Venue-agnostic by construction: nothing here names Kalshi, and a Polymarket
    US contract producing the same claim must price identically.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    series_ticker: str
    underlying: str
    operator: ClaimOperator
    resolution_time: datetime
    resolution_source: str
    rules_primary: str
    threshold: Decimal | None = None
    lower_bound: Decimal | None = None
    upper_bound: Decimal | None = None
    reference_is_measured: bool = False

    @model_validator(mode="after")
    def _bounds_match_operator(self) -> "Claim":
        if self.operator is ClaimOperator.between:
            if self.lower_bound is None or self.upper_bound is None:
                raise ValueError("between claim requires both bounds")
            if self.lower_bound >= self.upper_bound:
                raise ValueError(
                    f"between claim needs lower < upper (got {self.lower_bound}, "
                    f"{self.upper_bound})"
                )
        elif self.threshold is None:
            raise ValueError(f"{self.operator.value} claim requires a threshold")
        return self

    def resolves_yes(self, value: Decimal | float) -> bool:
        """Evaluate the claim against a settled value.

        The single source of truth for claim semantics — the backtest, the
        shadow evaluator, and settlement reconciliation all call this rather
        than re-implementing the comparison.
        """
        v = Decimal(str(value))
        if self.operator is ClaimOperator.above:
            return v > self.threshold  # type: ignore[operator]
        if self.operator is ClaimOperator.at_or_above:
            return v >= self.threshold  # type: ignore[operator]
        if self.operator is ClaimOperator.below:
            return v < self.threshold  # type: ignore[operator]
        return self.lower_bound <= v <= self.upper_bound  # type: ignore[operator]

    def hours_to_resolution(self, now: datetime) -> float:
        return (self.resolution_time - now).total_seconds() / 3600.0

    def describe(self) -> str:
        """Plain English for the proposal trace."""
        when = self.resolution_time.strftime("%Y-%m-%d %H:%M UTC")
        if self.operator is ClaimOperator.between:
            body = f"{self.underlying} between {self.lower_bound} and {self.upper_bound}"
        else:
            word = {
                ClaimOperator.above: "above",
                ClaimOperator.at_or_above: "at or above",
                ClaimOperator.below: "below",
            }[self.operator]
            body = f"{self.underlying} {word} {self.threshold}"
        tail = " (threshold set by measured reference)" if self.reference_is_measured else ""
        return f"{body} at {when}, per {self.resolution_source}{tail}"


class UnderlyingRegistry:
    """Series ticker -> underlying symbol, loaded from config.

    Fail-closed by design: `lookup` raises for an unknown series rather than
    inferring one from the ticker's shape. KXSOLE and KXSOLD are both SOL while
    KXSHIBA is SHIB — the shapes do not support a rule.
    """

    def __init__(self, mapping: dict[str, str]) -> None:
        self._map = {k.upper(): v.upper() for k, v in mapping.items()}

    @classmethod
    def from_yaml(cls, path: str | Path) -> "UnderlyingRegistry":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        series = data.get("series") or {}
        if not isinstance(series, dict) or not series:
            raise ValueError(f"{path} has no `series` mapping")
        return cls(series)

    def lookup(self, series_ticker: str) -> str:
        try:
            return self._map[series_ticker.upper()]
        except KeyError:
            raise ClaimParseError(
                RejectReason.unmapped_series,
                f"series {series_ticker!r} is not in the underlying registry",
            ) from None

    def __contains__(self, series_ticker: str) -> bool:
        return series_ticker.upper() in self._map

    @property
    def symbols(self) -> set[str]:
        return set(self._map.values())


# Settlement index inside the rules text, e.g. "CF Benchmarks' BRTI" or
# "CF Benchmarks' Shiba Inu-Dollar Spot Rate (SHIBUSD_RTI)".
_SOURCE_PATTERNS = (
    re.compile(r"\(([A-Z][A-Z0-9_]*RTI)\)"),
    re.compile(r"\b([A-Z][A-Z0-9_]*RTI)\b"),
)
_PROVIDER = re.compile(r"(CF Benchmarks)", re.I)


def extract_resolution_source(rules: str) -> str | None:
    """Identify the settlement index a market resolves against.

    Resolution risk is real: ambiguous wording and disputed settlement happen,
    and capital is locked until resolution. A market whose source cannot be
    identified is refused rather than assumed.
    """
    if not rules:
        return None
    index = None
    for pattern in _SOURCE_PATTERNS:
        match = pattern.search(rules)
        if match:
            index = match.group(1)
            break
    provider = _PROVIDER.search(rules)
    if index and provider:
        return f"{provider.group(1)} {index}"
    if index:
        return index
    return None


def _is_relative(rules: str) -> bool:
    """Whether the claim compares against a measured reference rather than a
    fixed strike ("price up in next 15 mins")."""
    return "at least the simple average" in rules.lower()


def parse_claim(market: VenueMarket, registry: UnderlyingRegistry) -> Claim:
    """Turn one market into a :class:`Claim`, or raise :class:`ClaimParseError`."""
    series = market.series_ticker or _series_from_ticker(market.ticker)
    underlying = registry.lookup(series)

    operator = _STRIKE_TYPE_TO_OPERATOR.get(market.strike_type or "")
    if operator is None:
        raise ClaimParseError(
            RejectReason.unsupported_strike_type,
            f"strike_type {market.strike_type!r} has no structured strike "
            "(the title is deliberately not parsed)",
            market.ticker,
        )

    rules = market.rules_primary or ""
    if not rules.strip():
        raise ClaimParseError(
            RejectReason.missing_rules, "no resolution criteria published", market.ticker
        )

    source = extract_resolution_source(rules)
    if source is None:
        raise ClaimParseError(
            RejectReason.unidentified_resolution_source,
            "could not identify the settlement source",
            market.ticker,
        )

    resolution_time = market.close_time or market.expiration_time
    if resolution_time is None:
        raise ClaimParseError(
            RejectReason.missing_resolution_time, "no close or expiration time", market.ticker
        )

    threshold = lower = upper = None
    if operator is ClaimOperator.between:
        lower, upper = market.floor_strike, market.cap_strike
        if lower is None or upper is None:
            raise ClaimParseError(
                RejectReason.missing_strike_values,
                "between claim missing floor or cap",
                market.ticker,
            )
        if lower >= upper:
            raise ClaimParseError(
                RejectReason.inconsistent_bounds, f"floor {lower} >= cap {upper}", market.ticker
            )
    elif operator is ClaimOperator.below:
        threshold = market.cap_strike
        if threshold is None:
            raise ClaimParseError(
                RejectReason.missing_strike_values, "below claim missing cap", market.ticker
            )
    else:
        threshold = market.floor_strike
        if threshold is None:
            raise ClaimParseError(
                RejectReason.missing_strike_values, "above claim missing floor", market.ticker
            )

    return Claim(
        ticker=market.ticker,
        series_ticker=series,
        underlying=underlying,
        operator=operator,
        threshold=threshold,
        lower_bound=lower,
        upper_bound=upper,
        resolution_time=resolution_time,
        resolution_source=source,
        rules_primary=rules,
        reference_is_measured=_is_relative(rules),
    )


def _series_from_ticker(ticker: str) -> str:
    """Kalshi tickers are ``SERIES-EVENT-STRIKE``; the series is the first part.

    Only used when the venue omitted `series_ticker`; the result still has to
    survive the registry lookup, so a wrong guess is rejected rather than traded.
    """
    return ticker.split("-", 1)[0]


class ParseReport(BaseModel):
    """Claims plus a full account of what was filtered out and why."""

    model_config = ConfigDict(frozen=True)

    claims: list[Claim]
    rejections: dict[str, int]
    rejected_examples: dict[str, str]

    @property
    def eligible_count(self) -> int:
        return len(self.claims)

    @property
    def rejected_count(self) -> int:
        return sum(self.rejections.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible_count,
            "rejected": self.rejected_count,
            "rejections_by_reason": dict(sorted(self.rejections.items())),
            "rejected_examples": self.rejected_examples,
        }


def parse_claims(markets: Iterable[VenueMarket], registry: UnderlyingRegistry) -> ParseReport:
    """Parse many markets, collecting rejections by reason rather than raising."""
    claims: list[Claim] = []
    counts: Counter[str] = Counter()
    examples: dict[str, str] = {}

    for market in markets:
        try:
            claims.append(parse_claim(market, registry))
        except ClaimParseError as exc:
            counts[exc.reason.value] += 1
            examples.setdefault(exc.reason.value, f"{market.ticker}: {exc.detail}")

    if counts:
        log.info("claim parsing: %d eligible, rejected %s", len(claims), dict(counts))
    return ParseReport(claims=claims, rejections=dict(counts), rejected_examples=examples)
