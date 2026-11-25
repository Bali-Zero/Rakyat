import asyncio
import os

os.environ.setdefault("DATABASE_URL", "postgresql://example.test/synthetic")
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_ID_RCLONE", "synthetic-client-id")
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_SECRET_RCLONE", "synthetic-client-secret")
os.environ.setdefault("GOOGLE_OAUTH_REFRESH_TOKEN", "synthetic-refresh-token")

from scripts import bulk_populate_clients


class FakeConnection:
    def __init__(self, exact_company_id):
        self.exact_company_id = exact_company_id
        self.statements = []
        self.count_arguments = None

    async def fetchval(self, statement, *arguments):
        self.statements.append(statement)
        if "JOIN client_company_links" in statement:
            return None
        if "LOWER(TRIM(company_name))" in statement:
            return self.exact_company_id
        if "COUNT(*) FROM companies" in statement:
            self.count_arguments = arguments
            return 3
        if "company_name ILIKE" in statement and "LIMIT 1" in statement:
            return 900001
        if "FROM client_company_links" in statement:
            return None
        raise AssertionError(f"Unexpected statement: {statement}")

    async def execute(self, statement, *arguments):
        self.statements.append(statement)


def process_company(monkeypatch, connection, dry_run=False):
    ingested = []

    def fake_list_folder(service, folder_id):
        assert folder_id == "folder-900001"
        return [
            {
                "id": "company-folder-900001",
                "mimeType": bulk_populate_clients.FOLDER_MIME,
                "name": "PT Contoh Satu",
            }
        ]

    async def fake_ingest_company_docs(*arguments):
        ingested.append(arguments)
        return 1

    monkeypatch.setattr(bulk_populate_clients, "list_folder", fake_list_folder)
    monkeypatch.setattr(bulk_populate_clients, "ingest_company_docs", fake_ingest_company_docs)

    result = {"docs": 0}
    processed = asyncio.run(
        bulk_populate_clients.process_company(
            connection,
            object(),
            {"id": 900001, "full_name": "Contoh Satu"},
            {"id": "folder-900001"},
            dry_run,
            result,
        )
    )
    return processed, ingested, result


def test_process_company_does_not_link_or_ingest_substring_match(monkeypatch):
    connection = FakeConnection(exact_company_id=None)

    processed, ingested, result = process_company(monkeypatch, connection)

    assert processed is False
    assert not any(
        "INSERT INTO client_company_links" in statement for statement in connection.statements
    )
    assert not ingested
    assert result == {"docs": 0}
    assert any("COUNT(*) FROM companies" in statement for statement in connection.statements)
    assert connection.count_arguments == ("%Contoh%",)


def test_process_company_links_and_ingests_exact_match(monkeypatch):
    connection = FakeConnection(exact_company_id=900001)

    processed, ingested, result = process_company(monkeypatch, connection)

    assert processed is True
    assert any(
        "INSERT INTO client_company_links" in statement for statement in connection.statements
    )
    assert len(ingested) == 1
    assert result == {"docs": 1}


def test_process_company_exact_match_dry_run_does_not_insert(monkeypatch):
    connection = FakeConnection(exact_company_id=900001)

    processed, ingested, result = process_company(monkeypatch, connection, dry_run=True)

    assert processed is True
    assert not any(
        "INSERT INTO client_company_links" in statement for statement in connection.statements
    )
    assert len(ingested) == 1
    assert result == {"docs": 1}
