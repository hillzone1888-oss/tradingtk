"""Token-bucket rate limiter, with injected clock + sleep (no real waiting)."""

from __future__ import annotations

from tradetk.signals.ratelimit import RateLimiter


def _limiter(rate: float, burst: float):
    t = {"now": 0.0}
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        t["now"] += s  # sleeping advances the clock

    lim = RateLimiter(rate_per_s=rate, burst=burst, clock=lambda: t["now"], sleep=sleep)
    return lim, t, slept


def test_burst_is_free_then_throttles() -> None:
    lim, t, slept = _limiter(rate=10.0, burst=3.0)
    for _ in range(3):
        lim.acquire()  # burst budget, no sleep
    assert slept == []
    lim.acquire()  # bucket empty -> must wait 1 token / 10 per s = 0.1s
    assert slept == [0.1]


def test_refill_over_time_avoids_sleep() -> None:
    lim, t, slept = _limiter(rate=10.0, burst=1.0)
    lim.acquire()  # spends the one token
    t["now"] += 1.0  # 1s passes -> +10 tokens (capped at burst=1)
    lim.acquire()  # token available again, no sleep
    assert slept == []


def test_acquire_more_than_burst_raises() -> None:
    lim, _t, _s = _limiter(rate=10.0, burst=5.0)
    try:
        lim.acquire(6)
    except ValueError as exc:
        assert "burst" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")
