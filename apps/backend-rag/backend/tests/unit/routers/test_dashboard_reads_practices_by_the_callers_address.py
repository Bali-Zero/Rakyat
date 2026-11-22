"""Dashboard practice reads use the caller's normalized email address."""

from unittest.mock import AsyncMock, patch

import pytest

USER_ID = "5f0c1a52-0000-4000-8000-000000000001"
USER = {
    "email": "Person.One@Example.Test",
    "user_id": USER_ID,
    "role": "consultant",
    "permissions": [],
}


async def _dashboard_call(user):
    with (
        patch(
            "backend.app.routers.dashboard_summary.get_practices_stats",
            new_callable=AsyncMock,
            return_value={},
        ) as practice_stats,
        patch(
            "backend.app.routers.dashboard_summary.get_interactions_stats",
            new_callable=AsyncMock,
            return_value={},
        ) as interaction_stats,
        patch(
            "backend.app.routers.dashboard_summary.list_practices",
            new_callable=AsyncMock,
            return_value=[],
        ) as practices,
        patch(
            "backend.app.routers.dashboard_summary.list_interactions",
            new_callable=AsyncMock,
            return_value=[],
        ) as interactions,
        patch(
            "backend.app.routers.dashboard_summary._get_email_stats",
            new_callable=AsyncMock,
            return_value={},
        ) as email_stats,
        patch(
            "backend.app.routers.dashboard_summary._get_critical_deadlines",
            new_callable=AsyncMock,
            return_value=0,
        ) as deadlines,
        patch(
            "backend.app.routers.dashboard_summary._get_revenue_stats",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "backend.app.routers.dashboard_summary._calculate_revenue_growth",
            new_callable=AsyncMock,
            return_value=0,
        ),
        patch(
            "backend.app.routers.dashboard_summary._get_total_clients",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        from backend.app.routers.dashboard_summary import get_dashboard_summary

        await get_dashboard_summary(current_user=user, db_pool=object())

    return {
        "practice_stats": practice_stats,
        "interaction_stats": interaction_stats,
        "practices": practices,
        "interactions": interactions,
        "email_stats": email_stats,
        "deadlines": deadlines,
    }


@pytest.mark.asyncio
async def test_practice_reads_use_normalized_caller_email_as_owner_key():
    calls = await _dashboard_call(USER)

    assert calls["practice_stats"].call_args.kwargs["user_id"] == "person.one@example.test"
    assert calls["practices"].call_args.kwargs["assigned_to"] == "person.one@example.test"
    assert calls["deadlines"].call_args.args[1] == "person.one@example.test"


@pytest.mark.asyncio
async def test_practice_full_view_uses_practice_visibility_rule():
    with patch(
        "backend.app.routers.dashboard_summary.can_view_all_practices",
        return_value=True,
        create=True,
    ):
        calls = await _dashboard_call({**USER, "role": "accounting"})

    assert calls["practice_stats"].call_args.kwargs["is_admin"] is True
    assert calls["practices"].call_args.kwargs["assigned_to"] is None
    assert calls["deadlines"].call_args.args[2] is True


@pytest.mark.asyncio
async def test_non_practice_reads_keep_member_id_and_existing_fallbacks():
    calls = await _dashboard_call({"user_id": USER_ID, "role": "consultant"})

    assert calls["practice_stats"].call_args.kwargs["user_id"] == USER_ID
    assert calls["practices"].call_args.kwargs["user_id"] == USER_ID
    assert calls["interaction_stats"].call_args.args[0] == USER_ID
    assert calls["interactions"].call_args.kwargs["user_id"] == USER_ID
    assert calls["email_stats"].call_args.args[1] == USER_ID
    assert calls["deadlines"].call_args.args[1] == USER_ID


@pytest.mark.asyncio
async def test_admin_keeps_full_view_of_practice_reads():
    calls = await _dashboard_call({**USER, "role": "admin"})

    assert calls["practice_stats"].call_args.kwargs["is_admin"] is True
    assert calls["practices"].call_args.kwargs["assigned_to"] is None
    assert calls["deadlines"].call_args.args[2] is True
