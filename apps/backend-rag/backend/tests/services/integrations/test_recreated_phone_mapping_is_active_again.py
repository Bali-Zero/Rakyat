from __future__ import annotations

import os
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import asyncpg
import pytest
import pytest_asyncio

from backend.services.integrations.messaging_identity_service import MessagingIdentityService


class _RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, statement: str, *args: Any) -> None:
        self.statements.append(statement)


class _Acquire:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    async def __aenter__(self) -> Any:
        return self.connection

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


class _SingleConnectionPool:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


@pytest.mark.asyncio
async def test_create_mapping_conflict_reactivates_phone_mapping() -> None:
    connection = _RecordingConnection()
    service = MessagingIdentityService(_SingleConnectionPool(connection))

    assert await service.create_mapping(
        user_id=str(uuid.uuid4()),
        channel="whatsapp",
        phone="6281000000001",
        display_name="Person One",
        verified=True,
    )

    statement = re.sub(r"\s+", " ", connection.statements[0]).lower()
    conflict_branch = statement.split("do update set", maxsplit=1)[1]
    assert "active = true" in conflict_branch


class TestRecreatedPhoneMappingPostgres:
    _DB_URL = os.environ.get("TEST_DATABASE_URL")

    def _is_safe_test_database(url: str | None) -> bool:
        if not url:
            return False
        parsed = urlparse(url)
        name = re.sub(r"_gw\d+$", "", parsed.path.lstrip("/"))
        return parsed.hostname in {"localhost", "127.0.0.1"} and (
            name == "test" or name.endswith("_test")
        )

    pytestmark = pytest.mark.skipif(
        not _is_safe_test_database(_DB_URL), reason="needs a local test database"
    )

    @pytest_asyncio.fixture
    async def messaging_service(self) -> AsyncIterator[MessagingIdentityService]:
        connection = await asyncpg.connect(self._DB_URL)
        schema = f"mapping_reactivation_{uuid.uuid4().hex}"
        try:
            await connection.execute(f'CREATE SCHEMA "{schema}"')
            await connection.execute(f'SET search_path TO "{schema}"')
            await connection.execute(
                """
                CREATE TABLE messaging_users (
                    user_id UUID NOT NULL,
                    channel TEXT NOT NULL,
                    phone TEXT,
                    telegram_chat_id BIGINT,
                    display_name TEXT,
                    verified BOOLEAN DEFAULT FALSE,
                    active BOOLEAN DEFAULT TRUE,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    last_message_at TIMESTAMP WITH TIME ZONE
                )
                """
            )
            await connection.execute(
                """
                CREATE UNIQUE INDEX messaging_users_phone_unique
                ON messaging_users(phone) WHERE phone IS NOT NULL
                """
            )
            yield MessagingIdentityService(_SingleConnectionPool(connection))
        finally:
            try:
                await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            finally:
                await connection.close()

    @pytest.mark.asyncio
    async def test_recreating_deactivated_mapping_makes_phone_resolvable(
        self, messaging_service: MessagingIdentityService
    ) -> None:
        user_id = str(uuid.uuid4())
        phone = "6281000000001"

        assert await messaging_service.create_mapping(
            user_id=user_id,
            channel="whatsapp",
            phone=phone,
            display_name="Person One",
            verified=True,
        )
        assert await messaging_service.deactivate_mapping(phone=phone)
        assert await messaging_service.create_mapping(
            user_id=user_id,
            channel="whatsapp",
            phone=phone,
            display_name="Person One",
            verified=True,
        )

        mapping = await messaging_service.get_user_by_phone(phone)
        assert mapping is not None
        # The column is a UUID: the driver hands back a UUID object.
        assert str(mapping["user_id"]) == user_id

    @pytest.mark.asyncio
    async def test_creating_new_or_active_mapping_keeps_existing_behavior(
        self, messaging_service: MessagingIdentityService
    ) -> None:
        user_id = str(uuid.uuid4())
        phone = "6281000000002"

        assert await messaging_service.create_mapping(
            user_id=user_id,
            channel="whatsapp",
            phone=phone,
            display_name="Person One",
            verified=False,
        )
        assert await messaging_service.create_mapping(
            user_id=user_id,
            channel="whatsapp",
            phone=phone,
            display_name="Person Two",
            verified=True,
        )

        mapping = await messaging_service.get_user_by_phone(phone)
        assert mapping is not None
        assert mapping["display_name"] == "Person Two"
        assert mapping["verified"] is True
