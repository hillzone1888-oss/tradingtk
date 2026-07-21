"""Transport resilience: retry-with-backoff and a circuit breaker.

Both take injectable `clock` and `sleep` seams so tests run without real time.
Kept provider-agnostic so the Kalshi/Polymarket clients can reuse them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, TypeVar

T = TypeVar("T")


class CircuitOpenError(Exception):
    """Raised by a call attempted while the breaker is open."""


@dataclass
class CircuitBreaker:
    """Opens after `fail_threshold` consecutive failures; half-opens after a
    cooldown, then closes on the next success or re-opens on the next failure."""

    fail_threshold: int = 5
    reset_after_s: float = 30.0
    clock: Callable[[], float] = time.monotonic

    _consecutive_failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)

    def allow(self) -> bool:
        """Whether a call may proceed right now."""
        if self._opened_at is None:
            return True
        if self.clock() - self._opened_at >= self.reset_after_s:
            return True  # half-open: allow a trial call
        return False

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.fail_threshold:
            self._opened_at = self.clock()

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None and not self.allow()


@dataclass
class RetryPolicy:
    """Exponential backoff with a hard attempt cap. `sleep` is injectable."""

    max_attempts: int = 3
    backoff_base_s: float = 0.5
    backoff_factor: float = 2.0
    max_backoff_s: float = 8.0
    sleep: Callable[[float], None] = time.sleep

    def backoff_for(self, attempt: int) -> float:
        """Delay before the given 1-indexed attempt's retry."""
        return min(self.backoff_base_s * (self.backoff_factor ** (attempt - 1)), self.max_backoff_s)


def call_resilient(
    fn: Callable[[], T],
    *,
    retry: RetryPolicy,
    breaker: CircuitBreaker | None = None,
    retry_on: tuple[type[Exception], ...] = (Exception,),
) -> T:
    """Invoke `fn` with retry/backoff and an optional circuit breaker.

    Re-raises the last error after exhausting attempts; raises
    :class:`CircuitOpenError` immediately if the breaker is open.
    """
    if breaker is not None and not breaker.allow():
        raise CircuitOpenError("circuit breaker is open; refusing call")

    last: Exception | None = None
    for attempt in range(1, retry.max_attempts + 1):
        try:
            result = fn()
        except retry_on as exc:  # noqa: PERF203 - explicit retry loop
            last = exc
            if breaker is not None:
                breaker.record_failure()
            if attempt < retry.max_attempts:
                retry.sleep(retry.backoff_for(attempt))
            continue
        else:
            if breaker is not None:
                breaker.record_success()
            return result
    assert last is not None
    raise last
