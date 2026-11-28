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


def fake_voyage(seen=None, failures=()):
    queue = list(failures)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.headers.get("authorization"))
        if queue:
            return httpx.Response(queue.pop(0))
        body = json.loads(request.content)
        data = []
        for i, text in enumerate(body["input"]):
            vector = [0.0] * DIMENSIONS
            vector[int(sha(text)[:8], 16) % DIMENSIONS] = 1.0
            data.append({"index": i, "embedding": vector})
        return httpx.Response(200, json={"model": body["model"], "data": data})

    return httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler))


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
    embedded = []
    api = fake_voyage(embedded)
    assert asyncio.run(lex.write_release(db, release, api)) == 0
    assert embedded == []
    assert db.count(lex.COLLECTION, exact=True).count == 5


def test_changed_byte_refused(db, tmp_path):
    path, digest = write_chunks(tmp_path, basic())
    raw = bytearray(path.read_bytes())
    raw[10] ^= 1
    path.write_bytes(bytes(raw))
    with pytest.raises(ValueError, match="checksum"):
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
    with pytest.raises(ValueError, match="build marker"):
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
    with pytest.raises(ValueError, match="alias"):
        asyncio.run(lex.write_release(db, lex.Release.load(path, digest), fake_voyage()))
    assert db.count("elsewhere").count == 0


def test_build_touches_only_legal_lex_v1(db, tmp_path):
    touched = set()

    class Recording:
        def __getattr__(self, name):
            target = getattr(db, name)
            if name.startswith(("upsert", "create_", "update_", "set_", "delete", "clear")):

                def call(collection, *a, **kw):
                    touched.add(collection)
                    return target(collection, *a, **kw)

                return call
            return target

    path, digest = write_chunks(tmp_path, basic())
    asyncio.run(lex.write_release(Recording(), lex.Release.load(path, digest), fake_voyage()))
    assert touched == {"legal_lex_v1"}


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
    with pytest.raises(ValueError, match="not ready"):
        search(db, "pajak")


def test_dense_only_query_without_sparse_terms(db, tmp_path):
    build(db, tmp_path)
    assert len(search(db, "?!")) == 3


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


def mutate(**changes):
    def apply(rec):
        rec.update(changes)
        return rec

    return apply


def drop(name):
    def apply(rec):
        del rec[name]
        return rec

    return apply


def retext(text):
    def apply(rec):
        rec["text"] = text
        return reseal(rec)

    return apply


REFUSALS = {
    "missing_field": drop("bab"),
    "extra_field": mutate(surprise=1),
    "seq_type": mutate(seq="0"),
    "list_type": mutate(amended_by="x"),
    "key_segments": mutate(chunk_key="UU-1-2020#body#1"),
    "key_canonical": mutate(chunk_key="OTHER#body#1#0000"),
    "key_pasal_disagrees": mutate(chunk_key="UU-1-2020#body#7#0000"),
    "key_part_disagrees": mutate(chunk_key="UU-1-2020#lampiran#2#0000"),
    "key_seq_disagrees": mutate(chunk_key="UU-1-2020#body#2#0003"),
    "key_seq_unpadded": mutate(chunk_key="UU-1-2020#body#2#0"),
    "bad_id": mutate(id=str(uuid.uuid4())),
    "bad_body_sha": mutate(body_sha256="0" * 64),
    "no_header": retext("no header here"),
    "empty_body": retext("[UU 1/2020 · Judul · Pasal 1]\n"[:-1]),
    "too_long": retext("[UU 1/2020 · Judul · Pasal 1]\n" + "a" * lex.MAX_TEXT),
    "http_url": mutate(official_url="http://x.example/a"),
    "dicabut": mutate(status="dicabut"),
    "valid_to": mutate(valid_to="31/12/2020"),
    "valid_to_impossible": mutate(valid_to="2020-02-31"),
}


@pytest.mark.parametrize("name", sorted(REFUSALS))
def test_record_refusals_write_nothing(db, tmp_path, name):
    records = basic()
    records[1] = REFUSALS[name](dict(records[1]))
    path, digest = write_chunks(tmp_path, records)
    with pytest.raises(ValueError, match="line 2"):
        lex.Release.load(path, digest)
    assert not db.collection_exists(lex.COLLECTION)


def test_duplicate_ids_refused(tmp_path):
    path, digest = write_chunks(tmp_path, [basic()[0], basic()[0]])
    with pytest.raises(ValueError, match="duplicate"):
        lex.Release.load(path, digest)


def test_oversize_file_refused(tmp_path, monkeypatch):
    path, digest = write_chunks(tmp_path, basic())
    monkeypatch.setattr(lex, "MAX_FILE", 10)
    with pytest.raises(ValueError, match="size"):
        lex.Release.load(path, digest)


def test_batches_respect_text_and_token_limits(monkeypatch):
    monkeypatch.setattr(lex, "BATCH_TOKENS", 20_000)
    many = [record(canonical=f"UU-{i}-2020", pasal=str(i), body="x" * 30) for i in range(150)]
    sizes = [len(b) for b in lex.batches(tuple(many))]
    assert sizes == [64, 64, 22]
    big = [record(canonical=f"UU-{i}-2020", pasal=str(i), body="y" * 4000) for i in range(60)]
    groups = lex.batches(tuple(big))
    assert all(sum(len(r["text"]) for r in g) / 3 <= 20_000 for g in groups)
    assert len(groups) > 1 and sum(len(g) for g in groups) == 60


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
