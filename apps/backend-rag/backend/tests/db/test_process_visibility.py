"""Run the workspace's assignment policy against PostgreSQL and the actual HTTP routes."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.deps import process_access
from backend.app.routers import (
    crm_interactions,
    crm_practice_delivery,
    crm_practices,
    wa_mirror_messages,
)
from backend.services.crm.practice_delivery import practice

pytestmark = pytest.mark.asyncio


class Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest_asyncio.fixture
async def db(db_tx, monkeypatch):
    monkeypatch.setattr(
        process_access, "OWNER_EMAILS", {"owner@example.test", "owner-alias@example.test"}
    )
    monkeypatch.setenv(
        "PROCESS_VIEW_GRANTS_JSON",
        '{"book@example.test":"all","lead@example.test":"setup","head@example.test":"tax","former@example.test":"setup"}',
    )
    await db_tx.execute("""
        CREATE TEMP TABLE team_members(id int, email text, full_name text, name text, active bool, department text, role text, created_at timestamptz default now());
        INSERT INTO team_members VALUES
            (1,'lead@example.test','Lead',NULL,TRUE,'setup','direktur'),
            (2,'head@example.test','Head',NULL,TRUE,'tax','tax supervisor'),
            (3,'book@example.test','Book',NULL,TRUE,'accounting','accounting'),
            (4,'alpha@example.test','Alpha',NULL,TRUE,'setup','consultant'),
            (5,'beta@example.test','Beta',NULL,TRUE,'tax','tax consultant'),
            (6,'former@example.test','Former',NULL,FALSE,'setup','consultant'),
            (7,'service@example.test',NULL,NULL,TRUE,'tax','monitoring');
        CREATE TEMP TABLE clients(id int, full_name text, email text, phone text, assigned_to text);
        INSERT INTO clients VALUES (20,'Fixture','fixture@example.test',NULL,'outsider@example.test');
        CREATE TEMP TABLE practice_types(id int, name text, code text, category text, required_documents jsonb);
        INSERT INTO practice_types VALUES (30,'Fixture','fixture','tax','[]');
        CREATE TEMP TABLE client_family_members(id int, client_id int, full_name text, relationship text);
        CREATE TEMP TABLE practices(id int, client_id int default 20, practice_type_id int default 30,
            assigned_to text, created_by text default 'outsider@example.test', family_member_id int,
            status text default 'on_process', created_at timestamptz default NOW(), payment_status text,
            client_visible boolean, metadata jsonb default '{}', completion_date date, start_date date,
            quoted_price numeric, discount_amount numeric);
        INSERT INTO practices(id,assigned_to) VALUES (101,' Alpha@Example.Test '),(102,'beta@example.test'),
            (103,'lead@example.test'),(104,'head@example.test'),(105,NULL),(106,'former@example.test'),
            (107,'service@example.test'),(108,'outsider@example.test');
        CREATE TEMP TABLE activity_log(entity_type text,entity_id int,action text,changes jsonb,performed_at timestamptz);
        INSERT INTO activity_log SELECT 'practice', id, 'updated', '{"status":"on_process"}', NOW() FROM practices;
        CREATE TEMP TABLE practice_required_documents(id int, practice_id int, document_type text, document_label text,
            description text, is_required bool, uploaded_by_client bool, uploaded_file_id text, uploaded_at timestamptz,
            client_notes text, team_member_notes text, status text, created_at timestamptz, updated_at timestamptz);
    """)
    return Pool(db_tx)


def http(db, email, role="consultant", auth_method=None):
    app = FastAPI()
    for module in (crm_practices, crm_interactions, wa_mirror_messages, crm_practice_delivery):
        app.include_router(module.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "email": email,
        "role": role,
        "auth_method": auth_method,
    }
    app.dependency_overrides[get_database_pool] = lambda: db
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.parametrize(
    ("caller", "role", "ids"),
    [
        ("owner", "admin", range(101, 109)),
        ("owner-alias", "admin", range(101, 109)),
        ("book", "accounting", range(101, 109)),
        ("lead", "direktur", {101, 103}),
        ("head", "tax supervisor", {102, 104}),
        ("alpha", "consultant", {101}),
        ("beta", "tax consultant", {102}),
        ("outsider", "admin", {108}),
        ("former", "consultant", set()),
        ("service", "monitoring", set()),
        ("head", "client", set()),
    ],
)
async def test_list_scopes_before_pagination_and_projects_roster(db, caller, role, ids):
    async with http(db, caller + "@example.test", role) as client:
        response = await client.get("/api/crm/practices/", params={"include_history": "true"})
        assert response.status_code == 200, response.text
        rows = response.json()
        assert [r["id"] for r in rows] == sorted(ids, reverse=True)
        if 101 in ids:
            alpha = next(r for r in rows if r["id"] == 101)
            assert alpha["assignee_color_index"] == 3
            assert (
                alpha["assignee_member_id"],
                alpha["assignee_name"],
                alpha["assignee_department"],
            ) == ("4", "Alpha", "setup")
        if 102 in ids:
            assert next(r for r in rows if r["id"] == 102)["assignee_color_index"] == 4
            assert next(r for r in rows if r["id"] == 102)["assignee_department"] == "tax"
        page = await client.get("/api/crm/practices/", params={"limit": 1, "offset": 1})
        assert [r["id"] for r in page.json()] == sorted(ids, reverse=True)[1:2]


@pytest.mark.parametrize(
    "path",
    [
        "/api/crm/practices/102",
        "/api/crm/practices/102/required-documents",
        "/api/crm/practices/102/delivery",
        "/api/crm/interactions/practice/102/history",
        "/api/wa/messages?practice_id=102",
    ],
)
async def test_creator_and_client_lead_cannot_open_another_assignment(db, path):
    async with http(db, "outsider@example.test", "admin") as client:
        response = await client.get(path)
        assert response.status_code == 404, response.text


async def test_department_view_grants_read_only_and_reassignment_takes_effect(db, monkeypatch):
    monkeypatch.setattr(crm_practices, "smart_search_available", AsyncMock(return_value=False))
    async with http(db, "lead@example.test", "direktur") as client:
        detail = (await client.get("/api/crm/practices/101")).json()
        assert detail["id"] == 101
        assert detail["can_edit_process"] is False and detail["can_manage_delivery"] is False
        assert (await client.post("/api/crm/practices/101/regenerate-invoice")).status_code == 403
        assert (await client.get("/api/crm/practices/101/required-documents")).status_code == 200
        assert (
            await client.get("/api/crm/practices/?assigned_to=beta%40example.test&search=Fixture")
        ).json() == []
        async with db.acquire() as conn:
            # Neither a teammate leaving nor registered newcomers change an existing slot.
            await conn.execute("UPDATE team_members SET active=false WHERE id=2")
            await conn.execute(
                "INSERT INTO team_members(id,email,active,department,role,created_at) VALUES (8,'new@example.test',true,'tax','tax consultant',NOW()+interval '1 day')"
            )
            user = {"email": "lead@example.test", "role": "direktur"}
            assert (await practice(conn, 101, user, read_only=True))["id"] == 101
            with pytest.raises(HTTPException) as error:
                await practice(conn, 101, user, lock=True)
            assert error.value.status_code == 404
            await conn.execute("UPDATE team_members SET department='tax' WHERE id=4")
        assert (await client.get("/api/crm/practices/101")).status_code == 404
    async with http(db, "alpha@example.test") as client:
        results = (await client.get("/api/crm/practices/?search=Fixture")).json()
        assert [(r["id"], r["assignee_color_index"]) for r in results] == [(101, 3)]


async def test_machine_channel_does_not_inherit_staff_grant(db):
    async with http(db, "book@example.test", "admin", "admin_key") as client:
        assert (await client.get("/api/crm/practices/")).json() == []


async def test_update_cannot_use_creator_or_admin_role_to_modify_a_hidden_practice(db):
    async with http(db, "outsider@example.test", "admin") as client:
        result = await client.patch("/api/crm/practices/102", json={"priority": "urgent"})
        assert result.status_code == 404
