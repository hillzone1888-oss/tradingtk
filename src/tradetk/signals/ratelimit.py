"""Client-side token-bucket rate limiter.

Moon Dev documents 60 req/s sustained, 200 burst. This throttles *before* the
request so we stay under the limit rather than reacting to 429s. Clock and sleep
are injectable so tests exercise it without real time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class RateLimiter:
    rate_per_s: float
    burst: float
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    _tokens: float = field(default=0.0, init=False)
    _last: float = field(default=0.0, init=False)
    _init: bool = field(default=False, init=False)

    def _refill(self) -> None:
        now = self.clock()
        if not self._init:
            self._tokens = self.burst
            self._last = now
            self._init = True
            return
        elapsed = now - self._last
        self._last = now
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate_per_s)

    def acquire(self, n: float = 1.0) -> None:
        """Block (via injected sleep) until `n` tokens are available, then spend them."""
        if n > self.burst:
            raise ValueError(f"cannot acquire {n} tokens; burst capacity is {self.burst}")
        self._refill()
        if self._tokens < n:
            deficit = n - self._tokens
            self.sleep(deficit / self.rate_per_s)
            self._refill()
        self._tokens -= n
