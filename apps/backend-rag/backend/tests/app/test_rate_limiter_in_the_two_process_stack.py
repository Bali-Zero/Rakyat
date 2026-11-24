"""The rate limiter in the real stack: two processes, one charge, the verified caller.

The API process faces the edge and forwards the heavy routes to the RAG worker through
its proxy route. Both are assembled by the same `register_middleware`. These tests drive
the REAL applications in-process (`main_api.app`, with its proxy pointed at
`main_rag.app`), with the application's limiter bound to the Redis of ``REDIS_URL``:

- one request through both processes is charged once, by the process that faces the edge;
- the limiter runs after authentication: what authentication refuses is not counted,
  what a route refuses is; a refusal is traced, audited with the verified caller,
  readable by a browser of ours, counted by threshold and paged to nobody.

No database: every route used here answers without one. Invented values only
(documentation addresses, stub keys). The tests skip without a reachable Redis.
"""

from __future__ import annotations

import ipaddress
import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend.app import main_api, rag_proxy
from backend.app.core.config import settings
from backend.app.main_rag import app as RAG
from backend.middleware import activity_logging
from backend.middleware import rate_limiter as rl
from backend.middleware.activity_logging import ActivityLoggingMiddleware
from backend.middleware.error_monitoring import ErrorMonitoringMiddleware
from backend.middleware.hybrid_auth import HybridAuthMiddleware
from backend.middleware.rate_limiter import RateLimitMiddleware
from backend.middleware.request_tracing import RequestTracingMiddleware
from backend.services.pii.violation_store import hash_subject

pytestmark = pytest.mark.redis

API = main_api.app

#: A public route the API forwards to the worker; the worker answers it without a database.
PUBLIC_FORWARDED = "/api/knowledge/visa"
#: The phone upsert: public for authentication, its own route checks a write key.
UPSERT = "/api/crm/clients/upsert-by-phone"
#: A route of the API process that needs a credential.
PROTECTED = "/api/admin/rate-limit/stats"
ORIGIN = "https://kita.balizero.com"


def _new_address() -> str:
    return str(ipaddress.ip_address(f"2001:db8::{uuid.uuid4().hex[:4]}:{uuid.uuid4().hex[:4]}"))


@pytest.fixture
def address() -> str:
    """A caller address of its own for each test, so no two tests share a counter."""
    return _new_address()


@pytest.fixture
def redis_client() -> Iterator[object]:
    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.skip("REDIS_URL is not set: these tests count on a real Redis")
    import redis

    client = redis.Redis.from_url(url)
    try:
        client.ping()
    except redis.RedisError as exc:
        pytest.skip(f"the Redis of REDIS_URL is not reachable ({type(exc).__name__})")
    yield client
    client.close()


@pytest.fixture
def members(redis_client, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str, str], int]]:
    """Bind the application's limiter to the real Redis; count what a caller has stored."""
    limiter = rl.rate_limiter
    monkeypatch.setattr(limiter, "redis_client", redis_client)
    monkeypatch.setattr(limiter, "redis_available", True)
    monkeypatch.setattr(limiter, "_script", None, raising=False)
    monkeypatch.setattr(limiter, "_script_client", None, raising=False)
    monkeypatch.setattr(limiter, "metrics", {**limiter.metrics, "refused": 0})
    monkeypatch.setattr(limiter, "refused_by_bucket", {}, raising=False)
    subjects: set[str] = set()

    def count(subject: str, path: str) -> int:
        subjects.add(subject)
        return int(redis_client.zcard(f"ratelimit:{hash_subject(subject)}:{path}"))

    yield count

    for subject in subjects:
        for key in redis_client.scan_iter(match=f"ratelimit:{hash_subject(subject)}:*"):
            redis_client.delete(key)


@pytest_asyncio.fixture
async def stack(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[AsyncClient, list[str]]]:
    """A client of the API process, whose proxy reaches the worker in-process.

    Yields the client and the list of paths the worker received.
    """
    received: list[str] = []

    async def worker(scope, receive, send) -> None:
        if scope["type"] == "http":
            received.append(scope["path"])
        await RAG(scope, receive, send)

    to_worker = httpx.AsyncClient(transport=ASGITransport(app=worker), base_url="http://rag.test")
    monkeypatch.setattr(rag_proxy, "_proxy_client", to_worker)
    async with AsyncClient(transport=ASGITransport(app=API), base_url="http://api.test") as client:
        yield client, received
    await to_worker.aclose()


@pytest.fixture
def api_key(monkeypatch: pytest.MonkeyPatch) -> Callable[..., str]:
    """Add a key to the key store of the application's own authentication middleware."""
    if API.middleware_stack is None:
        API.middleware_stack = API.build_middleware_stack()
    node = API.middleware_stack
    while node is not None and not isinstance(node, HybridAuthMiddleware):
        node = getattr(node, "app", None)
    assert node is not None, "the application has no authentication middleware"
    if not node.api_auth_enabled:
        pytest.skip("API authentication is switched off in this environment")
    store = node.api_key_auth

    def add(key: str, role: str = "user") -> str:
        info = {
            "role": role,
            "permissions": ["read"],
            "created_at": "2026-01-01T00:00:00+00:00",
            "description": "stub key of a test",
        }
        monkeypatch.setitem(store.valid_keys, key, info)
        monkeypatch.setitem(store.key_stats, key, {"usage_count": 0, "last_used": None})
        return key

    return add


def _from(address: str, **extra: str) -> dict[str, str]:
    return {"Fly-Client-IP": address, **extra}


# --------------------------------------------------------------------------- #
# Two processes, one charge
# --------------------------------------------------------------------------- #
def test_the_api_process_limits_after_authentication_and_the_worker_does_not() -> None:
    api_stack = [entry.cls for entry in API.user_middleware]
    worker_stack = [entry.cls for entry in RAG.user_middleware]

    # On a request, in this order; compression and CORS follow, CORS innermost.
    assert api_stack[:5] == [
        ActivityLoggingMiddleware,
        ErrorMonitoringMiddleware,
        RequestTracingMiddleware,
        HybridAuthMiddleware,
        RateLimitMiddleware,
    ]
    assert len(api_stack) == 7
    assert api_stack[-1].__name__ == "CORSMiddleware"
    # The worker is the same stack without the limiter: the API has already charged.
    assert RateLimitMiddleware not in worker_stack
    assert worker_stack == [cls for cls in api_stack if cls is not RateLimitMiddleware]


async def test_one_request_through_both_processes_is_charged_once(stack, members, address) -> None:
    client, received = stack

    response = await client.post(
        UPSERT, json={"phone_normalized": "620000000001"}, headers=_from(address)
    )

    assert received == [UPSERT]  # the worker did answer it
    assert response.status_code != 429
    assert members(f"addr:{address}", UPSERT) == 1
    # The caller sees the API's count: one request spent out of this path's own threshold.
    assert response.headers.get("x-ratelimit-limit") == "600"
    assert response.headers.get("x-ratelimit-remaining") == "599"


async def test_the_first_refused_request_is_the_one_after_the_limit(
    stack, members, address, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, received = stack
    monkeypatch.setitem(RateLimitMiddleware.RATE_LIMITS, PUBLIC_FORWARDED, (3, 60))

    responses = [await client.get(PUBLIC_FORWARDED, headers=_from(address)) for _ in range(5)]
    stranger = await client.get(PUBLIC_FORWARDED, headers=_from(_new_address()))

    assert [r.status_code == 429 for r in responses] == [False, False, False, True, True]
    assert [r.headers.get("x-ratelimit-remaining") for r in responses] == ["2", "1", "0", "0", "0"]
    assert {r.headers.get("x-ratelimit-limit") for r in responses} == {"3"}
    assert members(f"addr:{address}", PUBLIC_FORWARDED) == 3
    # A refused request never reaches the worker, and another caller is not affected.
    assert received.count(PUBLIC_FORWARDED) == 3 + 1
    assert stranger.status_code != 429


# --------------------------------------------------------------------------- #
# After authentication
# --------------------------------------------------------------------------- #
async def test_a_401_written_by_authentication_is_not_counted(stack, members, address) -> None:
    client, _ = stack

    response = await client.get(PROTECTED, headers=_from(address))

    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"
    assert "x-ratelimit-limit" not in response.headers
    assert members(f"addr:{address}", PROTECTED) == 0


async def test_a_403_of_the_role_boundary_is_not_counted(stack, members, address, api_key) -> None:
    client, _ = stack
    portal_key = api_key("stub-key-portal-role", role="client")

    response = await client.get(PROTECTED, headers=_from(address, **{"X-API-Key": portal_key}))

    assert response.status_code == 403
    assert "x-ratelimit-limit" not in response.headers
    assert members(f"addr:{address}", PROTECTED) == 0


async def test_a_503_of_a_failing_authentication_is_not_counted(
    stack, members, address, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = stack

    async def identity_store_down(self, request):
        raise RuntimeError("identity store down")

    monkeypatch.setattr(HybridAuthMiddleware, "authenticate_request", identity_store_down)

    response = await client.get(PROTECTED, headers=_from(address))

    assert response.status_code == 503
    assert "x-ratelimit-limit" not in response.headers
    assert members(f"addr:{address}", PROTECTED) == 0


async def test_a_refusal_written_by_a_route_is_counted(
    stack, members, address, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = stack
    monkeypatch.setattr(settings, "wa_mirror_crm_write_enabled", True)
    monkeypatch.setattr(settings, "wa_mirror_crm_write_key", "stub-write-key")

    response = await client.post(
        UPSERT,
        json={"phone_normalized": "620000000001"},
        headers=_from(address, **{"X-CRM-Write-Key": "not-the-key"}),
    )

    assert response.status_code == 401
    assert "www-authenticate" not in response.headers  # the route wrote it
    assert response.headers.get("x-ratelimit-limit") == "600"
    assert members(f"addr:{address}", UPSERT) == 1


async def test_valid_and_invalid_write_keys_from_one_address_share_its_counter(
    stack, members, address, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = stack
    monkeypatch.setattr(settings, "wa_mirror_crm_write_enabled", True)
    monkeypatch.setattr(settings, "wa_mirror_crm_write_key", "stub-write-key")
    elsewhere = _new_address()
    body = {"phone_normalized": "620000000001"}

    wrong = await client.post(
        UPSERT, json=body, headers=_from(address, **{"X-CRM-Write-Key": "not-the-key"})
    )
    right = await client.post(
        UPSERT, json=body, headers=_from(address, **{"X-CRM-Write-Key": "stub-write-key"})
    )
    await client.post(
        UPSERT, json=body, headers=_from(elsewhere, **{"X-CRM-Write-Key": "stub-write-key"})
    )

    assert wrong.status_code == 401
    assert right.status_code not in (401, 429)
    # A public route is counted by address, whatever key the request carries.
    assert members(f"addr:{address}", UPSERT) == 2
    assert members(f"addr:{elsewhere}", UPSERT) == 1


async def test_two_api_keys_of_one_role_do_not_share_a_counter(
    stack, members, address, api_key, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = stack
    path = f"/api/zz-limiter-test/{uuid.uuid4().hex}"
    monkeypatch.setitem(RateLimitMiddleware.RATE_LIMITS, path, (1, 60))
    one, two = api_key("stub-key-one"), api_key("stub-key-two")

    first = await client.get(path, headers=_from(address, **{"X-API-Key": one}))
    again = await client.get(path, headers=_from(address, **{"X-API-Key": one}))
    other = await client.get(path, headers=_from(address, **{"X-API-Key": two}))

    assert first.status_code == 404  # no such route: the router's answer, and counted
    assert again.status_code == 429
    assert other.status_code == 404  # same role, same address, its own counter
    assert members(f"addr:{address}", path) == 0  # neither is counted by address


async def test_a_refusal_is_traced_and_audited_with_the_verified_caller(
    stack, members, address, api_key, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = stack
    audit = AsyncMock()
    monkeypatch.setattr(activity_logging.activity_logger, "log_api_call", audit)
    path = f"/api/zz-limiter-test/{uuid.uuid4().hex}"
    monkeypatch.setitem(RateLimitMiddleware.RATE_LIMITS, path, (1, 60))
    headers = _from(address, **{"X-API-Key": api_key("stub-key-one")})

    await client.get(path, headers=headers)
    refused = await client.get(path, headers=headers)

    assert refused.status_code == 429
    assert refused.headers.get("x-correlation-id")
    assert refused.headers.get("x-auth-type") == "api_key"
    audited = audit.await_args_list[-1].kwargs
    assert audited["response_status"] == 429
    assert audited["user_email"] == "user@zantara.dev"  # the identity authentication verified


# --------------------------------------------------------------------------- #
# A refusal and the browser
# --------------------------------------------------------------------------- #
async def test_a_browser_of_ours_can_read_a_refusal(
    stack, members, address, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = stack
    monkeypatch.setitem(RateLimitMiddleware.RATE_LIMITS, PUBLIC_FORWARDED, (1, 60))
    headers = _from(address, Origin=ORIGIN)

    await client.get(PUBLIC_FORWARDED, headers=headers)
    refused = await client.get(PUBLIC_FORWARDED, headers=headers)

    assert refused.status_code == 429
    assert refused.headers.get("access-control-allow-origin") == ORIGIN
    assert refused.headers.get("access-control-allow-credentials") == "true"
    assert "Retry-After" in refused.headers.get("access-control-expose-headers", "")
    assert 1 <= int(refused.headers.get("retry-after", "0")) <= 60


async def test_a_preflight_is_answered_after_the_bucket_is_exhausted(
    stack, members, address, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = stack
    monkeypatch.setitem(RateLimitMiddleware.RATE_LIMITS, PUBLIC_FORWARDED, (1, 60))

    await client.get(PUBLIC_FORWARDED, headers=_from(address))
    refused = await client.get(PUBLIC_FORWARDED, headers=_from(address))
    preflight = await client.options(
        PUBLIC_FORWARDED,
        headers=_from(address, **{"Origin": ORIGIN, "Access-Control-Request-Method": "GET"}),
    )

    assert refused.status_code == 429
    assert preflight.status_code == 200
    assert preflight.headers.get("access-control-allow-origin") == ORIGIN
    assert members(f"addr:{address}", PUBLIC_FORWARDED) == 1  # the preflight stored nothing


async def test_a_refusal_pages_nobody_and_is_counted_by_threshold(
    stack, members, address, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = stack
    alerts = MagicMock()
    alerts.send_http_error_alert = AsyncMock()
    monkeypatch.setattr(API.state, "alert_service", alerts, raising=False)
    monkeypatch.setitem(RateLimitMiddleware.RATE_LIMITS, PUBLIC_FORWARDED, (1, 60))

    await client.get(PUBLIC_FORWARDED, headers=_from(address))
    refused = await client.get(PUBLIC_FORWARDED, headers=_from(address))

    assert refused.status_code == 429
    alerts.send_http_error_alert.assert_not_awaited()
    assert rl.rate_limiter.metrics.get("refused") == 1
    assert getattr(rl.rate_limiter, "refused_by_bucket", None) == {PUBLIC_FORWARDED: 1}
