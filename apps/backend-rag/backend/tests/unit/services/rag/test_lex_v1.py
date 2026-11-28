import asyncio
import hashlib
import json
import subprocess
import sys
import uuid
from argparse import Namespace
from datetime import date
from pathlib import Path

import httpx
import pytest
from qdrant_client import QdrantClient, models

from backend.services.rag import lex_v1 as lex
from backend.services.rag.voyage_public import BASE_URL, DIMENSIONS
from backend.services.rag.voyage_shadow import LegalSparseV1
from scripts import legal_lex_v1 as cli

ON = date(2026, 11, 28)
SENTINEL = "sentinel-secret-9f3a"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def record(
    canonical="UU-1-2020", pasal="1", seq=0, body="Setiap orang wajib membayar pajak.", **kw
):
    header = f"[UU 1/2020 · Judul · Pasal {pasal}]"
    text = header + "\n" + body
    key = f"{canonical}#body#{pasal}#{seq:04d}"
    out = {
        "amended_by": [],
        "authority_tier": "1",
        "ayat_from": None,
        "ayat_to": None,
        "bab": None,
        "body_sha256": sha(body),
        "canonical_id": canonical,
        "chunk_key": key,
        "citation": "UU 1/2020",
        "domain": "tax",
        "header": header,
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "legal-lex-v1:" + key + ":" + sha(text))),
        "issuer": "DPR",
        "jenis": "UU",
        "language": "id",
        "nomor": "1",
        "official_url": "https://peraturan.example.go.id/uu-1-2020",
        "page_from": None,
        "page_to": None,
        "part": "body",
        "pasal": pasal,
        "repealed_by": [],
        "seq": seq,
        "source_sha256": sha(canonical),
        "status": "berlaku",
        "status_scope": "na",
        "structure": "parser_v5_5",
        "tahun": 2020,
        "text": text,
        "title": "Judul",
        "valid_from": "2020-01-01",
        "valid_to": None,
    }
    out.update(kw)
    return out


def reseal(rec):
    """Recompute the derived fields after a test edits text or key."""
    rec["body_sha256"] = sha(rec["text"][len(rec["header"]) + 1 :])
    rec["id"] = str(
        uuid.uuid5(uuid.NAMESPACE_URL, "legal-lex-v1:" + rec["chunk_key"] + ":" + sha(rec["text"]))
    )
    return rec


def write_chunks(tmp_path, records, name="chunks.jsonl"):
    path = tmp_path / name
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def basic():
    return [
        record("UU-1-2020", "1", 0, "Pajak penghasilan dikenakan atas penghasilan orang pribadi."),
        record("PP-2-2021", "2", 0, "Modal disetor perseroan terbatas penanaman modal asing."),
        record("PERMEN-3-2022", "3", 0, "Masa berlaku paspor paling singkat enam bulan."),
    ]


def fake_voyage(seen=None, failures=(), reject=None, sizes=None):
    queue = list(failures)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.headers.get("authorization"))
        if queue:
            return httpx.Response(queue.pop(0))
        body = json.loads(request.content)
        if sizes is not None:
            sizes.append(len(body["input"]))
        if reject is not None and (status := reject(body["input"])):
            return httpx.Response(status)
        data = []
        for i, text in enumerate(body["input"]):
            vector = [0.0] * DIMENSIONS
            vector[int(sha(text)[:8], 16) % DIMENSIONS] = 1.0
            data.append({"index": i, "embedding": vector})
        return httpx.Response(200, json={"model": body["model"], "data": data})

    return httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler))


FORBIDDEN = {
    "recreate_collection",
    "upload_points",
    "upload_collection",
    "batch_update_points",
    "overwrite_payload",
    "set_payload",
    "delete",
    "delete_collection",
    "delete_vectors",
    "delete_payload",
    "update_collection_aliases",
    "clear_payload",
}
WRITES = (
    "upsert",
    "create_",
    "update_",
    "set_",
    "delete",
    "clear",
    "upload_",
    "batch_",
    "overwrite",
)


class Recorder:
    """Wraps a local client and records every method call."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = []

    def __getattr__(self, name):
        target = getattr(self.inner, name)
        if not callable(target):
            return target

        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return target(*args, **kwargs)

        return call

    def names(self):
        return [c[0] for c in self.calls]

    def written_collections(self):
        return {
            (a[0] if a else kw.get("collection_name"))
            for name, a, kw in self.calls
            if name.startswith(WRITES)
        }


@pytest.fixture
def db(tmp_path):
    client = QdrantClient(path=str(tmp_path / "store"))
    yield client
    client.close()


def build(db, tmp_path, records=None):
    path, digest = write_chunks(tmp_path, records or basic())
    release = lex.Release.load(path, digest)
    return release, asyncio.run(lex.write_release(db, release, fake_voyage()))


def test_rebuild_writes_zero(db, tmp_path):
    release, added = build(db, tmp_path)
    assert added == 3
    assert db.count(lex.COLLECTION, exact=True).count == 5
    embedded, recorder = [], Recorder(db)
    api = fake_voyage(embedded)
    assert asyncio.run(lex.write_release(recorder, release, api)) == 0
    assert embedded == []
    assert "upsert" not in recorder.names()
    assert db.count(lex.COLLECTION, exact=True).count == 5


def test_changed_byte_refused(db, tmp_path):
    path, digest = write_chunks(tmp_path, basic())
    raw = bytearray(path.read_bytes())
    raw[10] ^= 1
    path.write_bytes(bytes(raw))
    with pytest.raises(lex.LexError, match="checksum"):
        lex.Release.load(path, digest)
    assert not db.collection_exists(lex.COLLECTION)


def test_foreign_nonempty_refused(db, tmp_path):
    lex.ensure_collection(db, create=True)
    db.upsert(
        lex.COLLECTION,
        [models.PointStruct(id=5, vector={lex.DENSE: [1.0] + [0.0] * 1023}, payload={"kind": "x"})],
        wait=True,
    )
    path, digest = write_chunks(tmp_path, basic())
    with pytest.raises(lex.LexError, match="build marker"):
        asyncio.run(lex.write_release(db, lex.Release.load(path, digest), fake_voyage()))
    assert db.count(lex.COLLECTION, exact=True).count == 1


def test_alias_refused(db, tmp_path):
    db.create_collection("elsewhere", vectors_config=models.VectorParams(size=4, distance="Cosine"))
    db.update_collection_aliases(
        [
            models.CreateAliasOperation(
                create_alias=models.CreateAlias(
                    collection_name="elsewhere", alias_name=lex.COLLECTION
                )
            )
        ]
    )
    path, digest = write_chunks(tmp_path, basic())
    with pytest.raises(lex.LexError, match="alias"):
        asyncio.run(lex.write_release(db, lex.Release.load(path, digest), fake_voyage()))
    assert db.count("elsewhere").count == 0


def search(db, question, limit=8, api=None):
    return asyncio.run(lex.search(db, question, limit, api or fake_voyage(), on=ON))


def test_search_excludes_dicabut(db, tmp_path):
    release, _ = build(db, tmp_path)
    text = "Ketentuan lama yang sudah dicabut mengenai pajak penghasilan."
    vector = [0.0] * DIMENSIONS
    vector[int(sha(text)[:8], 16) % DIMENSIONS] = 1.0
    payload = release.payload(basic()[0]) | {"status_vigensi": "dicabut", "text": text}
    db.upsert(
        lex.COLLECTION,
        [models.PointStruct(id=str(uuid.uuid4()), vector={lex.DENSE: vector}, payload=payload)],
        wait=True,
    )
    hits = search(db, text)
    assert hits and all(h["metadata"]["status_vigensi"] != "dicabut" for h in hits)
    assert text not in [h["text"] for h in hits]


def test_search_excludes_expired(db, tmp_path):
    old = record(
        "UU-9-2007", "9", 0, "Ketentuan yang berakhir.", status="diubah", valid_to="2020-01-01"
    )
    future = record(
        "UU-8-2007", "8", 0, "Ketentuan yang masih berlaku.", status="diubah", valid_to="2030-01-01"
    )
    build(db, tmp_path, [*basic(), old, future])
    hits = search(db, old["text"], limit=10)
    ids = {h["metadata"]["document_id"] for h in hits}
    assert "UU-9-2007" not in ids
    assert {"UU-8-2007", "UU-1-2020"} <= ids


def test_payload_is_citable(db, tmp_path):
    build(db, tmp_path)
    hits = search(db, basic()[0]["text"])
    assert len(hits) == 3
    for hit in hits:
        meta = {**hit["metadata"], "text": hit["text"]}
        assert hit["score_kind"] == "LEX_V1_RRF"
        assert meta["text"] and meta["title"].startswith("UU 1/2020 — ")
        assert meta["url"].startswith("https://")
        assert meta["document_id"] and meta["status_vigensi"] in lex.STATUSES
        assert meta["retrieval_scope"] == "current" and meta["kind"] == "chunk"


def test_search_refused_until_ready(db):
    lex.ensure_collection(db, create=True)
    with pytest.raises(lex.LexError, match="not ready"):
        search(db, "pajak")


def test_dense_only_query_without_sparse_terms(db, tmp_path):
    build(db, tmp_path)
    hits = search(db, "?!")
    assert len(hits) == 3 and {h["score_kind"] for h in hits} == {"LEX_V1_DENSE"}


def run_cli(monkeypatch, capsys, argv, env=None):
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    code = cli.main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_never_prints_secret(monkeypatch, capsys, tmp_path):
    seen = []
    real = httpx.AsyncClient
    mock = fake_voyage(seen)
    monkeypatch.setattr(
        cli.httpx, "AsyncClient", lambda **kw: real(**{**kw, "transport": mock._transport})
    )
    path, digest = write_chunks(tmp_path, basic())
    env = {"VOYAGE_API_KEY": SENTINEL, "QDRANT_API_KEY": SENTINEL}
    local = ["--local-dir", str(tmp_path / "cli-store")]
    code, out, err = run_cli(
        monkeypatch, capsys, ["build", *local, "--chunks", str(path), "--sha256", digest], env
    )
    assert code == 0 and json.loads(out)["added"] == 3
    assert seen and set(seen) == {"Bearer " + SENTINEL}
    code, out2, err2 = run_cli(monkeypatch, capsys, ["status", *local])
    assert json.loads(out2)["count"] == 5
    code, out3, err3 = run_cli(
        monkeypatch,
        capsys,
        ["status", "--remote"],
        {**env, "QDRANT_URL": "https://127.0.0.1:1"},
    )
    assert code != 0
    assert SENTINEL not in out + err + out2 + err2 + out3 + err3


def test_cli_refuses_credentialed_url(monkeypatch, capsys):
    env = {"QDRANT_URL": "https://user:hunter2@example.com", "QDRANT_API_KEY": SENTINEL}
    code, out, err = run_cli(monkeypatch, capsys, ["status", "--remote"], env)
    assert code != 0 and "credentials" in err
    assert "hunter2" not in out + err and SENTINEL not in out + err


def test_cli_status_absent_writes_nothing(monkeypatch, capsys, tmp_path):
    local = ["--local-dir", str(tmp_path / "empty")]
    code, out, _ = run_cli(monkeypatch, capsys, ["status", *local])
    assert code == 0 and json.loads(out) == {"collection": "legal_lex_v1", "exists": False}
    db = QdrantClient(path=str(tmp_path / "empty"))
    assert db.get_collections().collections == []
    db.close()


def test_cli_help_lists_commands():
    done = subprocess.run(
        [sys.executable, "-m", "scripts.legal_lex_v1", "--help"], capture_output=True, text=True
    )
    assert done.returncode == 0
    assert all(c in done.stdout for c in ("build", "status", "query", "eval"))


def test_oversize_file_refused(tmp_path, monkeypatch):
    path, digest = write_chunks(tmp_path, basic())
    monkeypatch.setattr(lex, "MAX_FILE", 10)
    with pytest.raises(lex.LexError, match="size"):
        lex.Release.load(path, digest)


def test_backoff_retries_once_on_429_then_succeeds(db, tmp_path, monkeypatch):
    sleeps = []

    async def nap(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(lex.asyncio, "sleep", nap)
    path, digest = write_chunks(tmp_path, basic())
    api = fake_voyage(failures=[429])
    added = asyncio.run(lex.write_release(db, lex.Release.load(path, digest), api))
    assert added == 3 and sleeps == [lex.BACKOFF_SECONDS]


def test_backoff_gives_up_and_other_errors_are_not_retried(monkeypatch):
    async def nap(seconds):
        pass

    monkeypatch.setattr(lex.asyncio, "sleep", nap)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(lex.embed_retry(fake_voyage(failures=[503] * 6), ["a"], documents=True))
    assert asyncio.run(lex.embed_retry(fake_voyage(failures=[503] * 5), ["a"], documents=True))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(lex.embed_retry(fake_voyage(failures=[400]), ["a"], documents=True))


def test_eval_recall_arithmetic(db, tmp_path):
    build(db, tmp_path)
    a, b = basic()[0], basic()[1]
    cases = [
        {
            "id": "c1",
            "question": a["text"],
            "domain": "tax",
            "ready_norms": ["UU-1-2020", "PP-2-2021"],
            "ready_pasal": [["UU-1-2020", "1"]],
        },
        {
            "id": "c2",
            "question": b["text"],
            "domain": "company",
            "ready_norms": ["PP-2-2021"],
            "ready_pasal": [],
        },
    ]
    cpath = tmp_path / "cases.json"
    cpath.write_text(json.dumps({"cases": cases}))
    cdigest = hashlib.sha256(cpath.read_bytes()).hexdigest()
    spath = tmp_path / "supersedes.json"
    spath.write_text(json.dumps({"UU-1-2020": ["old-1"], "PP-2-2021": ["old-2"]}))
    sdigest = hashlib.sha256(spath.read_bytes()).hexdigest()

    class Service:
        async def search_with_reranking(self, **kw):
            assert kw["collection_override"] == "legal_unified"
            return {
                "results": [{"metadata": {"document_id": "old-1"}}, {"metadata": {"id": "zzz"}}]
            }

    args = Namespace(
        cases=cpath, sha256=cdigest, k=1, user_level=3, supersedes=spath, supersedes_sha256=sdigest
    )
    out = asyncio.run(cli.evaluate(db, args, fake_voyage(), Service()))
    rows = {r["id"]: r for r in out["rows"]}
    assert rows["c1"]["lex_v1"] == {"recall": 0.5, "hit": True, "pasal_hit": True}
    assert rows["c1"]["legal_unified"] == {"recall": 0.5, "hit": True, "pasal_hit": False}
    assert rows["c2"]["lex_v1"]["recall"] == 1.0
    assert rows["c2"]["legal_unified"] == {"recall": 0.0, "hit": False, "pasal_hit": False}
    assert out["lex_v1"]["all"] == {"cases": 2, "recall": 0.75, "hit_rate": 1.0, "pasal_hit": 1}
    assert out["legal_unified"]["by_domain"]["tax"]["recall"] == 0.5
    assert "question" not in json.dumps(out)


def test_nothing_else_in_backend_imports_this_module():
    root = Path(lex.__file__).parents[3]
    own = Path(lex.__file__).resolve()
    offenders = [
        str(p)
        for p in (root / "backend").rglob("*.py")
        if p.resolve() != own and "/tests/" not in str(p) and "lex_v1" in p.read_text()
    ]
    assert offenders == []


def test_key_with_dash_for_missing_pasal_and_high_seq_passes(tmp_path):
    rec = record("UU-5-2020", "x", 9999)
    rec.update(pasal=None, chunk_key="UU-5-2020#body#-#9999")
    path, digest = write_chunks(tmp_path, [reseal(rec)])
    assert len(lex.Release.load(path, digest).records) == 1


def test_eval_loaders_parse_the_real_shapes(tmp_path):
    cases = {
        "ambiguous": [],
        "cases": [
            {
                "id": "Q1",
                "domain": "immigration",
                "question": "q",
                "ready_norms": ["A-1-2020"],
                "ready_pasal": [["A-1-2020", "56"]],
            }
        ],
    }
    cpath = tmp_path / "c.json"
    cpath.write_text(json.dumps(cases))
    spath = tmp_path / "s.json"
    spath.write_text(json.dumps({"A-1-2020": ["old_1"]}))
    cdigest = hashlib.sha256(cpath.read_bytes()).hexdigest()
    sdigest = hashlib.sha256(spath.read_bytes()).hexdigest()
    assert len(cli.load_cases(cpath, cdigest)) == 1
    assert cli.old_to_canonical(cli.load_supersedes(spath, sdigest)) == {"old_1": "A-1-2020"}


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {"cases": []},
        {
            "cases": [
                {
                    "id": "Q",
                    "domain": "d",
                    "question": "q",
                    "ready_norms": ["A"],
                    "ready_pasal": [{"norm": "A", "pasal": "1"}],
                }
            ]
        },
        {
            "cases": [
                {
                    "id": "Q",
                    "domain": "d",
                    "question": "q",
                    "ready_norms": ["A"],
                    "ready_pasal": [["A"]],
                }
            ]
        },
        {
            "cases": [
                {"id": "Q", "domain": "d", "question": "q", "ready_norms": [], "ready_pasal": []}
            ]
        },
    ],
)
def test_eval_cases_other_shapes_refused(tmp_path, bad):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError):
        cli.load_cases(path, hashlib.sha256(path.read_bytes()).hexdigest())


def test_eval_supersedes_other_shapes_refused(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"A": "old"}))
    with pytest.raises(ValueError):
        cli.load_supersedes(path, hashlib.sha256(path.read_bytes()).hexdigest())


@pytest.fixture(autouse=True)
def no_hybrid(monkeypatch):
    monkeypatch.setattr(cli, "hybrid_enabled", lambda: False)


def run_write(db, release, api=None):
    return asyncio.run(lex.write_release(db, release, api or fake_voyage()))


def load(tmp_path, records, name="chunks.jsonl"):
    path, digest = write_chunks(tmp_path, records, name)
    return lex.Release.load(path, digest), digest


def test_build_touches_only_legal_lex_v1_and_never_deletes(db, tmp_path):
    release, _ = load(tmp_path, basic())
    recorder = Recorder(db)
    run_write(recorder, release)
    run_write(recorder, release)
    search(recorder, basic()[0]["text"])
    assert recorder.written_collections() == {"legal_lex_v1"}
    assert FORBIDDEN.isdisjoint(recorder.names())


def test_foreign_compatible_collection_gets_no_index_and_no_write(db, tmp_path):
    lex.ensure_collection(db, create=True)
    db.upsert(
        lex.COLLECTION,
        [models.PointStruct(id=5, vector={lex.DENSE: [1.0] + [0.0] * 1023}, payload={"kind": "x"})],
        wait=True,
    )
    release, _ = load(tmp_path, basic())
    recorder = Recorder(db)
    with pytest.raises(lex.LexError, match="build marker"):
        run_write(recorder, release)
    assert "create_payload_index" not in recorder.names()
    assert "upsert" not in recorder.names()


def test_absent_collection_is_created_and_indexed(db, tmp_path):
    release, _ = load(tmp_path, basic())
    recorder = Recorder(db)
    run_write(recorder, release)
    indexed = {kw["field_name"] for n, _, kw in recorder.calls if n == "create_payload_index"}
    assert indexed == {*lex.INDEXED, "valid_to"}
    assert recorder.names().count("create_collection") == 1


def test_markers_and_chunks_carry_the_pinned_release(db, tmp_path):
    release, digest = load(tmp_path, basic())
    run_write(db, release)
    rows, _ = db.scroll(lex.COLLECTION, limit=50, with_payload=True)
    chunks = [r.payload for r in rows if r.payload["kind"] == "chunk"]
    markers = {r.id: r.payload for r in rows if r.payload["kind"] != "chunk"}
    assert len(chunks) == 3 and {c["release"] for c in chunks} == {digest}
    assert markers[1]["release"] == markers[0]["release"] == digest
    assert markers[1]["kind"] == "build" and markers[0]["kind"] == "ready"
    assert markers[0]["chunk_count"] == 3 and markers[0]["contract"] == lex.CONTRACT


def test_build_marker_of_another_release_refused(db, tmp_path):
    first, _ = load(tmp_path, basic())
    run_write(db, first)
    other, _ = load(tmp_path, [record("UU-7-2020", "7", 0, "Ketentuan lain.")], "other.jsonl")
    with pytest.raises(lex.LexError, match="foreign release marker"):
        run_write(db, other)
    assert db.count(lex.COLLECTION, exact=True).count == 5


def test_persisted_payload_difference_refused(db, tmp_path):
    release, _ = load(tmp_path, basic())
    run_write(db, release)
    db.set_payload(lex.COLLECTION, {"text": "tampered"}, points=[basic()[1]["id"]])
    with pytest.raises(lex.LexError, match="persisted payload disagrees"):
        run_write(db, release)


def test_published_collection_missing_a_chunk_refused(db, tmp_path):
    release, _ = load(tmp_path, basic())
    run_write(db, release)
    db.delete(lex.COLLECTION, points_selector=[basic()[0]["id"]])
    with pytest.raises(lex.LexError, match="published collection is incomplete"):
        run_write(db, release)


def test_unexpected_extra_point_fails_the_final_count(db, tmp_path):
    release, _ = load(tmp_path, basic())
    lex.ensure_collection(db, create=True)
    db.upsert(
        lex.COLLECTION,
        [
            models.PointStruct(id=1, vector={}, payload=release.marker("build")),
            models.PointStruct(id=9, vector={lex.DENSE: [1.0] + [0.0] * 1023}, payload={"k": 1}),
        ],
        wait=True,
    )
    with pytest.raises(lex.LexError, match="unexpected points"):
        run_write(db, release)
    assert db.retrieve(lex.COLLECTION, [0]) == []


def test_search_refuses_a_ready_marker_of_another_contract(db, tmp_path):
    release, _ = load(tmp_path, basic())
    run_write(db, release)
    db.set_payload(lex.COLLECTION, {"contract": "something/else"}, points=[0])
    with pytest.raises(lex.LexError, match="not ready"):
        search(db, "pajak")


def edit(**changes):
    def apply(rec):
        rec.update(changes)
        return reseal(rec)

    return apply


def body_of(rec, body):
    rec["text"] = rec["header"] + "\n" + body
    return reseal(rec)


def drop(name):
    def apply(rec):
        del rec[name]
        return rec

    return apply


REFUSALS = {
    "fields: missing": (drop("bab"), "fields differ"),
    "fields: extra": (edit(surprise=1), "fields differ"),
    "str: citation": (edit(citation=5), "citation is not a string"),
    "str: text": (lambda r: {**r, "text": 5}, "text is not a string"),
    "optstr: bab": (edit(bab=5), "bab has the wrong type"),
    "optstr: pasal": (edit(pasal=5), "pasal has the wrong type"),
    "valid_from: none": (edit(valid_from=None), "valid_from has the wrong type"),
    "valid_from: format": (edit(valid_from="2020/01/01"), "valid_from is not an ISO date"),
    "valid_to: format": (edit(valid_to="31-12-2020"), "valid_to is not an ISO date"),
    "valid_to: impossible": (edit(valid_to="2020-02-31"), "valid_to is not an ISO date"),
    "valid_to: type": (edit(valid_to=2020), "valid_to has the wrong type"),
    "optint: page_from": (edit(page_from="3"), "page_from has the wrong type"),
    "optint: page_to bool": (edit(page_to=True), "page_to has the wrong type"),
    "int: tahun": (edit(tahun="2020"), "tahun is not an integer"),
    "int: seq bool": (edit(seq=False), "seq is not an integer"),
    "list: type": (edit(amended_by="x"), "amended_by is not a list"),
    "list: element": (edit(repealed_by=[1]), "repealed_by is not a list"),
    "key: too few segments": (edit(chunk_key="UU-2-2021#body#2"), "chunk_key disagrees"),
    "key: canonical": (edit(chunk_key="OTHER#body#2#0000"), "chunk_key disagrees"),
    "key: part": (edit(chunk_key="PP-2-2021#lampiran#2#0000"), "chunk_key disagrees"),
    "key: pasal": (edit(chunk_key="PP-2-2021#body#7#0000"), "chunk_key disagrees"),
    "key: seq value": (edit(chunk_key="PP-2-2021#body#2#0003"), "chunk_key disagrees"),
    "key: seq unpadded": (edit(chunk_key="PP-2-2021#body#2#0"), "chunk_key disagrees"),
    "id": (lambda r: {**r, "id": str(uuid.uuid4())}, "id mismatch"),
    "body_sha256": (lambda r: {**r, "body_sha256": "0" * 64}, "body_sha256 mismatch"),
    "no header": (edit(text="plain text without the header"), "does not start with its header"),
    "too long": (lambda r: body_of(r, "a" * lex.MAX_TEXT), "text length out of bounds"),
    "empty text": (lambda r: {**r, "text": ""}, "text length out of bounds"),
    "empty body": (lambda r: body_of(r, ""), "text has no body"),
    "blank body": (lambda r: body_of(r, "   \n"), "text has no body"),
    "http url": (edit(official_url="http://x.example/a"), "official_url is not https"),
    "dicabut": (edit(status="dicabut"), "status is not accepted"),
}


@pytest.mark.parametrize("name", sorted(REFUSALS))
def test_record_refusals_name_their_reason(tmp_path, name):
    mutator, reason = REFUSALS[name]
    records = basic()
    records[1] = mutator(dict(records[1]))
    path, digest = write_chunks(tmp_path, records)
    with pytest.raises(lex.LexError, match=f"^line 2: .*{reason}"):
        lex.Release.load(path, digest)


def test_max_text_boundary_is_accepted(tmp_path):
    rec = record("UU-4-2020", "4")
    rec = body_of(rec, "b" * (lex.MAX_TEXT - len(rec["header"]) - 1))
    assert len(rec["text"]) == lex.MAX_TEXT
    path, digest = write_chunks(tmp_path, [rec])
    assert len(lex.Release.load(path, digest).records) == 1


def test_duplicate_id_refused(tmp_path):
    path, digest = write_chunks(tmp_path, [basic()[0], basic()[0]])
    with pytest.raises(lex.LexError, match="line 2: duplicate id or chunk_key"):
        lex.Release.load(path, digest)


def test_duplicate_chunk_key_with_different_ids_refused(tmp_path):
    first = basic()[0]
    second = body_of(dict(first), "Isi yang berbeda sama sekali.")
    assert second["id"] != first["id"] and second["chunk_key"] == first["chunk_key"]
    path, digest = write_chunks(tmp_path, [first, second])
    with pytest.raises(lex.LexError, match="line 2: duplicate id or chunk_key"):
        lex.Release.load(path, digest)


def test_not_json_and_empty_files_refused(tmp_path):
    path = tmp_path / "x.jsonl"
    path.write_text("{not json}\n")
    with pytest.raises(lex.LexError, match="line 1: not JSON"):
        lex.Release.load(path, hashlib.sha256(path.read_bytes()).hexdigest())
    path.write_text("\n")
    with pytest.raises(lex.LexError, match="no records"):
        lex.Release.load(path, hashlib.sha256(path.read_bytes()).hexdigest())


def guarded_cli(monkeypatch, capsys, tmp_path, argv):
    opened = []
    monkeypatch.setattr(cli, "open_database", lambda *a, **k: opened.append(a))
    code = cli.main(argv)
    return code, opened, capsys.readouterr()


def test_cli_wrong_sha_never_opens_a_database(monkeypatch, capsys, tmp_path):
    path, digest = write_chunks(tmp_path, basic())
    store = tmp_path / "store"
    argv = ["build", "--local-dir", str(store), "--chunks", str(path), "--sha256", "0" * 64]
    code, opened, out = guarded_cli(monkeypatch, capsys, tmp_path, argv)
    assert code != 0 and opened == [] and "checksum" in out.err
    assert not store.exists()


def test_cli_invalid_record_never_opens_a_database(monkeypatch, capsys, tmp_path):
    records = basic()
    records[2] = edit(status="dicabut")(dict(records[2]))
    path, digest = write_chunks(tmp_path, records)
    store = tmp_path / "store"
    argv = ["build", "--local-dir", str(store), "--chunks", str(path), "--sha256", digest]
    code, opened, out = guarded_cli(monkeypatch, capsys, tmp_path, argv)
    assert code != 0 and opened == [] and "line 3" in out.err
    assert not store.exists()


def vec(*parts):
    out = [0.0] * DIMENSIONS
    for index, weight in parts:
        out[index] += weight
    return out


def slot(text):
    return int(sha(text)[:8], 16) % DIMENSIONS


def plant(db, point_id, text, dense, **payload):
    sparse = LegalSparseV1().generate_sparse_vector(text)
    vectors = {lex.DENSE: dense}
    if sparse["indices"]:
        vectors[lex.SPARSE] = models.SparseVector(**sparse)
    base = {
        "kind": "chunk",
        "text": text,
        "status_vigensi": "berlaku",
        "valid_to": None,
        "document_id": str(point_id),
        "title": "t",
        "url": "https://x.example",
        "retrieval_scope": "current",
    }
    db.upsert(
        lex.COLLECTION,
        [models.PointStruct(id=point_id, vector=vectors, payload={**base, **payload})],
        wait=True,
    )


def ready_collection(db):
    lex.ensure_collection(db, create=True)
    marker = {"kind": "ready", "contract": lex.CONTRACT, "release": "x", "chunk_count": 0}
    db.upsert(lex.COLLECTION, [models.PointStruct(id=0, vector={}, payload=marker)], wait=True)


def flood_scenario(db):
    """Nine excluded points identical to the query flood both candidate lists.

    A is found only by the dense list and B only by the sparse list (five dense
    fillers push B out of the dense top 6), so the right answer for limit 2 is {A, B}
    and every one of the four filter sites matters.
    """
    query = "kuncirahasia pembayaran nomorunik"
    q = slot(query)
    ready_collection(db)
    for n in range(3):
        plant(db, 100 + n, query, vec((q, 1.0)), status_vigensi="dicabut")
        plant(db, 110 + n, query, vec((q, 1.0)), valid_to="2020-01-01", status_vigensi="diubah")
        plant(db, 120 + n, query, vec((q, 1.0)), kind="other")
    plant(db, 1, "alfa beta gamma", vec((q, 0.9), (1000, 0.1)), document_id="A")
    for n in range(5):
        plant(
            db,
            10 + n,
            f"delta{n} epsilon",
            vec((q, 0.8 - 0.1 * n), (1001, 0.2)),
            document_id=f"F{n}",
        )
    plant(db, 2, query + " tambahan", vec((1002, 1.0)), document_id="B")
    return query


def test_excluded_points_never_survive_any_filter_site(db):
    query = flood_scenario(db)
    hits = search(db, query, limit=2)
    assert {h["metadata"]["document_id"] for h in hits} == {"A", "B"}
    assert len(hits) == 2


@pytest.mark.parametrize("site", ["dense_prefetch", "sparse_prefetch"])
def test_each_filter_site_is_necessary(db, monkeypatch, site):
    query = flood_scenario(db)
    real = lex.models.Prefetch
    seen = []

    class Spy(real):
        pass

    # Strip the filter at exactly one site and the flood must show.
    def strip_prefetch(**kw):
        seen.append(kw["using"])
        if (site == "dense_prefetch" and kw["using"] == lex.DENSE) or (
            site == "sparse_prefetch" and kw["using"] == lex.SPARSE
        ):
            kw["filter"] = None
        return real(**kw)

    monkeypatch.setattr(lex.models, "Prefetch", strip_prefetch)
    hits = search(db, query, limit=2)
    assert {h["metadata"]["document_id"] for h in hits} != {"A", "B"} or len(hits) != 2


def test_every_prefetch_and_the_fused_query_carry_the_same_filter(db):
    query = flood_scenario(db)
    recorder = Recorder(db)
    search(recorder, query, limit=2)
    (call,) = [c for c in recorder.calls if c[0] == "query_points"]
    kwargs = call[2]
    expected = lex.eligible(ON)
    assert kwargs["query_filter"] == expected
    assert [p.using for p in kwargs["prefetch"]] == [lex.DENSE, lex.SPARSE]
    assert all(p.filter == expected for p in kwargs["prefetch"])
    assert kwargs["limit"] == 2 and all(p.limit == 6 for p in kwargs["prefetch"])
    must_not = {c.key for c in expected.must_not}
    assert must_not == {"status_vigensi", "valid_to"} and expected.must[0].key == "kind"


def test_dense_only_branch_excludes_dicabut_expired_and_non_chunks(db):
    query = "?!"
    q = slot(query)
    assert LegalSparseV1().generate_query_sparse_vector(query)["indices"] == []
    ready_collection(db)
    plant(db, 100, "tua", vec((q, 1.0)), status_vigensi="dicabut")
    plant(db, 101, "lewat", vec((q, 1.0)), valid_to="2020-01-01", status_vigensi="diubah")
    plant(db, 102, "lain", vec((q, 1.0)), kind="other")
    plant(db, 1, "satu", vec((q, 0.5), (1000, 0.5)), document_id="A")
    plant(db, 2, "dua", vec((q, 0.4), (1001, 0.6)), document_id="B")
    hits = search(db, query, limit=2)
    assert [h["metadata"]["document_id"] for h in hits] == ["A", "B"]
    assert {h["score_kind"] for h in hits} == {"LEX_V1_DENSE"}


def test_valid_to_boundary_keeps_today_and_drops_yesterday(db, tmp_path):
    keep = record(
        "UU-6-2020", "6", 0, "Berlaku sampai hari ini.", status="diubah", valid_to="2026-11-28"
    )
    drop_ = record("UU-5-2020", "5", 0, "Berakhir kemarin.", status="diubah", valid_to="2026-11-27")
    build(db, tmp_path, [keep, drop_, *basic()])
    ids = {h["metadata"]["document_id"] for h in search(db, keep["text"], limit=10)}
    assert "UU-6-2020" in ids and "UU-5-2020" not in ids


def test_hybrid_label_and_limit_validation(db, tmp_path):
    build(db, tmp_path)
    assert {h["score_kind"] for h in search(db, basic()[0]["text"])} == {"LEX_V1_RRF"}
    with pytest.raises(lex.LexError, match="limit"):
        search(db, "pajak", limit=0)


def test_real_sized_batches_respect_the_real_constants():
    many = [body_of(record(f"UU-{i}-2020", str(i)), "x" * 4400) for i in range(150)]
    assert all(len(r["text"]) > 4400 for r in many)
    groups = lex.batches(tuple(many))
    assert lex.BATCH_TEXTS == 64 and lex.BATCH_TOKENS == 100_000 and lex.CHARS_PER_TOKEN == 2
    assert all(len(g) <= 64 for g in groups)
    assert all(sum(len(r["text"]) for r in g) / 2 <= 100_000 for g in groups)
    assert len(groups) > 3 and sum(len(g) for g in groups) == 150
    small = [record(f"UU-{i}-2020", str(i), body="x") for i in range(150)]
    assert [len(g) for g in lex.batches(tuple(small))] == [64, 64, 22]


def test_http_400_halves_the_batch_until_it_fits(db, tmp_path):
    sizes = []
    api = fake_voyage(reject=lambda texts: 400 if len(texts) > 1 else None, sizes=sizes)
    release, _ = load(tmp_path, basic())
    assert run_write(db, release, api) == 3
    assert sizes[0] == 3 and set(sizes[1:]) == {1, 2} and sizes.count(1) == 3


def test_http_400_on_a_single_text_names_its_chunk(db, tmp_path):
    release, _ = load(tmp_path, basic())
    api = fake_voyage(reject=lambda texts: 400)
    with pytest.raises(
        lex.LexError,
        match="voyage refused chunk PP-2-2021#body#2#0000|voyage refused chunk UU-1-2020#body#1#0000|voyage refused chunk PERMEN-3-2022#body#3#0000",
    ):
        run_write(db, release, api)


def test_baseline_error_key_fails_the_eval_with_its_type(db, tmp_path):
    build(db, tmp_path)
    args, _ = eval_args(tmp_path, k=1)

    class Service:
        async def search_with_reranking(self, **kw):
            return {"results": [], "error": "boom secret detail"}

    with pytest.raises(cli.BaselineFailure) as caught:
        asyncio.run(cli.evaluate(db, args, fake_voyage(), Service()))
    assert (caught.value.kind, caught.value.case_id) == ("SearchError", "c1")


def test_baseline_exception_fails_the_eval_with_its_type(db, tmp_path):
    build(db, tmp_path)
    args, _ = eval_args(tmp_path, k=1)

    class Service:
        async def search_with_reranking(self, **kw):
            raise TimeoutError("x")

    with pytest.raises(cli.BaselineFailure) as caught:
        asyncio.run(cli.evaluate(db, args, fake_voyage(), Service()))
    assert caught.value.kind == "TimeoutError"


def eval_args(tmp_path, k=1):
    case = {
        "id": "c1",
        "question": basic()[0]["text"],
        "domain": "tax",
        "ready_norms": ["UU-1-2020"],
        "ready_pasal": [],
    }
    cpath = tmp_path / "cases.json"
    cpath.write_text(json.dumps({"cases": [case]}))
    digest = hashlib.sha256(cpath.read_bytes()).hexdigest()
    return Namespace(
        cases=cpath, sha256=digest, k=k, user_level=3, supersedes=None, supersedes_sha256=None
    ), digest


def test_cli_eval_exits_nonzero_with_json_error_on_baseline_failure(monkeypatch, capsys, tmp_path):
    real = httpx.AsyncClient
    mock = fake_voyage()
    monkeypatch.setattr(
        cli.httpx, "AsyncClient", lambda **kw: real(**{**kw, "transport": mock._transport})
    )
    monkeypatch.setenv("VOYAGE_API_KEY", SENTINEL)
    path, digest = write_chunks(tmp_path, basic())
    local = ["--local-dir", str(tmp_path / "s")]
    assert cli.main(["build", *local, "--chunks", str(path), "--sha256", digest]) == 0
    capsys.readouterr()
    args, cdigest = eval_args(tmp_path)

    class Service:
        async def search_with_reranking(self, **kw):
            return {"error": "x"}

    monkeypatch.setattr(cli, "baseline_service", lambda: Service())
    code = cli.main(["eval", *local, "--cases", str(args.cases), "--sha256", cdigest])
    out = capsys.readouterr().out
    assert code == 1 and json.loads(out) == {"error": "SearchError", "case": "c1"}


@pytest.mark.parametrize(
    ("enabled", "has_method", "expected"),
    [
        (True, True, "hybrid_search_with_reranking"),
        (False, True, "search_with_reranking"),
        (True, False, "search_with_reranking"),
    ],
)
def test_baseline_follows_the_served_branch(
    db, tmp_path, monkeypatch, enabled, has_method, expected
):
    build(db, tmp_path)
    args, _ = eval_args(tmp_path, k=2)
    calls = []

    async def method(self, **kw):
        calls.append(kw)
        return {"results": []}

    attrs = {"search_with_reranking": method}
    if has_method:
        attrs["hybrid_search_with_reranking"] = method
    service = type("Service", (), attrs)()
    monkeypatch.setattr(cli, "hybrid_enabled", lambda: enabled)
    out = asyncio.run(cli.evaluate(db, args, fake_voyage(), service))
    assert out["baseline_branch"] == expected
    assert calls == [
        {
            "query": basic()[0]["text"],
            "user_level": 3,
            "limit": 2,
            "collection_override": "legal_unified",
        }
    ]


def sources():
    return {
        "id": dict.fromkeys(("document_id", "chapter_id", "id", "none"), 0),
        "pasal": dict.fromkeys(("pasal", "pasal_number", "text", "none"), 0),
    }


def test_hit_identity_prefers_document_id_and_counts_sources():
    src, mapping = sources(), {"old-1": "UU-1-2020"}
    chunks = [
        {"metadata": {"document_id": "old-1", "chapter_id": "BAB-1", "pasal": "5"}},
        {"metadata": {"chapter_id": "old-1", "pasal_number": 7}},
        {"metadata": {"id": "zzz"}, "text": "x" * 290 + " Pasal 8"},
        {"metadata": {}, "text": "x" * 300 + " Pasal 9"},
        {"metadata": {"document_id": "old-1"}, "text": "Pasal 56A tentang paspor"},
    ]
    got = [cli.hit_identity(c, mapping, src) for c in chunks]
    assert got == [
        ("UU-1-2020", "5"),
        ("UU-1-2020", "7"),
        ("zzz", "8"),
        ("", None),
        ("UU-1-2020", "56A"),
    ]
    assert src["id"] == {"document_id": 2, "chapter_id": 1, "id": 1, "none": 1}
    assert src["pasal"] == {"pasal": 1, "pasal_number": 1, "text": 2, "none": 1}


def test_eval_output_reports_field_sources(db, tmp_path):
    build(db, tmp_path)
    args, _ = eval_args(tmp_path)

    class Service:
        async def search_with_reranking(self, **kw):
            return {"results": [{"metadata": {"document_id": "UU-1-2020"}, "text": "Pasal 3"}]}

    out = asyncio.run(cli.evaluate(db, args, fake_voyage(), Service()))
    assert out["baseline_field_sources"]["id"]["document_id"] == 1
    assert out["baseline_field_sources"]["pasal"]["text"] == 1


def test_lex_error_is_a_value_error():
    assert issubclass(lex.LexError, ValueError)


def test_cli_prints_only_the_type_for_foreign_exceptions(monkeypatch, capsys, tmp_path):
    from pydantic import BaseModel, ValidationError

    class Model(BaseModel):
        number: int

    async def boom(args):
        try:
            Model(number=SENTINEL)
        except ValidationError:
            raise

    monkeypatch.setattr(cli, "run", boom)
    code = cli.main(["status", "--local-dir", str(tmp_path / "s")])
    err = capsys.readouterr().err
    assert code == 1 and SENTINEL not in err and err.strip() == "error: ValidationError"

    async def plain(args):
        raise ValueError("private detail")

    monkeypatch.setattr(cli, "run", plain)
    code = cli.main(["status", "--local-dir", str(tmp_path / "s")])
    err = capsys.readouterr().err
    assert code == 1 and "private detail" not in err and "ValueError" in err

    async def refusal(args):
        raise lex.LexError("collection is not ready")

    monkeypatch.setattr(cli, "run", refusal)
    assert cli.main(["status", "--local-dir", str(tmp_path / "s")]) == 2
    assert "collection is not ready" in capsys.readouterr().err
