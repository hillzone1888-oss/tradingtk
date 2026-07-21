"""Retry/backoff and circuit-breaker behaviour, with injected clock + sleep."""

from __future__ import annotations

import pytest

from tradetk.signals.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    RetryPolicy,
    call_resilient,
)


def test_retry_succeeds_after_transient_failures() -> None:
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("transient")
        return "ok"

    policy = RetryPolicy(max_attempts=3, sleep=lambda _s: None)
    assert call_resilient(flaky, retry=policy) == "ok"
    assert calls["n"] == 3


def test_retry_exhausts_and_reraises_last() -> None:
    def always_fail() -> None:
        raise ValueError("nope")

    policy = RetryPolicy(max_attempts=2, sleep=lambda _s: None)
    with pytest.raises(ValueError, match="nope"):
        call_resilient(always_fail, retry=policy)


def test_backoff_is_exponential_and_capped() -> None:
    policy = RetryPolicy(backoff_base_s=1.0, backoff_factor=2.0, max_backoff_s=5.0)
    assert policy.backoff_for(1) == 1.0
    assert policy.backoff_for(2) == 2.0
    assert policy.backoff_for(3) == 4.0
    assert policy.backoff_for(4) == 5.0  # capped


def test_circuit_opens_then_half_opens_after_cooldown() -> None:
    t = {"now": 0.0}
    cb = CircuitBreaker(fail_threshold=2, reset_after_s=30.0, clock=lambda: t["now"])

    assert cb.allow()
    cb.record_failure()
    assert cb.allow()  # 1 failure < threshold
    cb.record_failure()  # opens
    assert not cb.allow()
    assert cb.is_open

    t["now"] = 31.0  # cooldown elapsed -> half-open
    assert cb.allow()
    cb.record_success()  # closes
    assert cb.allow()


def test_call_resilient_refuses_when_circuit_open() -> None:
    t = {"now": 0.0}
    cb = CircuitBreaker(fail_threshold=1, reset_after_s=100.0, clock=lambda: t["now"])
    cb.record_failure()  # opens immediately
    with pytest.raises(CircuitOpenError):
        call_resilient(lambda: "x", retry=RetryPolicy(sleep=lambda _s: None), breaker=cb)
