import copy
import json

import pytest

from backend.services.crm.drive_taxonomy_repair import (
    _REQUIRED,
    RepairError,
    inspect_files,
    inventory_root,
    reconcile,
)


class Request:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class Files:
    def __init__(self, root, pages):
        self.root, self.pages = root, pages
        self.queries = []

    def get(self, **kwargs):
        return Request(self.root)

    def list(self, **kwargs):
        self.queries.append(kwargs["q"])
        return Request(self.pages.get(kwargs.get("pageToken"), {}))


class Drive:
    def __init__(self, root, pages):
        self.api = Files(root, pages)

    def files(self):
        return self.api


FOLDER = "application/vnd.google-apps.folder"
NAMES = ("01_Profile", "02_Immigration", "03_Company_and_Tax", "04_Archive")


def drive(children, root=None, next_token=None):
    root = root or {
        "id": "root",
        "name": "Sensitive Person",
        "mimeType": FOLDER,
        "parents": ["parent"],
        "trashed": False,
    }
    return Drive(root, {None: {"files": children, "nextPageToken": next_token}})


def children():
    return [
        {"id": str(i), "name": n, "mimeType": FOLDER, "parents": ["root"], "trashed": False}
        for i, n in enumerate(NAMES)
    ]


def test_complete_inventory():
    d = drive(children())
    result = inventory_root(d, "root", "parent", "individual")
    assert result["reason"] == "ready"
    assert len(result["destinations"]) == 4
    assert d.api.queries == ["'root' in parents and trashed = false"]


def test_reject_duplicate_and_shortcut():
    items = children() + [
        {
            "id": "x",
            "name": NAMES[0],
            "mimeType": "application/vnd.google-apps.shortcut",
            "parents": ["root"],
            "trashed": False,
        }
    ]
    assert (
        inventory_root(drive(items), "root", "parent", "individual")["reason"]
        == "ambiguous_canonical"
    )


def test_wrong_parent_and_pagination_failure():
    assert (
        inventory_root(drive(children()), "root", "other", "individual")["reason"] == "invalid_root"
    )
    with pytest.raises(Exception):
        inventory_root(drive(children(), next_token="bad"), "root", "parent", "individual")


class Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, typ, value, tb):
        return False


class Transaction(Context):
    async def __aenter__(self):
        self.snapshot = copy.deepcopy(self.value.maps)
        return self.value

    async def __aexit__(self, typ, value, tb):
        if typ:
            self.value.maps = self.snapshot
            self.value.rollbacks += 1
        return False


class DB:
    def __init__(self):
        self.entities = [1]
        self.secondary = []
        self.maps = {}
        self.kind = "individual"
        self.extra_reverse = []
        self.writes = []
        self.statements = []
        self.queries = []
        self.explains = []
        self.fail_explain = False
        self.write_entities = []
        self.rollbacks = 0
        self.binding_reads = 0
        self.change_binding_after = None
        self.docs, self.company_docs = [], []

    def acquire(self):
        return Context(self)

    def transaction(self, **kwargs):
        assert kwargs["isolation"] == "read_committed"
        return Transaction(self)

    async def fetch(self, sql, *args):
        self.queries.append(sql)
        if sql.startswith("EXPLAIN "):
            self.explains.append(sql)
            if self.fail_explain:
                raise RuntimeError("secret-schema-details")
            return [{"QUERY PLAN": "planned"}]
        if "information_schema.columns" in sql:
            tables = {
                **_REQUIRED,
                "documents": {"client_id", "file_id", "document_type", "document_category"},
                "company_documents": {"company_id", "google_drive_file_id", "document_type"},
            }
            return [{"table_name": t, "column_name": c} for t in args[0] for c in tables[t]]
        if "FROM documents d" in sql:
            return self.docs
        if "FROM company_documents d" in sql:
            return self.company_docs
        if "tax_dept_folder_id" in sql:
            return [{"id": i} for i in self.secondary]
        if "FROM clients WHERE id = ANY" in sql or "FROM companies WHERE id = ANY" in sql:
            return [{"id": i} for i in args[0] if i in self.entities]
        if "FROM clients WHERE" in sql or "FROM companies WHERE" in sql:
            self.binding_reads += 1
            if self.change_binding_after and self.binding_reads >= self.change_binding_after:
                return [{"id": 999}]
            return [{"id": i} for i in self.entities]
        if "WHERE subfolder_id = ANY" in sql or "WHERE drive_folder_id = ANY" in sql:
            kind = "individual" if "WHERE subfolder_id = ANY" in sql else "company"
            rows = [
                {"entity_id": entity, "name": name, "folder_id": folder}
                for (entity, name), folder in self.maps.items()
                if folder in args[0] and kind == self.kind
            ]
            rows.extend(
                {k: row[k] for k in ("entity_id", "name", "folder_id")}
                for row in self.extra_reverse
                if row["kind"] == kind and row["folder_id"] in args[0]
            )
            return sorted(rows, key=lambda r: (r["entity_id"], r["name"]))
        if "FROM client_drive_subfolders" in sql or "FROM company_drive_subfolders" in sql:
            return [
                {"entity_id": entity, "name": name, "folder_id": folder}
                for (entity, name), folder in sorted(self.maps.items())
                if entity in args[0]
            ]
        raise AssertionError("unexpected query")

    async def execute(self, sql, *args):
        self.statements.append(sql)
        if (
            "pg_advisory_xact_lock" in sql
            or sql.startswith("LOCK TABLE")
            or sql.startswith("SET LOCAL")
        ):
            return
        assert sql.startswith("INSERT INTO ")
        self.writes.append(sql)
        self.write_entities.append(args[0])
        self.maps[(args[0], args[1])] = args[2]

    async def fetchval(self, sql, *args):
        key = (args[0], args[1])
        if key in self.maps and self.maps[key] != args[3]:
            return None
        # Model global UNIQUE(subfolder_id) in the client mapping table.
        if self.kind == "individual" and any(
            k != key and v == args[2] for k, v in self.maps.items()
        ):
            raise RuntimeError("unique_violation")
        await self.execute(sql, *args)
        return 1


@pytest.mark.asyncio
async def test_plan_apply_idempotence_and_legacy_retained():
    db = DB()
    db.entities = [1]
    db.secondary = [77]
    db.maps[(1, "Sensitive Legacy Label")] = "legacy"
    d = drive(children())
    plan = await reconcile(db, d, ["root"], "parent", "individual")
    assert not db.writes
    assert len(db.explains) == 1
    assert db.explains[0].startswith("EXPLAIN INSERT INTO client_drive_subfolders")
    assert "ANALYZE" not in db.explains[0]
    assert plan["roots"][0]["entity_ids"] == [1]
    assert plan["roots"][0]["tax_secondary_ids"] == [77]
    assert not plan["roots"][0]["shared_root"]
    assert "Sensitive" not in json.dumps(plan)
    await reconcile(db, d, ["root"], "parent", "individual", plan["plan_sha256"])
    assert len(db.maps) == 5
    assert db.maps[(1, "Sensitive Legacy Label")] == "legacy"
    assert all("client_id, subfolder_name, subfolder_id" in sql for sql in db.writes)
    next_plan = await reconcile(db, d, ["root"], "parent", "individual")
    old_maps = copy.deepcopy(db.maps)
    await reconcile(db, d, ["root"], "parent", "individual", next_plan["plan_sha256"])
    assert db.maps == old_maps


@pytest.mark.asyncio
async def test_company_tax_only_and_company_columns():
    db = DB()
    db.kind = "company"
    db.entities = []
    db.secondary = [42]
    d = drive(
        [
            {"id": str(i), "name": n, "mimeType": FOLDER, "parents": ["root"], "trashed": False}
            for i, n in enumerate(
                (
                    "01_Legal_and_Licensing",
                    "02_Tax_Returns_and_Payments",
                    "03_Finance_and_Banking",
                    "99_Archive",
                )
            )
        ]
    )
    plan = await reconcile(db, d, ["root"], "parent", "company")
    assert plan["roots"][0]["reason"] == "unbound_root"
    await reconcile(db, d, ["root"], "parent", "company", plan["plan_sha256"])
    assert not db.writes
    db.entities = [42]
    plan = await reconcile(db, d, ["root"], "parent", "company")
    await reconcile(db, d, ["root"], "parent", "company", plan["plan_sha256"])
    assert all("company_id, folder_name, drive_folder_id" in sql for sql in db.writes)


@pytest.mark.asyncio
async def test_digest_and_binding_change_roll_back():
    db, d = DB(), drive(children())
    plan = await reconcile(db, d, ["root"], "parent", "individual")
    with pytest.raises(RepairError, match="plan_changed"):
        await reconcile(db, d, ["root"], "parent", "individual", "0" * 64)
    assert not db.maps
    db.change_binding_after = db.binding_reads + 2
    report = await reconcile(db, d, ["root"], "parent", "individual", plan["plan_sha256"])
    assert report["apply_results"][0]["status"] == "state_changed"
    assert not db.maps
    assert db.rollbacks == 1


@pytest.mark.asyncio
async def test_drive_change_rolls_back():
    db, d = DB(), drive(children())
    plan = await reconcile(db, d, ["root"], "parent", "individual")
    original = db.execute

    async def mutate(sql, *args):
        await original(sql, *args)
        if sql.startswith("INSERT"):
            d.api.pages[None]["files"][0]["version"] = "changed"

    db.execute = mutate
    report = await reconcile(db, d, ["root"], "parent", "individual", plan["plan_sha256"])
    assert report["apply_results"][0]["status"] == "state_changed"
    assert not db.maps


@pytest.mark.asyncio
async def test_file_inspection_scrubs_custom_values():
    db = DB()
    db.docs = [
        {
            "entity_id": 1,
            "document_type": "Sensitive secret",
            "document_category": "Private label",
            "root_id": "root",
            "eligible": True,
        }
    ]
    d = drive(children())
    result = await inspect_files(db, d, ["root"])
    assert result["files"][0]["reason"] == "matched"
    assert result["files"][0]["matches"][0]["type"] == "unknown"
    assert result["files"][0]["matches"][0]["category"] == "unknown"
    assert "Sensitive" not in json.dumps(result)
    db.company_docs = [{"entity_id": 2, "document_type": "akta", "root_id": "root"}]
    result = await inspect_files(db, d, ["root"])
    assert result["files"][0]["reason"] == "ambiguous"


def test_invalid_roots_and_incomplete_inventories():
    root = {
        "id": "root",
        "name": "Private",
        "mimeType": FOLDER,
        "parents": ["parent"],
        "trashed": True,
    }
    assert (
        inventory_root(drive(children(), root=root), "root", "parent", "individual")["reason"]
        == "invalid_root"
    )
    item = children()
    item[0]["mimeType"] = "application/vnd.google-apps.shortcut"
    assert (
        inventory_root(drive(item), "root", "parent", "individual")["reason"]
        == "ambiguous_canonical"
    )
    assert (
        inventory_root(drive(children()[1:]), "root", "parent", "individual")["reason"]
        == "missing_canonical"
    )
    d = drive(children())
    d.api.pages[None]["incompleteSearch"] = True
    with pytest.raises(RepairError, match="incomplete_inventory"):
        inventory_root(d, "root", "parent", "individual")


@pytest.mark.asyncio
async def test_existing_mapping_change_invalidates_plan():
    db, d = DB(), drive(children())
    plan = await reconcile(db, d, ["root"], "parent", "individual")
    db.maps[(1, "old")] = "old-id"
    with pytest.raises(RepairError, match="plan_changed"):
        await reconcile(db, d, ["root"], "parent", "individual", plan["plan_sha256"])
    assert not db.writes


@pytest.mark.asyncio
async def test_incomplete_schema_fails_before_drive_or_writes():
    db, d = DB(), drive(children())
    old_fetch = db.fetch

    async def incomplete(sql, *args):
        rows = await old_fetch(sql, *args)
        if "information_schema.columns" in sql:
            return rows[:-1]
        return rows

    db.fetch = incomplete
    with pytest.raises(RepairError, match="schema_missing"):
        await reconcile(db, d, ["root"], "parent", "individual")
    assert not db.writes


@pytest.mark.asyncio
async def test_new_binding_during_apply_rolls_back_without_touching_new_entity():
    db, d = DB(), drive(children())
    plan = await reconcile(db, d, ["root"], "parent", "individual")
    execute = db.execute

    async def concurrent_insert(sql, *args):
        await execute(sql, *args)
        if sql.startswith("INSERT") and 99 not in db.entities:
            db.entities.append(99)

    db.execute = concurrent_insert
    report = await reconcile(db, d, ["root"], "parent", "individual", plan["plan_sha256"])
    assert report["apply_results"][0]["status"] == "state_changed"
    assert 99 not in db.write_entities
    assert not db.maps
    assert all("LOCK TABLE" not in sql.upper() for sql in db.statements)


def test_cli_argument_error_and_exception_redaction(monkeypatch, capsys):
    from backend.scripts import crm_drive_reconcile as cli

    monkeypatch.setattr("sys.argv", ["crm_drive_reconcile", "--entity-kind", "sensitive-value"])
    assert cli.main() == 1
    captured = capsys.readouterr()
    assert captured.out.strip() == '{"error": "invalid_arguments"}'
    assert "sensitive-value" not in captured.out + captured.err

    async def failing(_args):
        raise RuntimeError("private-client-and-secret")

    monkeypatch.setattr("sys.argv", ["crm_drive_reconcile", "--file-id", "sensitive-id"])
    monkeypatch.setattr(cli, "run", failing)
    assert cli.main() == 1
    captured = capsys.readouterr()
    assert captured.out.strip() == '{"error": "operation_failed"}'
    assert "private-client-and-secret" not in captured.out + captured.err
    assert "sensitive-id" not in captured.out + captured.err


@pytest.mark.asyncio
async def test_independent_roots_continue_after_changed_root():
    class MultiFiles:
        def __init__(self):
            self.by_root = {
                root: [{**item, "id": root + item["id"], "parents": [root]} for item in children()]
                for root in ("alpha", "beta")
            }

        def get(self, **kwargs):
            root = kwargs["fileId"]
            return Request(
                {
                    "id": root,
                    "name": "Private",
                    "mimeType": FOLDER,
                    "parents": ["parent"],
                    "trashed": False,
                }
            )

        def list(self, **kwargs):
            root = kwargs["q"].split("'")[1]
            return Request({"files": self.by_root[root]})

    class MultiDB(DB):
        async def fetch(self, sql, *args):
            if "FROM clients WHERE google_drive_folder_id" in sql:
                return [{"id": {"alpha": 1, "beta": 2}[args[0]]}]
            return await super().fetch(sql, *args)

    class MultiDrive:
        def __init__(self):
            self.api = MultiFiles()

        def files(self):
            return self.api

    db, d = MultiDB(), MultiDrive()
    db.entities = [1, 2]
    plan = await reconcile(db, d, ["alpha", "beta"], "parent", "individual")
    execute = db.execute

    async def change_first(sql, *args):
        await execute(sql, *args)
        if sql.startswith("INSERT") and args[0] == 1:
            d.api.by_root["alpha"][0]["version"] = "changed"

    db.execute = change_first
    result = await reconcile(db, d, ["alpha", "beta"], "parent", "individual", plan["plan_sha256"])
    assert [item["status"] for item in result["apply_results"]] == ["state_changed", "applied"]
    assert {entity for entity, _ in db.maps} == {2}


@pytest.mark.asyncio
async def test_sql_planning_failure_is_safe_and_read_only():
    db, d = DB(), drive(children())
    db.fail_explain = True
    with pytest.raises(RepairError, match="sql_validation_failed") as exc:
        await reconcile(db, d, ["root"], "parent", "individual")
    assert "secret-schema-details" not in str(exc.value)
    assert not db.writes
    assert not db.maps
    assert not d.api.queries


def test_cli_logging_is_restored_on_import_and_main(monkeypatch, capsys):
    import importlib
    import logging

    from backend.scripts import crm_drive_reconcile as cli

    previous = logging.root.manager.disable
    try:
        logging.disable(logging.ERROR)
        cli = importlib.reload(cli)
        assert logging.root.manager.disable == logging.ERROR

        async def success(_args):
            assert logging.root.manager.disable == logging.CRITICAL
            return {"files": []}

        monkeypatch.setattr(cli, "run", success)
        monkeypatch.setattr("sys.argv", ["crm_drive_reconcile", "--file-id", "file"])
        assert cli.main() == 0
        assert logging.root.manager.disable == logging.ERROR
        monkeypatch.setattr("sys.argv", ["crm_drive_reconcile", "--unknown", "secret"])
        assert cli.main() == 1
        assert logging.root.manager.disable == logging.ERROR
        monkeypatch.setattr("sys.argv", ["crm_drive_reconcile", "--help"])
        with pytest.raises(SystemExit):
            cli.main()
        assert logging.root.manager.disable == logging.ERROR
        assert "secret" not in capsys.readouterr().out
    finally:
        logging.disable(previous)


@pytest.mark.asyncio
async def test_shared_root_blocks_all_mapping_writes():
    db, d = DB(), drive(children())
    db.entities = [1, 2]
    plan = await reconcile(db, d, ["root"], "parent", "individual")
    assert plan["roots"][0]["reason"] == "ambiguous_owner"
    assert plan["roots"][0]["changes"] == []
    result = await reconcile(db, d, ["root"], "parent", "individual", plan["plan_sha256"])
    assert result["apply_results"][0]["status"] == "ambiguous_owner"
    assert db.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,entity,name",
    [
        ("individual", 1, "legacy-private-label"),
        ("individual", 7, "01_Profile"),
        ("company", 8, "private-label"),
    ],
)
async def test_reverse_mapping_conflicts_block_any_owner_or_legacy(kind, entity, name):
    db, d = DB(), drive(children())
    db.extra_reverse = [{"kind": kind, "entity_id": entity, "name": name, "folder_id": "0"}]
    plan = await reconcile(db, d, ["root"], "parent", "individual")
    assert plan["roots"][0]["reason"] == "conflicting_mapping"
    assert plan["roots"][0]["changes"] == []
    assert "private-label" not in json.dumps(plan)
    result = await reconcile(db, d, ["root"], "parent", "individual", plan["plan_sha256"])
    assert result["apply_results"][0]["status"] == "conflicting_mapping"
    assert not db.writes


@pytest.mark.asyncio
async def test_plan_inverse_records_can_restore_overwrite_with_cas():
    from backend.services.crm.drive_taxonomy_repair import _UPSERTS

    db, d = DB(), drive(children())
    db.maps[(1, "01_Profile")] = "old-opaque-id"
    plan = await reconcile(db, d, ["root"], "parent", "individual")
    changes = plan["roots"][0]["changes"]
    assert len(changes) == 4
    updated = next(c for c in changes if c["folder_name"] == "01_Profile")
    assert updated["action"] == "update"
    assert updated["old_folder_id"] == "old-opaque-id"
    assert updated["new_folder_id"] == "0"
    assert updated["inverse"] == {
        "operation": "restore_old_id",
        "expected_current_id": "0",
        "restore_id": "old-opaque-id",
    }
    inserted = next(c for c in changes if c["action"] == "insert")
    assert inserted["inverse"]["operation"] == "remove_created_row_manually"
    assert inserted["inverse"]["expected_current_id"] == inserted["new_folder_id"]
    result = await reconcile(db, d, ["root"], "parent", "individual", plan["plan_sha256"])
    assert result["apply_results"][0]["changes"] == changes
    assert db.maps[(1, "01_Profile")] == "0"
    # This fake demonstrates the inverse CAS; production has no rollback executor.
    assert await db.fetchval(_UPSERTS["individual"], 1, "01_Profile", "old-opaque-id", "0") == 1
    assert db.maps[(1, "01_Profile")] == "old-opaque-id"
    assert (
        await db.fetchval(_UPSERTS["individual"], 1, "01_Profile", "0", "wrong-current-id") is None
    )
    assert db.maps[(1, "01_Profile")] == "old-opaque-id"


@pytest.mark.asyncio
async def test_reverse_binding_change_invalidates_confirmed_plan():
    db, d = DB(), drive(children())
    plan = await reconcile(db, d, ["root"], "parent", "individual")
    db.extra_reverse = [{"kind": "individual", "entity_id": 9, "name": "unknown", "folder_id": "0"}]
    with pytest.raises(RepairError, match="plan_changed"):
        await reconcile(db, d, ["root"], "parent", "individual", plan["plan_sha256"])
    assert not db.writes


@pytest.mark.asyncio
async def test_relevant_rows_locked_and_new_reverse_owner_causes_rollback():
    db, d = DB(), drive(children())
    plan = await reconcile(db, d, ["root"], "parent", "individual")
    execute = db.execute

    async def concurrent_reverse(sql, *args):
        await execute(sql, *args)
        if sql.startswith("INSERT"):
            db.extra_reverse = [
                {"kind": "company", "entity_id": 11, "name": "Private Person", "folder_id": "0"}
            ]

    db.execute = concurrent_reverse
    result = await reconcile(db, d, ["root"], "parent", "individual", plan["plan_sha256"])
    assert result["apply_results"][0]["status"] == "state_changed"
    assert "Private Person" not in json.dumps(result)
    assert not db.maps
    assert any("FROM clients WHERE id = ANY" in q and "FOR SHARE" in q for q in db.queries)
    assert any("WHERE subfolder_id = ANY" in q and "FOR UPDATE" in q for q in db.queries)
    assert any("WHERE drive_folder_id = ANY" in q and "FOR UPDATE" in q for q in db.queries)
    assert all("LOCK TABLE" not in s.upper() for s in db.statements)
