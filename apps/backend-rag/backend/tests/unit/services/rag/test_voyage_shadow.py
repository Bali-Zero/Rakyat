import hashlib
import json

import httpx
import pytest
from qdrant_client import QdrantClient, models

from backend.services.rag import voyage_shadow as sh
from backend.services.rag.voyage_public import digest_text

QUESTION = "Pasal 8d Perpres 77/2021 ayat (2)"
TEXTS = [
    "[CONTEXT: PERPRES - NO 77 - TAHUN 2021 - Pasal 8d] Ayat (2).",
    "[CONTEXT: PERPRES - NO 77 - TAHUN 2020 - Pasal 8d] Ayat (1).",
]


def files(tmp_path, texts=TEXTS):
    src, idx = tmp_path / "public.json", tmp_path / "vec.json"
    src.write_text(
        json.dumps(
            {
                "public_queries": [{"text": QUESTION, "rerank_model": "rerank-3-lite"}],
                "public_documents": texts,
            }
        )
    )
    vectors = {}
    for i, text in enumerate(texts):
        vector = [0.0] * sh.DIMENSIONS
        vector[i] = 1.0
        vectors[digest_text(text)] = vector
    idx.write_text(
        json.dumps(
            {
                "version": 1,
                "dimensions": 1024,
                "document_model": sh.DOC_MODEL,
                "query_sha256": {digest_text(QUESTION): "rerank-3-lite"},
                "vectors": vectors,
            }
        )
    )
    return (
        src,
        hashlib.sha256(src.read_bytes()).hexdigest(),
        idx,
        hashlib.sha256(idx.read_bytes()).hexdigest(),
    )


@pytest.fixture
def db():
    client = QdrantClient(":memory:")
    yield client
    client.close()


def test_real_local_qdrant_hybrid_query_and_noop_restart(tmp_path):
    release = sh.PublicRelease.load(*files(tmp_path))
    db = QdrantClient(path=str(tmp_path / "store"))
    try:
        assert sh.write_release(db, release) == 2
    finally:
        db.close()
    db = QdrantClient(path=str(tmp_path / "store"))
    try:
        assert sh.write_release(db, release) == 0
        assert db.count(release.collection).count == 4
        ranked = sh._retrieve_by_vector(db, release, QUESTION, [1.0] + [0.0] * 1023, 2)
        assert ranked[0].payload["text"] == TEXTS[0]
        assert {x.payload["text"] for x in ranked} == set(TEXTS)
        assert db.get_aliases().aliases == []
    finally:
        db.close()


@pytest.mark.parametrize("kind", ["dimension", "sparse", "alias"])
def test_foreign_storage_is_untouched(db, tmp_path, kind):
    rel = sh.PublicRelease.load(*files(tmp_path))
    name = "legal_unified" if kind == "alias" else rel.collection
    db.create_collection(
        name,
        vectors_config={
            sh.DENSE: models.VectorParams(
                size=1536 if kind == "dimension" else 1024, distance=models.Distance.COSINE
            )
        },
        sparse_vectors_config={
            sh.SPARSE: models.SparseVectorParams(
                modifier=None if kind == "sparse" else models.Modifier.IDF
            )
        },
    )
    if kind == "alias":
        db.update_collection_aliases(
            [
                models.CreateAliasOperation(
                    create_alias=models.CreateAlias(collection_name=name, alias_name=rel.collection)
                )
            ]
        )
    with pytest.raises(ValueError):
        sh.write_release(db, rel)
    assert db.count(name).count == 0


def test_resume_partial_then_reject_tampered_point(db, tmp_path, monkeypatch):
    rel = sh.PublicRelease.load(*files(tmp_path))
    monkeypatch.setattr(sh, "BATCH", 1)
    original = db.upsert

    def stop(collection, points, **kwargs):
        if points[0].payload.get("kind") == "passage" and db.count(collection).count == 2:
            raise RuntimeError("interrupted")
        return original(collection, points, **kwargs)

    monkeypatch.setattr(db, "upsert", stop)
    with pytest.raises(RuntimeError):
        sh.write_release(db, rel)
    with pytest.raises(ValueError, match="not complete"):
        sh._retrieve_by_vector(db, rel, QUESTION, [1.0] * 1024, 2)
    monkeypatch.setattr(db, "upsert", original)
    assert sh.write_release(db, rel) == 1
    pt = sh.passage_point(rel, sh.LegalSparseV1(), digest_text(TEXTS[0]))
    db.set_payload(rel.collection, {"text": "changed"}, points=[pt.id], wait=True)
    with pytest.raises(ValueError, match="disagrees"):
        sh.write_release(db, rel)
    with pytest.raises(ValueError, match="unverified"):
        sh._retrieve_by_vector(db, rel, QUESTION, [1.0] * 1024, 2)


def test_unauthorized_question_and_foreign_vectors(db, tmp_path):
    rel = sh.PublicRelease.load(*files(tmp_path))
    sh.write_release(db, rel)
    with pytest.raises(ValueError, match="unauthorized"):
        sh._retrieve_by_vector(db, rel, "private", [], 1)
    with pytest.raises(ValueError, match="dimension"):
        sh._retrieve_by_vector(db, rel, QUESTION, [1.0] * 1536, 1)


def test_sparse_features_and_unicode_boundaries():
    v = sh.LegalSparseV1()
    header = v.tokenize(TEXTS[0])
    query = v.tokenize("Pasal 8D Ayat (2) Peraturan Presiden Nomor 77 Tahun 2021.")
    assert {"pasal_8d", "ayat_2", "perpres_77_2021"} <= set(header) & set(query)
    assert "pasal_iv" in v.tokenize("Pasal IV")
    assert not {"pasal_iv", "pasal_vx", "pasal_5"} & set(
        v.tokenize("PASAL IVé, Pasal VX, Pasal 5â")
    )
    assert "perpres_77_2021" not in v.tokenize("Perpres 77/2021/XX")


def test_foreign_inputs_rejected(tmp_path):
    src, src_sha, idx, idx_sha = files(tmp_path)
    src.write_text(src.read_text().replace("2020", "2022"))
    with pytest.raises(ValueError, match="checksum"):
        sh.PublicRelease.load(src, src_sha, idx, idx_sha)
    with pytest.raises(ValueError, match="do not match"):
        sh.PublicRelease.load(src, hashlib.sha256(src.read_bytes()).hexdigest(), idx, idx_sha)


async def test_query_calls_only_the_pinned_space_after_release_checks(tmp_path, db):
    rel = sh.PublicRelease.load(*files(tmp_path))
    calls = []

    def answer(req):
        body = json.loads(req.content)
        calls.append(body)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0] + [0.0] * 1023}]})

    async with httpx.AsyncClient(
        base_url="https://api.voyageai.com/v1/", transport=httpx.MockTransport(answer)
    ) as api:
        with pytest.raises(ValueError, match="does not exist"):
            await sh.query_release(db, rel, QUESTION, 1, api)
        sh.write_release(db, rel)
        with pytest.raises(ValueError, match="unauthorized"):
            await sh.query_release(db, rel, "other query", 1, api)
        result = await sh.query_release(db, rel, QUESTION, 1, api)
    assert len(calls) == 1
    assert calls[0]["model"] == "voyage-4-lite" and calls[0]["input_type"] == "query"
    assert result[0].payload["text"] == TEXTS[0]


def test_other_release_never_mutates_the_first(tmp_path, db):
    first = sh.PublicRelease.load(*files(tmp_path))
    sh.write_release(db, first)
    second = sh.PublicRelease.load(*files(tmp_path, texts=[TEXTS[0]]))
    assert sh.write_release(db, second) == 1
    assert first.collection != second.collection
    assert db.count(first.collection).count == 4 and db.count(second.collection).count == 3


def test_remote_shadow_uses_configured_https_only(monkeypatch):
    from secrets import token_hex
    from unittest.mock import Mock

    from scripts import voyage_qdrant_shadow as cli

    transport = Mock()
    monkeypatch.setattr(cli, "QdrantClient", transport)
    credential = token_hex(16)
    monkeypatch.setenv("QDRANT_API_KEY", credential)
    for url in (
        "",
        "http://example.org",
        "https://user@example.org",
        "https://example.org/?test=1",
    ):
        monkeypatch.setenv("QDRANT_URL", url)
        with pytest.raises(ValueError):
            cli.open_database(None, True)
    transport.assert_not_called()
    monkeypatch.setenv("QDRANT_URL", "https://example.org:6333")
    assert cli.open_database(None, True) is transport.return_value
    transport.assert_called_once_with(
        url="https://example.org:6333", api_key=credential, timeout=15
    )
