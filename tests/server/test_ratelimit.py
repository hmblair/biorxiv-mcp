"""Tests for the token bucket rate limiter."""

from biorxiv_mcp.server.ratelimit import TokenBucket


def test_consume_within_burst():
    bucket = TokenBucket(rate=10, burst=3)
    assert bucket.consume() is None
    assert bucket.consume() is None
    assert bucket.consume() is None


def test_consume_exceeds_burst():
    bucket = TokenBucket(rate=10, burst=2)
    bucket.consume()
    bucket.consume()
    wait = bucket.consume()
    assert wait is not None
    assert wait > 0


def test_tokens_refill_over_time():
    bucket = TokenBucket(rate=10, burst=1)
    bucket.consume()
    wait = bucket.consume()
    assert wait is not None

    # Simulate time passing
    bucket._last -= 0.2  # 0.2s * 10/s = 2 tokens refilled
    assert bucket.consume() is None


def test_burst_caps_tokens():
    bucket = TokenBucket(rate=100, burst=2)
    bucket._last -= 10  # Would refill 1000 tokens, but burst caps at 2
    bucket.consume()
    bucket.consume()
    wait = bucket.consume()
    assert wait is not None


def test_wait_time_is_reasonable():
    bucket = TokenBucket(rate=2, burst=1)
    bucket.consume()
    wait = bucket.consume()
    assert wait is not None
    assert 0 < wait <= 1.0  # At 2/s, wait should be ~0.5s
