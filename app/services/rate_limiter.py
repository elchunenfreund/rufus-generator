"""Adaptive rate limiter for Claude API — shared across Celery workers via Redis.

Uses Redis INCR + EXPIRE for RPM/TPM sliding-window counters and a Redis key for
concurrent-request tracking. Falls back to in-memory state when Redis is unavailable
(e.g. local development without a Redis server).
"""

import threading
import time
from dataclasses import dataclass, field

import redis as redis_lib

from app.config import get_logger, settings

log = get_logger(__name__)

# Redis key prefix — all rate-limiter keys live under this namespace.
_PREFIX = "rufus:ratelimit"
_RPM_KEY = f"{_PREFIX}:rpm"
_TPM_KEY = f"{_PREFIX}:tpm"
_CONCURRENT_KEY = f"{_PREFIX}:concurrent"
_THROTTLE_KEY = f"{_PREFIX}:throttle_until"
_BACKOFF_KEY = f"{_PREFIX}:backoff"


def _make_redis_client() -> redis_lib.Redis | None:
    """Try to connect to Redis; return None if unavailable."""
    try:
        client = redis_lib.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        client.ping()
        log.info("rate_limiter_redis_connected", redis_url=settings.redis_url)
        return client
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "rate_limiter_redis_unavailable",
            error=str(exc),
            fallback="in-memory",
        )
        return None


# ---------------------------------------------------------------------------
# In-memory fallback (original per-process implementation)
# ---------------------------------------------------------------------------

@dataclass
class _InMemoryState:
    """Thread-safe, per-process rate limiter (fallback when Redis is down)."""

    rpm_limit: int = settings.anthropic_rpm_limit
    tpm_limit: int = settings.anthropic_tpm_limit
    max_concurrent: int = settings.anthropic_max_concurrent

    _request_times: list[float] = field(default_factory=list)
    _token_log: list[tuple[float, int]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    _current_concurrent: int = 0
    _throttle_until: float = 0.0
    _backoff_active: bool = False

    @property
    def effective_concurrency(self) -> int:
        if self._backoff_active:
            return max(1, self.max_concurrent // 2)
        return self.max_concurrent

    def acquire(self) -> bool:
        with self._lock:
            now = time.time()
            if now < self._throttle_until:
                return False
            if self._current_concurrent >= self.effective_concurrency:
                return False
            cutoff = now - 60
            self._request_times = [t for t in self._request_times if t > cutoff]
            self._token_log = [(t, tok) for t, tok in self._token_log if t > cutoff]
            if len(self._request_times) >= self.rpm_limit:
                return False
            current_tpm = sum(tok for _, tok in self._token_log)
            if current_tpm >= self.tpm_limit:
                return False
            self._request_times.append(now)
            self._current_concurrent += 1
            return True

    def release(self, tokens_used: int = 0):
        with self._lock:
            self._current_concurrent = max(0, self._current_concurrent - 1)
            if tokens_used > 0:
                self._token_log.append((time.time(), tokens_used))

    def on_rate_limit(self):
        with self._lock:
            self._backoff_active = True
            self._throttle_until = time.time() + 60
            log.warning(
                "rate_limit_backoff",
                effective_concurrency=self.effective_concurrency,
                throttle_seconds=60,
            )

    def on_success(self):
        with self._lock:
            if self._backoff_active and time.time() > self._throttle_until:
                self._backoff_active = False
                log.info("rate_limit_recovered", concurrency=self.max_concurrent)


# ---------------------------------------------------------------------------
# Redis-backed implementation
# ---------------------------------------------------------------------------

class _RedisState:
    """Rate limiter using Redis for cross-worker shared state.

    RPM:  Redis key incremented per request with a 60-second TTL.
    TPM:  Redis key incremented by token count with a 60-second TTL.
    Concurrent: Redis counter incremented on acquire, decremented on release.
    Throttle/backoff: stored as Redis keys with TTL so all workers observe them.
    """

    def __init__(self, client: redis_lib.Redis):
        self._r = client
        self.rpm_limit: int = settings.anthropic_rpm_limit
        self.tpm_limit: int = settings.anthropic_tpm_limit
        self.max_concurrent: int = settings.anthropic_max_concurrent

    @property
    def effective_concurrency(self) -> int:
        if self._r.exists(_BACKOFF_KEY):
            return max(1, self.max_concurrent // 2)
        return self.max_concurrent

    # -- Lua scripts for atomic operations ----------------------------------

    # Atomically: check all limits, if OK increment counters, return 1; else 0.
    _ACQUIRE_SCRIPT = """
    local rpm_key        = KEYS[1]
    local tpm_key        = KEYS[2]
    local concurrent_key = KEYS[3]
    local throttle_key   = KEYS[4]
    local backoff_key    = KEYS[5]

    local rpm_limit      = tonumber(ARGV[1])
    local tpm_limit      = tonumber(ARGV[2])
    local max_conc       = tonumber(ARGV[3])

    -- Throttle active?
    if redis.call('EXISTS', throttle_key) == 1 then
        return 0
    end

    -- Effective concurrency (halved during backoff)
    local eff_conc = max_conc
    if redis.call('EXISTS', backoff_key) == 1 then
        eff_conc = math.max(1, math.floor(max_conc / 2))
    end

    -- Check concurrent
    local cur_conc = tonumber(redis.call('GET', concurrent_key) or '0') or 0
    if cur_conc >= eff_conc then
        return 0
    end

    -- Check RPM
    local cur_rpm = tonumber(redis.call('GET', rpm_key) or '0') or 0
    if cur_rpm >= rpm_limit then
        return 0
    end

    -- Check TPM
    local cur_tpm = tonumber(redis.call('GET', tpm_key) or '0') or 0
    if cur_tpm >= tpm_limit then
        return 0
    end

    -- All clear — increment counters
    if redis.call('EXISTS', rpm_key) == 0 then
        redis.call('SET', rpm_key, 1, 'EX', 60)
    else
        redis.call('INCR', rpm_key)
    end

    redis.call('INCRBY', concurrent_key, 1)

    return 1
    """

    def acquire(self) -> bool:
        try:
            result = self._r.eval(
                self._ACQUIRE_SCRIPT,
                5,
                _RPM_KEY,
                _TPM_KEY,
                _CONCURRENT_KEY,
                _THROTTLE_KEY,
                _BACKOFF_KEY,
                self.rpm_limit,
                self.tpm_limit,
                self.max_concurrent,
            )
            return bool(result)
        except redis_lib.RedisError as exc:
            log.warning("rate_limiter_redis_error", action="acquire", error=str(exc))
            return False

    def release(self, tokens_used: int = 0):
        try:
            pipe = self._r.pipeline(transaction=False)
            pipe.decr(_CONCURRENT_KEY)
            if tokens_used > 0:
                # Increment TPM counter; set TTL if key is new
                pipe.incrby(_TPM_KEY, tokens_used)
            pipe.execute()

            # Ensure TPM key has a TTL (it may have been created by INCRBY
            # without one, or the TTL may have been lost).
            if tokens_used > 0 and self._r.ttl(_TPM_KEY) < 0:
                self._r.expire(_TPM_KEY, 60)

            # Floor concurrent at 0 (safety against drift)
            if int(self._r.get(_CONCURRENT_KEY) or 0) < 0:
                self._r.set(_CONCURRENT_KEY, 0)
        except redis_lib.RedisError as exc:
            log.warning("rate_limiter_redis_error", action="release", error=str(exc))

    def on_rate_limit(self):
        try:
            # Both backoff flag and throttle share a 60-second TTL
            pipe = self._r.pipeline(transaction=False)
            pipe.set(_BACKOFF_KEY, "1", ex=60)
            pipe.set(_THROTTLE_KEY, "1", ex=60)
            pipe.execute()
            log.warning(
                "rate_limit_backoff",
                effective_concurrency=self.effective_concurrency,
                throttle_seconds=60,
            )
        except redis_lib.RedisError as exc:
            log.warning("rate_limiter_redis_error", action="on_rate_limit", error=str(exc))

    def on_success(self):
        try:
            # Recovery: if backoff is active but throttle has already expired,
            # clear the backoff flag.
            if self._r.exists(_BACKOFF_KEY) and not self._r.exists(_THROTTLE_KEY):
                self._r.delete(_BACKOFF_KEY)
                log.info("rate_limit_recovered", concurrency=self.max_concurrent)
        except redis_lib.RedisError as exc:
            log.warning("rate_limiter_redis_error", action="on_success", error=str(exc))


# ---------------------------------------------------------------------------
# Public wrapper — same API regardless of backend
# ---------------------------------------------------------------------------

class RateLimiterState:
    """Unified rate limiter. Uses Redis when available, in-memory otherwise."""

    def __init__(self):
        client = _make_redis_client()
        if client is not None:
            self._backend = _RedisState(client)
            self._using_redis = True
        else:
            self._backend = _InMemoryState()
            self._using_redis = False

    # Expose settings so callers that inspect these still work.
    @property
    def rpm_limit(self) -> int:
        return self._backend.rpm_limit

    @property
    def tpm_limit(self) -> int:
        return self._backend.tpm_limit

    @property
    def max_concurrent(self) -> int:
        return self._backend.max_concurrent

    @property
    def effective_concurrency(self) -> int:
        return self._backend.effective_concurrency

    def acquire(self) -> bool:
        """Try to acquire a slot. Returns True if allowed, False if should wait."""
        return self._backend.acquire()

    def release(self, tokens_used: int = 0):
        """Release a slot after request completes."""
        self._backend.release(tokens_used)

    def on_rate_limit(self):
        """Called when we receive a 429 — activate backoff."""
        self._backend.on_rate_limit()

    def on_success(self):
        """Called on successful request — recover from backoff after cooldown."""
        self._backend.on_success()

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
