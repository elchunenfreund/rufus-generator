"""Adaptive rate limiter for Claude API — monitors RPM and TPM, throttles on 429s.

NOTE: This is a per-process limiter. With multiple Celery workers, each gets its own
copy, so effective limits are multiplied by worker count. For production with >1 worker,
replace with a Redis-backed limiter (use INCR + EXPIRE for sliding window counters).
For V1 with a single worker process, this is sufficient.
"""

import threading
import time
from dataclasses import dataclass, field

from app.config import get_logger, settings

log = get_logger(__name__)


@dataclass
class RateLimiterState:
    """Thread-safe rate limiter tracking RPM and TPM with adaptive throttling."""

    rpm_limit: int = settings.anthropic_rpm_limit
    tpm_limit: int = settings.anthropic_tpm_limit
    max_concurrent: int = settings.anthropic_max_concurrent

    # Sliding window tracking
    _request_times: list[float] = field(default_factory=list)
    _token_log: list[tuple[float, int]] = field(default_factory=list)  # (timestamp, tokens)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # Adaptive throttling
    _current_concurrent: int = 0
    _throttle_until: float = 0.0
    _backoff_active: bool = False

    @property
    def effective_concurrency(self) -> int:
        if self._backoff_active:
            return max(1, self.max_concurrent // 2)
        return self.max_concurrent

    def acquire(self) -> bool:
        """Try to acquire a slot. Returns True if allowed, False if should wait."""
        with self._lock:
            now = time.time()

            # Check throttle
            if now < self._throttle_until:
                return False

            # Check concurrency
            if self._current_concurrent >= self.effective_concurrency:
                return False

            # Clean old entries (60-second window)
            cutoff = now - 60
            self._request_times = [t for t in self._request_times if t > cutoff]
            self._token_log = [(t, tok) for t, tok in self._token_log if t > cutoff]

            # Check RPM
            if len(self._request_times) >= self.rpm_limit:
                return False

            # Check TPM
            current_tpm = sum(tok for _, tok in self._token_log)
            if current_tpm >= self.tpm_limit:
                return False

            self._request_times.append(now)
            self._current_concurrent += 1
            return True

    def release(self, tokens_used: int = 0):
        """Release a slot after request completes."""
        with self._lock:
            self._current_concurrent = max(0, self._current_concurrent - 1)
            if tokens_used > 0:
                self._token_log.append((time.time(), tokens_used))

    def on_rate_limit(self):
        """Called when we receive a 429 — activate backoff."""
        with self._lock:
            self._backoff_active = True
            self._throttle_until = time.time() + 60  # 60-second cooldown
            log.warning(
                "rate_limit_backoff",
                effective_concurrency=self.effective_concurrency,
                throttle_seconds=60,
            )

    def on_success(self):
        """Called on successful request — recover from backoff after cooldown."""
        with self._lock:
            if self._backoff_active and time.time() > self._throttle_until:
                self._backoff_active = False
                log.info("rate_limit_recovered", concurrency=self.max_concurrent)

    def wait_for_slot(self, timeout: float = 300) -> bool:
        """Block until a slot is available. Returns False on timeout."""
        start = time.time()
        while time.time() - start < timeout:
            if self.acquire():
                return True
            time.sleep(0.5)
        return False


# Global singleton
rate_limiter = RateLimiterState()
