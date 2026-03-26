"""Tests for rate_limiter service — tests the _InMemoryState backend directly."""

import time

import pytest

from app.services.rate_limiter import _InMemoryState


class TestRateLimiterAcquireRelease:
    def test_acquire_succeeds_initially(self):
        limiter = _InMemoryState(rpm_limit=10, tpm_limit=100000, max_concurrent=5)
        assert limiter.acquire() is True

    def test_release_decrements_concurrent(self):
        limiter = _InMemoryState(rpm_limit=10, tpm_limit=100000, max_concurrent=5)
        limiter.acquire()
        limiter.release(tokens_used=100)
        assert limiter.acquire() is True

    def test_concurrency_limit_enforced(self):
        limiter = _InMemoryState(rpm_limit=100, tpm_limit=1000000, max_concurrent=2)
        assert limiter.acquire() is True
        assert limiter.acquire() is True
        assert limiter.acquire() is False  # 3rd should fail

    def test_rpm_limit_enforced(self):
        limiter = _InMemoryState(rpm_limit=3, tpm_limit=1000000, max_concurrent=100)
        for _ in range(3):
            assert limiter.acquire() is True
            limiter.release()
        assert limiter.acquire() is False


class TestRateLimiterBackoff:
    def test_on_rate_limit_activates_backoff(self):
        limiter = _InMemoryState(rpm_limit=100, tpm_limit=1000000, max_concurrent=10)
        limiter.on_rate_limit()
        assert limiter._backoff_active is True
        assert limiter.effective_concurrency == 5  # halved

    def test_throttle_blocks_acquire(self):
        limiter = _InMemoryState(rpm_limit=100, tpm_limit=1000000, max_concurrent=10)
        limiter.on_rate_limit()
        assert limiter.acquire() is False

    def test_on_success_recovers_after_cooldown(self):
        limiter = _InMemoryState(rpm_limit=100, tpm_limit=1000000, max_concurrent=10)
        limiter.on_rate_limit()
        limiter._throttle_until = time.time() - 1
        limiter.on_success()
        assert limiter._backoff_active is False
        assert limiter.effective_concurrency == 10


class TestRateLimiterTokenTracking:
    def test_tpm_limit_enforced(self):
        limiter = _InMemoryState(rpm_limit=100, tpm_limit=100, max_concurrent=100)
        limiter.acquire()
        limiter.release(tokens_used=90)
        limiter.acquire()
        limiter.release(tokens_used=20)  # total 110 > 100
        assert limiter.acquire() is False

    def test_release_records_tokens(self):
        limiter = _InMemoryState(rpm_limit=100, tpm_limit=100000, max_concurrent=10)
        limiter.acquire()
        limiter.release(tokens_used=500)
        assert len(limiter._token_log) == 1
        assert limiter._token_log[0][1] == 500


class TestRateLimiterStateWrapper:
    def test_wrapper_falls_back_to_in_memory(self):
        """When Redis is unavailable, RateLimiterState should use in-memory."""
        from app.services.rate_limiter import RateLimiterState
        limiter = RateLimiterState()
        # In test env without Redis, should fall back to in-memory
        # Just verify the public API works
        assert limiter.acquire() is True
        limiter.release(tokens_used=100)
        assert limiter.wait_for_slot(timeout=1) is True
