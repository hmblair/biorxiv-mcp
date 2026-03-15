"""In-process token bucket rate limiter."""

import time


class TokenBucket:
    """Simple token bucket: allows `rate` requests per second with `burst` capacity."""

    def __init__(self, rate: float, burst: int):
        self.rate = rate
        self.burst = burst
        self._tokens = float(burst)
        self._last = time.monotonic()

    def consume(self) -> float | None:
        """Try to consume a token.

        Returns None on success, or the number of seconds to wait if
        the bucket is empty.
        """
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
        if self._tokens >= 1:
            self._tokens -= 1
            return None
        return (1 - self._tokens) / self.rate
