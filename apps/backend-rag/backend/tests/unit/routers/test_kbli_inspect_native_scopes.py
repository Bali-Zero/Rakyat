"""Public inspect responses bind current scope data independently of cached KG summaries."""

import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_optional_database_pool
from backend.app.routers import kbli_notebook
from backend.core import cache as cache_module

DATASET = Path(__file__).resolve().parents[6] / "data/source_documents/KBLI_2025_FINAL_CLEAN.json"
DATASET_SHA = "75d5b9f527f3dbb56ebe560725ae260bc2ba5f897090495aec5c4c16c2ca7580"
ROWS_SHA = "eda9f92d5f0fc28aa612f29a9e51967dcf1b08e6104ba99c65297591ba3fc14c"
INSPECT = "/api/v1/kbli-notebook/inspect/"
ATTESTED_COUNTS = {
    "42914": 8,
    "42915": 4,
    "42991": 5,
    "42994": 5,
    "42995": 5,
    "42998": 11,
    "55106": 6,
    "55201": 3,
    "55203": 3,
    "56101": 6,
    "56303": 4,
    "55209": 3,
    "56102": 6,
    "68111": 4,
    "68210": 8,
    "68292": 4,
    "70201": 3,
    "70209": 8,
    "85510": 8,
    "93116": 3,
    "96220": 3,
    "96230": 4,
}
LEGACY_SOURCES = {
    "42914": ["42914"],
    "42915": ["42915", "42918", "42917", "42916"],
    "42991": ["42917"],
    "42994": ["42924"],
    "42995": ["42918"],
    "42998": ["42917"],
    "55201": ["80100"],
    "55203": ["55193"],
    "56101": ["56101", "56102", "56109"],
    "56303": ["56303"],
    "55209": ["55199"],
    "56102": ["56102"],
    "68111": ["68111"],
    "68210": ["68200"],
    "68292": ["68200"],
    "70201": ["70201", "70204"],
    "70209": ["70209"],
    "85510": [],
    "93116": ["93116"],
    "96220": [],
    "96230": [],
}


@pytest.fixture
def make_endpoint(monkeypatch):
    def make(
        *, present=True, node_exists=True, licenses_present=True, licensing_status="REGULATED"
    ):
        store = {}
        conn = MagicMock()

        async def node_query(query, entity_id):
            if not node_exists:
                return None
            code = entity_id.removeprefix("kbli:")
            return {
                "name": f"KG {code}",
                "description": "",
                "properties": {
                    "pp28_sources": LEGACY_SOURCES.get(code, ["55199"]),
                    "licensing_status": licensing_status,
                },
            }

        async def rows_query(query, *args):
            if "catalogue_size" in query:
                return [{"catalogue_size": 1559, "canonical_rows": int(present)}]
            if "relationship_type = 'REQUIRES'" in query:
                if not licenses_present:
                    return []
                return [
                    {
                        "entity_id": "perizinan:izin_usaha_industri",
                        "name": "Izin Usaha Industri",
                        "target_entity_type": "perizinan",
                        "properties": {
                            "skala_usaha": ["Besar"],
                            "kategori_risiko": "Menengah Rendah",
                            "kewajiban": ["Synthetic graph obligation"],
                            "persyaratan": ["Synthetic graph requirement"],
                        },
                    }
                ]
            return []

        conn.fetchrow = AsyncMock(side_effect=node_query)
        conn.fetch = AsyncMock(side_effect=rows_query)
        conn.fetchval = AsyncMock(return_value="sektor:I")

        @asynccontextmanager
        async def acquire():
            yield conn

        pool = MagicMock()
        pool.acquire = acquire
        cache = MagicMock()
        cache.get = AsyncMock(side_effect=store.get)
        cache.set = AsyncMock(side_effect=lambda key, value, ttl: store.update({key: value}))
        monkeypatch.setattr(cache_module, "get_cache_service", lambda: cache)
        monkeypatch.setattr(
            kbli_notebook,
            "_get_kbli_payload_from_qdrant",
            AsyncMock(
                return_value={
                    "kategori_risiko": "Menengah Rendah",
                    "scope_rows_status": "available",
                    "scope_rows": [{"scope_uraian": "Synthetic Qdrant claim"}],
                }
            ),
        )
        app = FastAPI()
        app.include_router(kbli_notebook.router, prefix="/api/v1")
        app.dependency_overrides[get_optional_database_pool] = lambda: pool
        return TestClient(app), store, conn

    return make


def test_actual_endpoint_returns_six_native_rows_separately_from_legacy_licenses(make_endpoint):
    client, store, conn = make_endpoint()
    stale_key = "kbli_inspect_v8_55106"
    store[stale_key] = {
        "code": "55106",
        "title": "stale v8",
        "description": "",
        "sector": "I",
        "risk_profile": "Menengah Rendah",
        "licensing_status": "REGULATED",
        "licenses": [],
        "related_codes": [],
        "scope_rows_status": "verified_empty",
        "scope_rows": [],
    }
    response = client.get(INSPECT + "55106")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "KG 55106"
    assert body["scope_rows_status"] == "available"
    assert body["scope_rows_source"]["dataset_sha256"] == DATASET_SHA
    assert body["scope_rows_source"]["rows_sha256"] == ROWS_SHA
    assert body["scope_rows_source"]["source"] == "OSS_RBA_resiko_2025"
    assert str(body["scope_rows_source"]["kbli_vintage"]) == "2025"
    # The KG view keeps its old obligations meaning; the scoped view gets its own source.
    assert body["risk_profile"] == "Menengah Rendah"
    assert body["licenses"] == [
        {
            "type": "Izin Usaha Industri",
            "scale": ["Besar"],
            "risk_level": "Menengah Rendah",
            "sla": "N/A",
            "requirements": ["Synthetic graph obligation"],
        }
    ]
    assert body["licensing_content_inherited_from"] == ["55199"]
    note = body["licensing_note"]
    assert "Historical licence entries have not been verified for this activity" in note
    assert "must not be relied on for current licensing terms" in note
    assert "confirm the licence type with the bali zero team before acting" in note.lower()
    assert (
        "KBLI 2025 OSS" in note
        and "scope-specific risk levels, requirements and obligations" in note
    )
    assert "_" not in note and re.search(r"\b\d{5}\b", note) is None
    native = next(
        row["per_skala"]
        for row in json.loads(DATASET.read_bytes())["data"]
        if row["kode_kbli_2025"] == "55106"
    )
    assert len(native) == len(body["scope_rows"]) == 6
    for index, (raw, served) in enumerate(zip(native, body["scope_rows"], strict=True)):
        assert served["row_ordinal"] == index
        assert served["scope_index"] == raw["scope_index"]
        assert served["scope_uraian"] == raw["scope_uraian"]
        assert served["scale"] == raw["skala_usaha"]
        assert served["risk_level"] == raw["kategori_risiko"]
        assert served["requirements"] == raw["persyaratan"]
        assert served["obligations"] == raw["kewajiban"]
        assert served["authorities"] == raw["kewenangan"]
    for threshold, risk in [("≤", "Menengah Rendah"), (">", "Menengah Tinggi")]:
        scoped = [row for row in body["scope_rows"] if threshold in row["scope_uraian"]]
        assert [row["scale"] for row in scoped] == [["Kecil"], ["Menengah"], ["Besar"]]
        assert {row["risk_level"] for row in scoped} == {risk}
    (key,) = set(store) - {stale_key}
    store[key].update(
        scope_rows_status="available",
        scope_rows=[{"scope_uraian": "bad cached scope"}],
        scope_rows_source={"dataset_sha256": "wrong revision"},
    )
    warm = client.get(INSPECT + "55106")
    assert warm.status_code == 200, warm.text
    assert warm.json() == body
    conn.fetchrow.assert_awaited_once()


def test_wrong_code_in_cache_is_a_miss_before_binding_native_rows(make_endpoint):
    client, store, conn = make_endpoint()
    assert client.get(INSPECT + "55201").status_code == 200
    store["kbli_inspect_v9_55106"] = store["kbli_inspect_v9_55201"]
    response = client.get(INSPECT + "55106")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == "55106" and body["title"] == "KG 55106"
    assert body["scope_rows_status"] == "available"
    assert conn.fetchrow.await_count == 2


def test_unattested_code_never_acquires_scopes_from_qdrant_or_graph(make_endpoint):
    client, _, _ = make_endpoint()
    response = client.get(INSPECT + "56304")
    assert response.status_code == 200
    body = response.json()
    assert body["licenses"] and body["risk_profile"] == "Menengah Rendah"
    assert body["scope_rows_status"] == "unavailable"
    assert body["scope_rows"] is None
    assert "KBLI 2020 code 55199" in body["licensing_note"]


def test_current_native_rows_override_cached_pending_until_source_is_lost(
    make_endpoint, monkeypatch
):
    client, cache, _ = make_endpoint(licenses_present=False, licensing_status="PENDING_REGULATION")
    path = INSPECT + "85510"
    verified = client.get(path).json()
    assert verified["scope_rows_status"] == "available"
    assert verified["licensing_status"] == "REGULATED"
    assert verified["licenses"] == [] and len(verified["scope_rows"]) == 8
    assert cache["kbli_inspect_v9_85510"]["licensing_status"] == "PENDING_REGULATION"
    with monkeypatch.context() as gap:
        gap.setattr(
            kbli_notebook,
            "read_native_scope_coverage",
            lambda code: kbli_notebook.NativeScopeCoverage(),
        )
        unavailable = client.get(path).json()
        assert unavailable["licensing_status"] == "PENDING_REGULATION"
        assert unavailable["scope_rows_status"] == "unavailable"
    assert client.get(path).json() == verified


@pytest.mark.parametrize("code", ATTESTED_COUNTS)
@pytest.mark.parametrize("licenses_present", [True, False])
def test_warm_cache_tracks_source_loss_and_recovery(
    make_endpoint, monkeypatch, code, licenses_present
):
    client, _, conn = make_endpoint(licenses_present=licenses_present)
    path = INSPECT + code
    covered = client.get(path).json()
    assert covered["scope_rows_status"] == "available"
    reader = kbli_notebook.read_native_scope_coverage
    unavailable = reader("56304")
    with monkeypatch.context() as gap:
        gap.setattr(kbli_notebook, "read_native_scope_coverage", lambda code: unavailable)
        without_source = client.get(path).json()
        assert without_source["scope_rows_status"] == "unavailable"
        assert without_source["scope_rows"] is None
        assert without_source["scope_rows_source"] is None
        assert (
            without_source["licensing_content_inherited_from"]
            == covered["licensing_content_inherited_from"]
        )
        note = without_source["licensing_note"]
        gap_note = (
            "Current scope-specific coverage could not be verified. "
            "Confirm the licence type with the Bali Zero team before acting on it."
        )
        assert gap_note in note
        assert "come from the KBLI 2025 OSS" not in note
        assert "_" not in note and re.search(r"\b\d{5}\b", note) is None
        if licenses_present:
            assert "Historical licence entries" in note
            assert "must not be relied on for current licensing terms" in note
        else:
            assert note == gap_note
    recovered = client.get(path).json()
    assert recovered == covered
    conn.fetchrow.assert_awaited_once()


@pytest.mark.parametrize("code,count", ATTESTED_COUNTS.items())
def test_attested_codes_bind_every_native_field_on_cold_and_warm_get(make_endpoint, code, count):
    client, _, conn = make_endpoint()
    expected = next(
        record["per_skala"]
        for record in json.loads(DATASET.read_bytes())["data"]
        if record["kode_kbli_2025"] == code
    )
    response = client.get(INSPECT + code)
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == code
    assert body["scope_rows_status"] == "available"
    assert body["scope_rows_source"]["dataset_sha256"] == DATASET_SHA
    assert body["scope_rows_source"]["kbli_vintage"] == "2025"
    inherited = {
        "42914": None,
        "42915": None,
        "42991": ["42917"],
        "42994": ["42924"],
        "42995": ["42918"],
        "42998": ["42917"],
        "55106": ["55199"],
        "55201": ["80100"],
        "55203": ["55193"],
        "55209": ["55199"],
        "56101": None,
        "56303": None,
        "56102": None,
        "68111": None,
        "68210": ["68200"],
        "68292": ["68200"],
        "70201": None,
        "70209": None,
        "85510": None,
        "93116": None,
        "96220": None,
        "96230": None,
    }[code]
    assert body["licensing_content_inherited_from"] == inherited
    note = body["licensing_note"]
    if code == "55201":
        assert (
            "archived licence reference conflicts with the official classification mapping" in note
        )
        assert "80100" not in note
    elif code == "55203":
        assert (
            "archived obligations are inconsistent with the official activity-specific record"
            in note
        )
    elif code == "70201":
        assert "archive combines tourism and industrial consultancy terms" in note
    else:
        assert "Historical licence entries have not been verified for this activity" in note
    assert "must not be relied on for current licensing terms" in note
    assert "current scope-specific risk levels, requirements and obligations" in note
    assert "KBLI 2025 OSS" in note and "confirm" in note.lower()
    assert "Bali Zero team" in note and "_" not in note
    assert re.search(r"\b\d{5}\b", note) is None
    assert body["licenses"][0]["requirements"] == ["Synthetic graph obligation"]
    assert len(body["scope_rows"]) == len(expected) == count
    for ordinal, (actual, raw) in enumerate(zip(body["scope_rows"], expected, strict=True)):
        assert actual == {
            "row_ordinal": ordinal,
            "scope_index": raw["scope_index"],
            "scope_uraian": raw["scope_uraian"],
            "scale": raw["skala_usaha"],
            "type": {
                "Rendah": "NIB",
                "Menengah Rendah": "NIB dan Sertifikat Standar",
                "Menengah Tinggi": "NIB dan Sertifikat Standar",
                "Tinggi": "NIB dan Izin",
            }[raw["kategori_risiko"]],
            "risk_level": raw["kategori_risiko"],
            "requirements": raw["persyaratan"],
            "obligations": raw["kewajiban"],
            "authorities": raw["kewenangan"],
        }
    assert client.get(INSPECT + code).json() == body
    conn.fetchrow.assert_awaited_once()


@pytest.mark.parametrize("code,count", ATTESTED_COUNTS.items())
def test_empty_legacy_licenses_note_only_describes_current_source(make_endpoint, code, count):
    client, _, conn = make_endpoint(licenses_present=False)
    response = client.get(INSPECT + code)
    assert response.status_code == 200
    body = response.json()
    assert body["licenses"] == []
    assert body["scope_rows_status"] == "available"
    assert len(body["scope_rows"]) == count
    assert body["licensing_note"] == (
        "The current scope-specific risk levels, requirements and obligations "
        "come from the KBLI 2025 OSS record. "
        "Confirm the licence type with the Bali Zero team before acting on it."
    )
    assert client.get(INSPECT + code).json() == body
    conn.fetchrow.assert_awaited_once()


def test_tombstone_never_asserts_a_verified_empty_or_available_scope(make_endpoint):
    client, store, conn = make_endpoint(present=False)
    body = client.get(INSPECT + "55106").json()
    assert body["licensing_status"] == "NOT_IN_KBLI_2025"
    assert body["scope_rows_status"] == "unavailable"
    assert body["scope_rows"] is None
    assert body["scope_rows_source"] is None
    (cached,) = store.values()
    cached.update(scope_rows_status="available", scope_rows=[{"scope_uraian": "bad cached scope"}])
    assert client.get(INSPECT + "55106").json() == body
    conn.fetchrow.assert_awaited_once()


def test_missing_graph_node_remains_404_even_if_native_data_exists(make_endpoint):
    client, _, _ = make_endpoint(node_exists=False)
    assert client.get(INSPECT + "55106").status_code == 404


def test_default_openapi_schema_exposes_scoped_fields(make_endpoint):
    client, _, _ = make_endpoint()
    schema = client.get("/openapi.json").json()
    response = schema["paths"][INSPECT + "{code}"]["get"]["responses"]["200"]
    model = response["content"]["application/json"]["schema"]["$ref"].split("/")[-1]
    fields = schema["components"]["schemas"][model]["properties"]
    assert {"scope_rows", "scope_rows_status", "scope_rows_source"} <= fields.keys()
