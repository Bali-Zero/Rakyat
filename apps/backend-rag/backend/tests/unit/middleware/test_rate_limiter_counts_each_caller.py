"""Who the rate limiter counts, against which threshold, and what a refusal says.

No Redis and no application here: the caller's name (`rate_limit_subject`), the
address the edge saw, the threshold table, the refusal counters and the CORS headers
of a 429. The counting itself is in `test_rate_limiter_window_script_redis.py`; the
order of the middleware and the two processes in
`tests/app/test_rate_limiter_in_the_two_process_stack.py`.

Invented values only: documentation addresses and example.test names.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request

from backend.app.setup.cors_config import get_allowed_origins
from backend.middleware import rate_limiter as rl
from backend.middleware.rate_limiter import RateLimiter, RateLimitMiddleware, get_rate_limit_stats

PEER = "10.0.0.9"
_UNSET = object()


def _request(
    path: str = "/api/crm/clients",
    *,
    method: str = "GET",
    headers: list[tuple[str, str]] | None = None,
    user: object = _UNSET,
    peer: str | None = PEER,
) -> Request:
    raw = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or [])]
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": raw,
        "client": (peer, 4321) if peer else None,
        "server": ("api.test", 80),
        "scheme": "http",
        "state": {},
    }
    request = Request(scope)
    if user is not _UNSET:
        request.state.user = user
    return request


def _subject(**kwargs: object) -> str:
    return rl.rate_limit_subject(_request(**kwargs))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Who is counted
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("method", ["jwt_header", "jwt_cookie", "jwt_stateless"])
def test_a_signed_in_user_is_counted_by_address_of_record(method: str) -> None:
    user = {"email": "  Person.One@Example.Test ", "auth_method": method}
    assert _subject(user=user) == "user:person.one@example.test"


def test_two_users_behind_one_address_are_two_callers() -> None:
    one = _subject(user={"email": "one@example.test", "auth_method": "jwt_header"})
    two = _subject(user={"email": "two@example.test", "auth_method": "jwt_header"})
    assert one != two


def test_one_user_on_two_addresses_is_one_caller() -> None:
    user = {"email": "one@example.test", "auth_method": "jwt_cookie"}
    here = _subject(user=user, peer="10.0.0.1", headers=[("Fly-Client-IP", "203.0.113.1")])
    there = _subject(user=user, peer="10.0.0.2", headers=[("Fly-Client-IP", "203.0.113.2")])
    assert here == there == "user:one@example.test"


def _api_key_user(role: str = "user") -> dict:
    # The shape `APIKeyAuth.validate_api_key` returns: the same e-mail for a whole role.
    return {"id": "api_key_stub-key", "email": f"{role}@zantara.dev", "auth_method": "api_key"}


def test_two_api_keys_of_one_role_are_two_callers() -> None:
    one = _subject(user=_api_key_user(), headers=[("X-API-Key", "stub-key-one")])
    two = _subject(user=_api_key_user(), headers=[("X-API-Key", "stub-key-two")])
    assert one.startswith("key:") and two.startswith("key:")
    assert one != two


def test_an_api_key_is_named_by_a_digest_of_the_whole_key() -> None:
    key = "stub-key-with-a-shared-first-part-A"
    twin = "stub-key-with-a-shared-first-part-B"  # same first eight characters
    subject = _subject(user=_api_key_user(), headers=[("X-API-Key", key)])
    name = subject.removeprefix("key:")

    assert len(name) == 32 and all(c in "0123456789abcdef" for c in name)
    assert key not in subject and "zantara.dev" not in subject
    assert subject == _subject(user=_api_key_user(), headers=[("X-API-Key", key)])
    assert subject != _subject(user=_api_key_user(), headers=[("X-API-Key", twin)])


def test_the_key_digest_is_keyed_not_a_bare_hash() -> None:
    import hashlib

    key = "stub-key-one"
    subject = _subject(user=_api_key_user(), headers=[("X-API-Key", key)])
    assert hashlib.sha256(key.encode()).hexdigest()[:32] not in subject


@pytest.mark.parametrize("method", ["internal_key", "admin_key"])
def test_a_shared_service_credential_is_one_shared_principal(method: str) -> None:
    user = {"email": "service@example.test", "auth_method": method}
    here = _subject(user=user, headers=[("Fly-Client-IP", "203.0.113.1")])
    there = _subject(user=user, headers=[("Fly-Client-IP", "203.0.113.2")])
    assert here == there == f"svc:{method}"


@pytest.mark.parametrize(
    "user",
    [
        None,
        "not-a-dict",
        {},
        {"email": "one@example.test"},  # no method: not an identity this limiter knows
        {"email": "one@example.test", "auth_method": "public"},
        {"email": "one@example.test", "auth_method": "something-new"},
        {"email": "", "auth_method": "jwt_header"},
        {"email": "   ", "auth_method": "jwt_header"},
        {"email": None, "auth_method": "jwt_stateless"},
        {"email": 7, "auth_method": "jwt_cookie"},
        {"auth_method": "api_key"},  # authenticated by key, but no key on the request
    ],
)
def test_an_identity_with_no_usable_field_falls_to_the_address(user: object) -> None:
    assert _subject(user=user, headers=[("Fly-Client-IP", "203.0.113.7")]) == "addr:203.0.113.7"


def test_a_public_route_is_counted_by_address_whatever_credentials_it_carries() -> None:
    # Authentication sets no user on a public route, with or without credentials.
    headers = [
        ("Fly-Client-IP", "203.0.113.7"),
        ("Authorization", "Bearer stub-token"),
        ("X-API-Key", "stub-key-one"),
        ("X-CRM-Write-Key", "stub-write-key"),
    ]
    assert _subject(headers=headers) == "addr:203.0.113.7"


def test_no_two_strangers_share_a_constant_subject() -> None:
    one = _subject(headers=[("Fly-Client-IP", "203.0.113.1")])
    two = _subject(headers=[("Fly-Client-IP", "203.0.113.2")])
    assert one != two
    assert "None" not in one and "None" not in two


# --------------------------------------------------------------------------- #
# The address the edge saw
# --------------------------------------------------------------------------- #
def test_the_edge_header_names_the_caller() -> None:
    assert _subject(headers=[("Fly-Client-IP", " 203.0.113.9 ")]) == "addr:203.0.113.9"


def test_one_address_in_two_spellings_is_one_caller() -> None:
    long_form = _subject(headers=[("Fly-Client-IP", "2001:DB8:0:0:0:0:0:1")])
    assert long_form == _subject(headers=[("Fly-Client-IP", "2001:db8::1")]) == "addr:2001:db8::1"


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("Fly-Client-IP", "")],
        [("Fly-Client-IP", "   ")],
        [("Fly-Client-IP", "not-an-address")],
        [("Fly-Client-IP", "203.0.113.9:443")],
        [("Fly-Client-IP", "203.0.113.9, 203.0.113.10")],
        [("Fly-Client-IP", "203.0.113.9"), ("Fly-Client-IP", "203.0.113.10")],
        [("Fly-Client-IP", "203.0.113.9"), ("fly-client-ip", "203.0.113.9")],
    ],
)
def test_an_absent_empty_malformed_or_repeated_edge_header_falls_to_the_peer(
    headers: list[tuple[str, str]],
) -> None:
    assert _subject(headers=headers) == f"addr:{PEER}"


@pytest.mark.parametrize("forged", ["X-Forwarded-For", "X-Real-IP", "Forwarded"])
def test_a_header_a_client_can_write_changes_nothing(forged: str) -> None:
    assert _subject(headers=[(forged, "198.51.100.66")]) == f"addr:{PEER}"
    with_edge = [("Fly-Client-IP", "203.0.113.9"), (forged, "198.51.100.66")]
    assert _subject(headers=with_edge) == "addr:203.0.113.9"


def test_a_request_with_no_peer_and_no_edge_header_still_has_a_subject() -> None:
    assert _subject(peer=None) == "addr:unknown"


# --------------------------------------------------------------------------- #
# Thresholds
# --------------------------------------------------------------------------- #
@pytest.fixture
def middleware() -> RateLimitMiddleware:
    return RateLimitMiddleware(app=MagicMock())


def test_the_phone_upsert_has_a_threshold_of_its_own(middleware: RateLimitMiddleware) -> None:
    assert middleware._get_rate_limit("/api/crm/clients/upsert-by-phone") == (600, 60)


@pytest.mark.parametrize("suffix", ["/", "/x", "-again", "?"])
def test_that_threshold_never_applies_to_a_longer_path(
    middleware: RateLimitMiddleware, suffix: str
) -> None:
    longer = "/api/crm/clients/upsert-by-phone" + suffix
    assert middleware._get_rate_limit(longer) == (100, 60)  # the CRM clients threshold


#: What every family of the table selected before this change, generated from that code:
#: the entry itself, a trailing slash, a longer path, a sibling. None of them may move.
THRESHOLDS_BEFORE = [
    ("/api/agents/journey/create", (10, 3600)),
    ("/api/agents/journey/create/", (10, 3600)),
    ("/api/agents/journey/create/x", (10, 3600)),
    ("/api/agents/journey/create-sibling", (10, 3600)),
    ("/api/agents/compliance/track", (20, 3600)),
    ("/api/agents/compliance/track/", (20, 3600)),
    ("/api/agents/compliance/track/x", (20, 3600)),
    ("/api/agents/compliance/track-sibling", (20, 3600)),
    ("/api/agents/ingestion/run", (5, 3600)),
    ("/api/agents/ingestion/run/", (5, 3600)),
    ("/api/agents/ingestion/run/x", (5, 3600)),
    ("/api/agents/ingestion/run-sibling", (5, 3600)),
    ("/api/crm/clients", (100, 60)),
    ("/api/crm/clients/", (100, 60)),
    ("/api/crm/clients/x", (100, 60)),
    ("/api/crm/clients-sibling", (100, 60)),
    ("/api/crm/practices", (100, 60)),
    ("/api/crm/practices/", (100, 60)),
    ("/api/crm/practices/x", (100, 60)),
    ("/api/crm/practices-sibling", (100, 60)),
    ("/api/crm/interactions", (150, 60)),
    ("/api/crm/interactions/", (150, 60)),
    ("/api/crm/interactions/x", (150, 60)),
    ("/api/crm/interactions-sibling", (150, 60)),
    ("/api/crm/shared-memory", (60, 60)),
    ("/api/crm/shared-memory/", (60, 60)),
    ("/api/crm/shared-memory/x", (60, 60)),
    ("/api/crm/shared-memory-sibling", (60, 60)),
    ("/api/crm/clients/stats", (30, 60)),
    ("/api/crm/clients/stats/", (100, 60)),
    ("/api/crm/clients/stats/x", (100, 60)),
    ("/api/crm/clients/stats-sibling", (100, 60)),
    ("/api/crm/practices/stats", (30, 60)),
    ("/api/crm/practices/stats/", (100, 60)),
    ("/api/crm/practices/stats/x", (100, 60)),
    ("/api/crm/practices/stats-sibling", (100, 60)),
    ("/api/crm/interactions/stats", (30, 60)),
    ("/api/crm/interactions/stats/", (150, 60)),
    ("/api/crm/interactions/stats/x", (150, 60)),
    ("/api/crm/interactions/stats-sibling", (150, 60)),
    ("/api/crm/shared-memory/team-overview", (20, 60)),
    ("/api/crm/shared-memory/team-overview/", (60, 60)),
    ("/api/crm/shared-memory/team-overview/x", (60, 60)),
    ("/api/crm/shared-memory/team-overview-sibling", (60, 60)),
    ("/api/crm/shared-memory/upcoming-renewals", (20, 60)),
    ("/api/crm/shared-memory/upcoming-renewals/", (60, 60)),
    ("/api/crm/shared-memory/upcoming-renewals/x", (60, 60)),
    ("/api/crm/shared-memory/upcoming-renewals-sibling", (60, 60)),
    ("/api/agents/journey/", (60, 60)),
    ("/api/agents/journey/x", (60, 60)),
    ("/api/agents/journey", (100, 60)),
    ("/api/agents/compliance/", (60, 60)),
    ("/api/agents/compliance/x", (60, 60)),
    ("/api/agents/compliance", (100, 60)),
    ("/api/agents/", (100, 60)),
    ("/api/agents/x", (100, 60)),
    ("/api/agents", (120, 60)),
    ("/bali-zero/chat", (30, 60)),
    ("/bali-zero/chat/", (30, 60)),
    ("/bali-zero/chat/x", (30, 60)),
    ("/bali-zero/chat-sibling", (30, 60)),
    ("/search", (60, 60)),
    ("/search/", (60, 60)),
    ("/search/x", (60, 60)),
    ("/search-sibling", (60, 60)),
    ("/api/", (120, 60)),
    ("/api/x", (120, 60)),
    ("/api", (200, 60)),
    ("/rerank", (100, 60)),
    ("/rerank/", (100, 60)),
    ("/rerank/x", (100, 60)),
    ("/rerank-sibling", (100, 60)),
    ("/api/intel/scraper/submit", (10, 60)),
    ("/api/intel/scraper/submit/", (120, 60)),
    ("/api/intel/scraper/submit/x", (120, 60)),
    ("/api/intel/scraper/submit-sibling", (120, 60)),
    ("/api/intel/staging/approve/", (20, 60)),
    ("/api/intel/staging/approve/x", (120, 60)),
    ("/api/intel/staging/approve", (120, 60)),
    ("/api/audio/", (30, 60)),
    ("/api/audio/x", (120, 60)),
    ("/api/audio", (120, 60)),
    ("/api/whatsapp/send", (30, 60)),
    ("/api/whatsapp/send/", (120, 60)),
    ("/api/whatsapp/send/x", (120, 60)),
    ("/api/whatsapp/send-sibling", (120, 60)),
    ("/api/knowledge/visa", (100, 60)),
    ("/api/knowledge/visa/", (120, 60)),
    ("/api/knowledge/visa/x", (120, 60)),
    ("/api/knowledge/visa-sibling", (120, 60)),
    ("/api/visa-oracle/evaluate", (30, 60)),
    ("/api/visa-oracle/evaluate/", (120, 60)),
    ("/api/visa-oracle/evaluate/x", (120, 60)),
    ("/api/visa-oracle/evaluate-sibling", (120, 60)),
    ("/api/public/tax-calendar/obligations", (30, 60)),
    ("/api/public/tax-calendar/obligations/", (120, 60)),
    ("/api/public/tax-calendar/obligations/x", (120, 60)),
    ("/api/public/tax-calendar/obligations-sibling", (120, 60)),
    ("/api/legal/parent-documents", (20, 60)),
    ("/api/legal/parent-documents/", (120, 60)),
    ("/api/legal/parent-documents/x", (120, 60)),
    ("/api/legal/parent-documents-sibling", (120, 60)),
    ("/totally/unknown", (200, 60)),
    ("/apix", (200, 60)),
]


@pytest.mark.parametrize(("path", "expected"), THRESHOLDS_BEFORE)
def test_no_other_path_changes_its_threshold(
    middleware: RateLimitMiddleware, path: str, expected: tuple[int, int]
) -> None:
    assert middleware._get_rate_limit(path) == expected


@pytest.mark.parametrize(
    ("path", "bucket"),
    [
        ("/api/crm/clients/upsert-by-phone", "/api/crm/clients/upsert-by-phone"),
        ("/api/crm/clients/123", "/api/crm/clients"),
        ("/api/visa-oracle/evaluate", "/api/visa-oracle/evaluate"),
        ("/api/anything-else", "/api/"),
        ("/totally/unknown", "*"),
    ],
)
def test_a_refusal_is_named_after_the_table_entry_that_matched(
    middleware: RateLimitMiddleware, path: str, bucket: str
) -> None:
    assert middleware._match_rate_limit(path)[0] == bucket


# --------------------------------------------------------------------------- #
# A refusal: counted, with headers a browser of ours can read
# --------------------------------------------------------------------------- #
def _refusing_limiter(info: dict | None = None) -> MagicMock:
    limiter = MagicMock()
    limiter.is_allowed = MagicMock(
        return_value=(False, info or {"limit": 5, "remaining": 0, "reset": 1111})
    )
    return limiter


CORS_HEADERS = (
    "access-control-allow-origin",
    "access-control-allow-credentials",
    "access-control-expose-headers",
    "vary",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", get_allowed_origins())
async def test_a_429_carries_cors_headers_for_every_origin_of_the_allowlist(
    middleware: RateLimitMiddleware, monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    monkeypatch.setattr(rl, "rate_limiter", _refusing_limiter())

    response = await middleware.dispatch(_request(headers=[("Origin", origin)]), AsyncMock())

    assert response.status_code == 429
    assert response.headers.get("access-control-allow-origin") == origin
    assert response.headers.get("access-control-allow-credentials") == "true"
    assert response.headers.get("vary") == "Origin"
    exposed = response.headers.get("access-control-expose-headers", "")
    for name in ("Retry-After", "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset"):
        assert name in exposed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("Origin", "https://attacker.example.test")],
        [("Origin", "https://balizero.com.example.test")],
        [("Origin", "null")],
    ],
)
async def test_a_429_carries_no_cors_header_for_any_other_origin(
    middleware: RateLimitMiddleware,
    monkeypatch: pytest.MonkeyPatch,
    headers: list[tuple[str, str]],
) -> None:
    monkeypatch.setattr(rl, "rate_limiter", _refusing_limiter())

    response = await middleware.dispatch(_request(headers=headers), AsyncMock())

    assert response.status_code == 429
    for name in CORS_HEADERS:
        assert name not in response.headers


@pytest.mark.asyncio
async def test_the_garuda_refusal_keeps_its_contract_body_and_privacy_headers(
    middleware: RateLimitMiddleware, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    monkeypatch.setattr(rl, "rate_limiter", _refusing_limiter())
    origin = get_allowed_origins()[0]

    response = await middleware.dispatch(
        _request("/api/visa/voa/orders", method="POST", headers=[("Origin", origin)]), AsyncMock()
    )

    assert response.status_code == 429
    assert json.loads(response.body) == RateLimitMiddleware.GARUDA_RATE_LIMITED_BODY
    for name, value in RateLimitMiddleware.GARUDA_PRIVACY_HEADERS.items():
        assert response.headers[name] == value
    assert response.headers["access-control-allow-origin"] == origin


@pytest.mark.asyncio
async def test_retry_after_is_what_the_counter_says_when_it_knows(
    middleware: RateLimitMiddleware, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = {"limit": 5, "remaining": 0, "reset": 1111, "retry_after": 7}
    monkeypatch.setattr(rl, "rate_limiter", _refusing_limiter(info))

    response = await middleware.dispatch(_request("/api/agents/journey/create"), AsyncMock())

    assert response.headers["Retry-After"] == "7"  # not the 3600 of this path's window


@pytest.mark.asyncio
async def test_a_preflight_is_never_counted(
    middleware: RateLimitMiddleware, monkeypatch: pytest.MonkeyPatch
) -> None:
    limiter = _refusing_limiter()
    monkeypatch.setattr(rl, "rate_limiter", limiter)
    answer = MagicMock(headers={})

    response = await middleware.dispatch(
        _request(method="OPTIONS", headers=[("Origin", "https://kita.balizero.com")]),
        AsyncMock(return_value=answer),
    )

    assert response is answer
    limiter.is_allowed.assert_not_called()


def _memory_limiter() -> RateLimiter:
    with patch("backend.core.redis_manager.RedisManager") as manager_class:
        manager = MagicMock()
        manager.get_sync_client.return_value = None
        manager_class.get_instance.return_value = manager
        return RateLimiter()


@pytest.mark.asyncio
async def test_refusals_are_counted_in_total_and_by_threshold(
    middleware: RateLimitMiddleware, monkeypatch: pytest.MonkeyPatch
) -> None:
    limiter = _memory_limiter()
    monkeypatch.setattr(rl, "rate_limiter", limiter)
    monkeypatch.setattr(rl, "_rate_limit_storage", {})
    monkeypatch.setitem(RateLimitMiddleware.RATE_LIMITS, "/api/crm/clients", (2, 60))
    caller = [("Fly-Client-IP", "203.0.113.50")]
    passed = MagicMock(headers={}, status_code=200)

    statuses = []
    for path in ["/api/crm/clients/1"] * 5 + ["/totally/unknown"]:
        response = await middleware.dispatch(
            _request(path, headers=caller), AsyncMock(return_value=passed)
        )
        statuses.append(response.status_code)

    # Each concrete path is its own counter: five calls to one path, limit two.
    assert statuses == [200, 200, 429, 429, 429, 200]
    assert limiter.metrics["refused"] == 3
    assert limiter.refused_by_bucket == {"/api/crm/clients": 3}
    stats = get_rate_limit_stats()
    assert stats["metrics"]["refused"] == 3
    assert stats["refused_by_bucket"] == {"/api/crm/clients": 3}


def test_the_stats_always_carry_the_refusal_counters() -> None:
    stats = get_rate_limit_stats()
    assert isinstance(stats["metrics"]["refused"], int)
    assert isinstance(stats["refused_by_bucket"], dict)


# --------------------------------------------------------------------------- #
# The fallbacks keep their behaviour
# --------------------------------------------------------------------------- #
def test_without_redis_the_memory_counter_keeps_the_full_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rl, "_rate_limit_storage", {})
    limiter = _memory_limiter()

    results = [limiter.is_allowed("k-memory", limit=3, window=60, now=5000.4) for _ in range(4)]

    assert [allowed for allowed, _ in results] == [True, True, True, False]
    refused = results[-1][1]
    assert refused["backend"] == "memory"
    assert refused["limit"] == 3
    assert refused["reset"] == 5060  # now + window, as before
    assert "retry_after" not in refused


def test_a_failing_redis_call_keeps_the_half_limit_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rl, "_rate_limit_storage", {})
    limiter = _memory_limiter()
    limiter.redis_available = True
    limiter.redis_client = MagicMock()
    limiter.redis_client.register_script.return_value.side_effect = ConnectionError("lost")

    results = [limiter.is_allowed("k-degraded", limit=6, window=60, now=5000.4) for _ in range(4)]

    assert [allowed for allowed, _ in results] == [True, True, True, False]
    refused = results[-1][1]
    assert refused["backend"] == "memory_degraded"
    assert refused["limit"] == 3
    assert refused["reset"] == 5060
    assert "retry_after" not in refused
    assert limiter.metrics["redis_errors"] == 4


@pytest.mark.asyncio
async def test_on_the_memory_counter_retry_after_stays_the_window(
    middleware: RateLimitMiddleware, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rl, "rate_limiter", _memory_limiter())
    monkeypatch.setattr(rl, "_rate_limit_storage", {})
    monkeypatch.setitem(RateLimitMiddleware.RATE_LIMITS, "/api/crm/clients", (1, 60))
    passed = MagicMock(headers={})

    await middleware.dispatch(_request(), AsyncMock(return_value=passed))
    response = await middleware.dispatch(_request(), AsyncMock(return_value=passed))

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"


def test_the_stats_count_every_rule_the_exact_ones_included() -> None:
    prefix_rules = set(RateLimitMiddleware.RATE_LIMITS)
    exact_rules = set(RateLimitMiddleware.EXACT_RATE_LIMITS)

    assert exact_rules - prefix_rules, "an exact-only rule exists"
    assert get_rate_limit_stats()["rate_limits_configured"] == len(prefix_rules | exact_rules)
    assert get_rate_limit_stats()["rate_limits_configured"] > len(prefix_rules)
