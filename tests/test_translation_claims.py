"""Claim parsing, on real Kalshi markets captured from the tape.

Fixtures below are literal payloads recorded on 2026-07-22 — not invented
shapes. The rejection tests matter as much as the happy path: a market that
parses *almost* right is worse than one that refuses to parse.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from tradetk.translation.claims import (
    Claim,
    ClaimOperator,
    ClaimParseError,
    RejectReason,
    UnderlyingRegistry,
    extract_resolution_source,
    parse_claim,
    parse_claims,
)
from tradetk.venues.kalshi import parse_market

REGISTRY = UnderlyingRegistry(
    {"KXBTCD": "BTC", "KXBNB": "BNB", "KXBTC15M": "BTC", "KXSHIBA": "SHIB", "KXETHD": "ETH"}
)

# ── real recorded markets ──────────────────────────────────────────

ABOVE = {  # strike_type "greater" — 1109 of 2428 observed
    "ticker": "KXBTCD-26JUL2214-T75299.99", "series_ticker": "KXBTCD",
    "title": "Bitcoin price on Jul 22, 2026?", "status": "open",
    "close_time": "2026-07-22T18:00:00Z", "strike_type": "greater",
    "floor_strike": 75299.99,
    "rules_primary": ("If the simple average of the sixty seconds of CF Benchmarks' Bitcoin "
                      "Real-Time Index (BRTI) before 2 PM EDT is above 75299.99 at 2 PM EDT "
                      "on Jul 22, 2026, then the market resolves to Yes."),
}

BELOW = {  # strike_type "less" — uses cap_strike, not floor
    "ticker": "KXBNB-26JUL2214-T395", "series_ticker": "KXBNB",
    "title": "BNB price on Jul 22, 2026?", "status": "open",
    "close_time": "2026-07-22T18:00:00Z", "strike_type": "less", "cap_strike": 395,
    "rules_primary": ("If the simple average of the sixty seconds of CF Benchmarks' "
                      "BNBUSD_RTI before 2 PM EDT is below $395 at 2 PM EDT on Jul 22, 2026, "
                      "then the market resolves to Yes."),
}

BETWEEN = {  # strike_type "between" — 992 of 2428 observed
    "ticker": "KXBNB-26JUL2214-B757", "series_ticker": "KXBNB",
    "title": "BNB price on Jul 22, 2026?", "status": "open",
    "close_time": "2026-07-22T18:00:00Z", "strike_type": "between",
    "floor_strike": 755, "cap_strike": 759.99,
    "rules_primary": ("If the simple average of the sixty seconds of CF Benchmarks' "
                      "BNBUSD_RTI before 2 PM EDT is between $755-759.99 at 2 PM EDT on "
                      "Jul 22, 2026, then the market resolves to Yes."),
}

RELATIVE = {  # "up in next 15 mins": threshold is a measured reference
    "ticker": "KXBTC15M-26JUL221315-15", "series_ticker": "KXBTC15M",
    "title": "BTC price up in next 15 mins?", "status": "open",
    "close_time": "2026-07-22T17:15:00Z", "strike_type": "greater_or_equal",
    "floor_strike": 66207.69,
    "rules_primary": ("If the simple average of the sixty seconds of CF Benchmarks' BRTI "
                      "before 1:15 PM EDT on Jul 22, 2026 is at least the simple average of "
                      "the sixty seconds of CF Benchmarks' BRTI before 1:00 PM EDT, then the "
                      "market resolves to Yes."),
}

CUSTOM = {  # strike_type "custom" — 304 of 2428; threshold readable but unstructured
    "ticker": "KXSHIBA-26JUL2217-T0.000013499", "series_ticker": "KXSHIBA",
    "title": "Shiba Inu price range on Jul 22, 2026?", "status": "open",
    "close_time": "2026-07-22T21:00:00Z", "strike_type": "custom",
    "rules_primary": ("If the simple average of the 60 seconds of CF Benchmarks' Shiba "
                      "Inu-Dollar Spot Rate (SHIBUSD_RTI) before 5 PM EDT is above "
                      "0.000013499 at 5 PM EDT on Jul 22, 2026, then the market resolves "
                      "to Yes."),
}


def _claim(raw: dict) -> Claim:
    return parse_claim(parse_market(raw), REGISTRY)


# ── the four operators ─────────────────────────────────────────────


def test_above_claim_from_floor_strike() -> None:
    c = _claim(ABOVE)
    assert c.operator is ClaimOperator.above
    assert c.underlying == "BTC"
    assert c.threshold == Decimal("75299.99")
    assert c.resolution_source == "CF Benchmarks BRTI"
    assert c.reference_is_measured is False


def test_below_claim_reads_cap_not_floor() -> None:
    """A `less` market carries its strike in cap_strike; reading floor gives None."""
    c = _claim(BELOW)
    assert c.operator is ClaimOperator.below
    assert c.threshold == Decimal("395")


def test_between_claim_carries_both_bounds() -> None:
    c = _claim(BETWEEN)
    assert c.operator is ClaimOperator.between
    assert (c.lower_bound, c.upper_bound) == (Decimal("755"), Decimal("759.99"))
    assert c.threshold is None


def test_relative_market_is_flagged() -> None:
    """Threshold set by measurement is ~50/50 by construction and must not be
    pooled with round-number strikes when calibrating."""
    c = _claim(RELATIVE)
    assert c.operator is ClaimOperator.at_or_above
    assert c.threshold == Decimal("66207.69")
    assert c.reference_is_measured is True


# ── evaluation semantics ───────────────────────────────────────────


@pytest.mark.parametrize("value,expected", [(75300, True), (75299.99, False), (70000, False)])
def test_above_is_strict(value, expected) -> None:
    assert _claim(ABOVE).resolves_yes(value) is expected


@pytest.mark.parametrize("value,expected", [(66207.69, True), (66207.68, False), (70000, True)])
def test_at_or_above_includes_the_boundary(value, expected) -> None:
    assert _claim(RELATIVE).resolves_yes(value) is expected


@pytest.mark.parametrize("value,expected", [(394.99, True), (395, False), (500, False)])
def test_below_is_strict(value, expected) -> None:
    assert _claim(BELOW).resolves_yes(value) is expected


@pytest.mark.parametrize(
    "value,expected",
    [(755, True), (759.99, True), (757, True), (754.99, False), (760, False)],
)
def test_between_is_inclusive(value, expected) -> None:
    assert _claim(BETWEEN).resolves_yes(value) is expected


def test_float_input_does_not_corrupt_the_comparison() -> None:
    # Decimal(str(v)) rather than Decimal(v): 0.1+0.2 style error must not decide a trade.
    assert _claim(ABOVE).resolves_yes(75299.991) is True


# ── rejections: fail closed, always with a reason ──────────────────


def test_custom_strike_type_is_rejected_not_regexed() -> None:
    """The threshold is plainly readable in the rules text. We still refuse:
    a regex that is right 95% of the time mis-prices the other 5%."""
    with pytest.raises(ClaimParseError) as exc:
        _claim(CUSTOM)
    assert exc.value.reason is RejectReason.unsupported_strike_type


def test_unmapped_series_is_rejected() -> None:
    with pytest.raises(ClaimParseError) as exc:
        _claim({**ABOVE, "series_ticker": "KXPOLITICS"})
    assert exc.value.reason is RejectReason.unmapped_series


def test_missing_strike_value_is_rejected() -> None:
    with pytest.raises(ClaimParseError) as exc:
        _claim({**ABOVE, "floor_strike": None})
    assert exc.value.reason is RejectReason.missing_strike_values


def test_between_with_inverted_bounds_is_rejected() -> None:
    with pytest.raises(ClaimParseError) as exc:
        _claim({**BETWEEN, "floor_strike": 800, "cap_strike": 700})
    assert exc.value.reason is RejectReason.inconsistent_bounds


def test_missing_rules_is_rejected() -> None:
    with pytest.raises(ClaimParseError) as exc:
        _claim({**ABOVE, "rules_primary": "   "})
    assert exc.value.reason is RejectReason.missing_rules


def test_unidentifiable_source_is_rejected() -> None:
    """Capital is locked until resolution; an unknown settlement source is not
    a risk worth taking silently."""
    with pytest.raises(ClaimParseError) as exc:
        _claim({**ABOVE, "rules_primary": "Resolves however the exchange decides."})
    assert exc.value.reason is RejectReason.unidentified_resolution_source


def test_missing_resolution_time_is_rejected() -> None:
    raw = {**ABOVE}
    raw.pop("close_time")
    with pytest.raises(ClaimParseError) as exc:
        _claim(raw)
    assert exc.value.reason is RejectReason.missing_resolution_time


# ── source extraction ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "rules,expected",
    [
        ("... CF Benchmarks' Bitcoin Real-Time Index (BRTI) before ...", "CF Benchmarks BRTI"),
        ("... CF Benchmarks' BNBUSD_RTI before ...", "CF Benchmarks BNBUSD_RTI"),
        ("... CF Benchmarks' BRTI before 1:15 PM ...", "CF Benchmarks BRTI"),  # short code
        ("... (SHIBUSD_RTI) ...", "SHIBUSD_RTI"),  # index without named provider
    ],
)
def test_extract_resolution_source(rules, expected) -> None:
    assert extract_resolution_source(rules) == expected


def test_extract_returns_none_when_absent() -> None:
    assert extract_resolution_source("no index named here") is None
    assert extract_resolution_source("") is None


# ── registry ───────────────────────────────────────────────────────


def test_registry_is_fail_closed() -> None:
    """Ticker shapes do not support a rule: KXSOLE and KXSOLD are both SOL."""
    reg = UnderlyingRegistry({"KXSOLE": "SOL", "KXSOLD": "SOL"})
    assert reg.lookup("KXSOLE") == "SOL"
    assert reg.lookup("kxsold") == "SOL"  # case-insensitive
    with pytest.raises(ClaimParseError):
        reg.lookup("KXSOL")  # plausible-looking, unmapped -> refused


def test_registry_loads_shipped_config() -> None:
    from pathlib import Path

    reg = UnderlyingRegistry.from_yaml(Path("config/underlyings.yaml"))
    assert reg.lookup("KXBTC15M") == "BTC"
    assert reg.lookup("KXSHIBA") == "SHIB"  # not "SHIBA"
    assert {"BTC", "ETH", "SOL"} <= reg.symbols


def test_registry_rejects_empty_config(tmp_path) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text("series: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no `series` mapping"):
        UnderlyingRegistry.from_yaml(path)


# ── batch report ───────────────────────────────────────────────────


def test_parse_claims_groups_rejections_with_examples() -> None:
    markets = [parse_market(r) for r in (ABOVE, BELOW, BETWEEN, RELATIVE, CUSTOM)]
    markets.append(parse_market({**ABOVE, "ticker": "X-1", "series_ticker": "KXUNKNOWN"}))
    report = parse_claims(markets, REGISTRY)

    assert report.eligible_count == 4
    assert report.rejected_count == 2
    assert report.rejections[RejectReason.unsupported_strike_type.value] == 1
    assert report.rejections[RejectReason.unmapped_series.value] == 1
    # An example per reason, so the operator can see *what* was dropped.
    assert "KXSHIBA" in report.rejected_examples[RejectReason.unsupported_strike_type.value]


def test_report_serialises_for_the_cli() -> None:
    report = parse_claims([parse_market(ABOVE), parse_market(CUSTOM)], REGISTRY)
    out = report.as_dict()
    assert out["eligible"] == 1
    assert out["rejections_by_reason"]["unsupported_strike_type"] == 1


# ── description + horizon ──────────────────────────────────────────


def test_describe_reads_as_english() -> None:
    assert _claim(ABOVE).describe() == (
        "BTC above 75299.99 at 2026-07-22 18:00 UTC, per CF Benchmarks BRTI"
    )
    assert "measured reference" in _claim(RELATIVE).describe()
    assert _claim(BETWEEN).describe().startswith("BNB between 755 and 759.99")


def test_hours_to_resolution() -> None:
    now = dt.datetime(2026, 7, 22, 16, 0, tzinfo=dt.timezone.utc)
    assert _claim(ABOVE).hours_to_resolution(now) == pytest.approx(2.0)
