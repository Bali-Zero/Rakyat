"""The counting script, against a real Redis.

Every admitted request is one member of a sorted set; one atomic script drops what left
the window, counts, and admits or refuses. These tests run the script itself: they need
the Redis named by ``REDIS_URL`` and skip without it (a fake Redis cannot run a script
here). Each test uses a key of its own and deletes it; nothing else on the server is
touched, and no script cache is flushed.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from backend.middleware.rate_limiter import RateLimiter

pytestmark = pytest.mark.redis


def _connect(**options: object):
    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.skip("REDIS_URL is not set: these tests count on a real Redis")
    import redis

    client = redis.Redis.from_url(url, **options)
    try:
        client.ping()
    except redis.RedisError as exc:
        pytest.skip(f"the Redis of REDIS_URL is not reachable ({type(exc).__name__})")
    return client


def _limiter_on(client: object | None) -> RateLimiter:
    """A limiter bound, through the manager, to the given client (or to none)."""
    with patch("backend.core.redis_manager.RedisManager") as manager_class:
        manager = MagicMock()
        manager.get_sync_client.return_value = client
        manager_class.get_instance.return_value = manager
        return RateLimiter()


@pytest.fixture
def redis_client() -> Iterator[object]:
    client = _connect()
    yield client
    client.close()


@pytest.fixture
def limiter(redis_client: object) -> RateLimiter:
    return _limiter_on(redis_client)


@pytest.fixture
def key(redis_client) -> Iterator[str]:
    name = f"ratelimit:test:{uuid.uuid4().hex}"
    yield name
    redis_client.delete(name)


def _allowed(results: list[tuple[bool, dict]]) -> list[bool]:
    return [allowed for allowed, _ in results]


# --------------------------------------------------------------------------- #
# Counting
# --------------------------------------------------------------------------- #
def test_every_request_inside_one_second_is_counted(limiter: RateLimiter, key: str) -> None:
    """The defect this replaces: one member per whole second, so a burst was one request."""
    results = [limiter.is_allowed(key, limit=3, window=60) for _ in range(5)]

    assert _allowed(results) == [True, True, True, False, False]
    assert all(info["backend"] == "redis" for _, info in results)


def test_remaining_falls_by_one_per_admitted_request(limiter: RateLimiter, key: str) -> None:
    results = [limiter.is_allowed(key, limit=3, window=60) for _ in range(4)]

    assert [info["remaining"] for _, info in results] == [2, 1, 0, 0]
    assert [info["limit"] for _, info in results] == [3, 3, 3, 3]


def test_a_refused_request_is_not_stored(limiter: RateLimiter, redis_client, key: str) -> None:
    for _ in range(2):
        limiter.is_allowed(key, limit=2, window=60)
    # An expiry far shorter than the window: a refusal that renewed it would put
    # the full window back, which two readings of the full window could not show.
    short_ttl_ms = 5_000
    assert redis_client.pexpire(key, short_ttl_ms)

    refusals = [limiter.is_allowed(key, limit=2, window=60) for _ in range(50)]

    assert not any(_allowed(refusals))
    assert redis_client.zcard(key) == 2
    # The expiry belongs to the last admission: a flood of refusals never renews it.
    assert 0 < redis_client.pttl(key) <= short_ttl_ms


def test_fifty_concurrent_callers_against_a_limit_of_ten_admit_exactly_ten(
    redis_client, key: str
) -> None:
    """Independent connections and limiter instances: the script is the only lock."""
    clients = [_connect() for _ in range(8)]
    limiters = [_limiter_on(client) for client in clients]
    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [
                pool.submit(limiters[i % len(limiters)].is_allowed, key, 10, 60) for i in range(50)
            ]
            results = [future.result() for future in futures]
    finally:
        for client in clients:
            client.close()

    assert sum(_allowed(results)) == 10
    assert redis_client.zcard(key) == 10


# --------------------------------------------------------------------------- #
# The window's edges (the time of each request is given, so the edges are exact)
# --------------------------------------------------------------------------- #
def test_a_request_leaves_the_window_exactly_one_window_later(
    limiter: RateLimiter, key: str
) -> None:
    assert limiter.is_allowed(key, limit=1, window=60, now=1000.0)[0] is True

    still_inside = limiter.is_allowed(key, limit=1, window=60, now=1059.999999)
    at_the_edge = limiter.is_allowed(key, limit=1, window=60, now=1060.0)

    assert still_inside[0] is False
    assert at_the_edge[0] is True  # a score of exactly now - window has left


def test_the_caller_is_admitted_again_when_its_oldest_request_leaves(
    limiter: RateLimiter, key: str
) -> None:
    limiter.is_allowed(key, limit=2, window=60, now=1000.0)
    limiter.is_allowed(key, limit=2, window=60, now=1030.0)

    refused, info = limiter.is_allowed(key, limit=2, window=60, now=1059.0)
    assert refused is False
    assert info["reset"] == 1060  # the second the oldest admitted request leaves
    assert info["retry_after"] == 1

    admitted, info = limiter.is_allowed(key, limit=2, window=60, now=1060.0)
    assert admitted is True
    assert info["reset"] == 1090  # now the request of 1030 is the oldest
    assert "retry_after" not in info


def test_reset_is_the_same_on_admitted_and_refused_answers(limiter: RateLimiter, key: str) -> None:
    _, first = limiter.is_allowed(key, limit=2, window=60, now=2000.25)
    _, second = limiter.is_allowed(key, limit=2, window=60, now=2010.0)
    _, refused = limiter.is_allowed(key, limit=2, window=60, now=2020.0)

    assert first["reset"] == second["reset"] == refused["reset"] == 2061  # ceil(2000.25 + 60)


@pytest.mark.parametrize(
    ("asked_at", "expected_wait"),
    [
        (3000.75, 60),  # 59.5 s left: rounded up, and never above the window
        (3030.0, 31),  # 30.25 s left
        (3059.5, 1),  # 0.75 s left
        (3060.2, 1),  # 0.05 s left: never zero
    ],
)
def test_retry_after_rounds_up_and_stays_within_the_window(
    limiter: RateLimiter, key: str, asked_at: float, expected_wait: int
) -> None:
    limiter.is_allowed(key, limit=1, window=60, now=3000.25)

    refused, info = limiter.is_allowed(key, limit=1, window=60, now=asked_at)

    assert refused is False
    assert info["retry_after"] == expected_wait


def test_an_hourly_window_counts_an_hour(limiter: RateLimiter, key: str) -> None:
    limiter.is_allowed(key, limit=2, window=3600, now=5000.0)
    limiter.is_allowed(key, limit=2, window=3600, now=5001.0)

    late, info = limiter.is_allowed(key, limit=2, window=3600, now=8599.5)
    assert late is False
    assert info["retry_after"] == 1
    assert info["reset"] == 8600

    assert limiter.is_allowed(key, limit=2, window=3600, now=8600.0)[0] is True


def test_the_key_expires_one_window_after_the_last_admission(
    limiter: RateLimiter, redis_client, key: str
) -> None:
    limiter.is_allowed(key, limit=5, window=60)

    assert 59_000 < redis_client.pttl(key) <= 60_000


# --------------------------------------------------------------------------- #
# The script and the client it is bound to
# --------------------------------------------------------------------------- #
def test_a_server_that_does_not_know_the_script_is_taught_in_the_same_call(
    limiter: RateLimiter, redis_client, key: str
) -> None:
    """NOSCRIPT, without flushing a server other tests share: an unknown digest."""
    limiter.is_allowed(key, limit=5, window=60)
    script = limiter._window_script()
    known = script.sha
    script.sha = "0" * 40  # the server has never seen this one: it answers NOSCRIPT

    allowed, info = limiter.is_allowed(key, limit=5, window=60)

    assert allowed is True
    assert info["backend"] == "redis"
    assert script.sha == known  # redis-py loaded the script and repeated the call
    assert redis_client.zcard(key) == 2
    assert limiter.metrics["redis_errors"] == 0


def test_a_start_without_redis_binds_the_script_when_a_client_arrives(
    redis_client, key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    limiter = _limiter_on(None)
    assert limiter.redis_available is False
    assert limiter._script is None
    assert limiter.is_allowed(key, limit=5, window=60)[1]["backend"] == "memory"

    with patch("backend.core.redis_manager.RedisManager") as manager_class:
        manager = MagicMock()
        manager.get_sync_client.return_value = redis_client
        manager_class.get_instance.return_value = manager
        assert limiter.refresh_from_manager() is True

    allowed, info = limiter.is_allowed(key, limit=5, window=60)

    assert (allowed, info["backend"]) == (True, "redis")
    assert redis_client.zcard(key) == 1


def test_the_script_is_never_kept_across_clients(limiter: RateLimiter, key: str) -> None:
    first_client = limiter.redis_client
    first_script = limiter._window_script()
    assert first_script.registered_client is first_client

    second_client = _connect()
    try:
        limiter.redis_client = second_client
        assert limiter._window_script().registered_client is second_client
        assert limiter.is_allowed(key, limit=5, window=60)[1]["backend"] == "redis"
    finally:
        second_client.close()


def test_every_bind_registers_the_script_again(limiter: RateLimiter, redis_client) -> None:
    before = limiter._window_script()

    with patch("backend.core.redis_manager.RedisManager") as manager_class:
        manager = MagicMock()
        manager.get_sync_client.return_value = redis_client
        manager_class.get_instance.return_value = manager
        limiter.refresh_from_manager()

    assert limiter._script is None  # dropped by the bind
    assert limiter._window_script() is not before


def test_a_reply_lost_after_the_script_ran_takes_the_half_limit_path(
    limiter: RateLimiter, redis_client, key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.middleware import rate_limiter as rl

    monkeypatch.setattr(rl, "_rate_limit_storage", {})
    real = limiter._window_script()

    def ran_then_lost(**kwargs: object) -> None:
        real(**kwargs)
        raise ConnectionError("the reply never arrived")

    limiter._script = ran_then_lost

    allowed, info = limiter.is_allowed(key, limit=10, window=60)

    assert allowed is True
    assert info["backend"] == "memory_degraded"
    assert info["limit"] == 5
    assert limiter.metrics["redis_errors"] == 1
    # The server did run it: that one request is held by both counters.
    assert redis_client.zcard(key) == 1


def test_a_client_that_decodes_replies_counts_the_same(key: str) -> None:
    client = _connect(decode_responses=True)
    try:
        limiter = _limiter_on(client)
        limiter.is_allowed(key, limit=1, window=60, now=4000.5)
        refused, info = limiter.is_allowed(key, limit=1, window=60, now=4010.0)
    finally:
        client.close()

    assert refused is False
    assert info["reset"] == 4061
    assert info["retry_after"] == 51
