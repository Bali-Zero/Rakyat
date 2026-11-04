"""Regulation-only cards and migration 343; DB checks use a private disposable cluster."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from backend.services.visa_engine.families import importer

MIGRATIONS = Path(importer.__file__).parents[3] / "db/migrations_v2"
MIGRATION = MIGRATIONS / "343_visa_types_law_family_c_secondary.sql"
CODES = ("C7", "C8", "C22")
CARDS = {code: json.loads((importer.HERE / f"law/C/{code}.json").read_text()) for code in CODES}
FORWARD, ROLLBACK = MIGRATION.read_text().split("-- === ROLLBACK ===", 1)
PURPOSES = {
    "C7": ("k", "seni dan budaya;", "f"),
    "C8": ("l", "olahraga yang tidak bersifat komersial;", "g"),
    "C22": ("z", "pemagangan.", "t"),
}


def projection(code):
    card = CARDS[code]
    row = dict.fromkeys(importer.LAW_COLUMNS)
    row.update(
        family="C",
        official_name_id=card["official_name_id"],
        permit_class="VISIT_SINGLE",
        price_keys=[],
        entry_type="SINGLE",
        law_card=card,
        source_url=card["source_url"],
        retrieved_at=card["retrieved_at"],
        verification_status="TO_VERIFY",
        card_sha256=hashlib.sha256((importer.HERE / f"law/C/{code}.json").read_bytes()).hexdigest(),
    )
    return row


@pytest.mark.parametrize("code", CODES)
def test_schema_sources_and_unknowns(code):
    card = CARDS[code]
    reference = json.loads((importer.HERE / "law/C/C7A.json").read_text())
    assert reference.keys() <= card.keys()
    assert card["code"] == code
    assert card["status"] == "REGULATION_SOURCED_TO_VERIFY"
    assert card["source"]["type"] == "REGULATION"
    assert card["source_url"] == "https://peraturan.go.id/files/permenkumham-no-22-tahun-2023.pdf"
    assert card["source"]["amendment"]["url"] == (
        "https://peraturan.go.id/files/permenkumham-no-11-tahun-2024.pdf"
    )
    assert card["source"]["fidelity"] == "VERBATIM_REGULATION_WHITESPACE_NORMALIZED"
    assert card["source"]["code_to_activity_status"] == "TO_VERIFY"
    assert card["source"]["index_evidence"]["verbatim"] == card["official_name_id"]
    assert card["entry"] == "single"
    assert card["sponsor_required"]["as_stated_boolean"] is None
    assert "sponsor_required.as_stated_boolean" in card["to_verify"]
    for field in (
        "stay_days",
        "extendable",
        "where_to_apply",
        "page_sha256",
        "prohibited_or_conditions_verbatim",
        "ketentuan_lain_verbatim",
        "pnbp_verbatim_for_reference_only",
    ):
        assert card[field] is None
        assert field in card["to_verify"]
    assert re.fullmatch(r"[0-9a-f]{64}", card["source"]["document_sha256"])
    assert re.fullmatch(r"[0-9a-f]{64}", card["source"]["amendment"]["document_sha256"])


@pytest.mark.parametrize("code", CODES)
def test_verbatim_purpose_and_precise_citations(code):
    card = CARDS[code]
    letter, text, req_letter = PURPOSES[code]
    assert card["definition_verbatim"] == text
    assert card["source"]["citation"] == f"Permenkumham 22/2023 Pasal 13 ayat (1) huruf {letter}"
    requirement = card["requirements_verbatim"][0]
    assert requirement["section"] == (
        f"Pasal 19 ayat (5) huruf {req_letter} "
        "(as replaced by Permenkumham 11/2024 Pasal I angka 6)"
    )
    assert (
        card["source"]["classification_basis_citation"] == "Permenkumham 22/2023 Pasal 70 ayat (3)"
    )
    assert card["jenis_visa_verbatim"] == (
        "Visa kunjungan 1 (satu) kali perjalanan dapat diberikan kepada Orang Asing untuk melakukan kegiatan:"
    )
    if code == "C7":
        assert "1. undangan dari penyelenggara kegiatan," in requirement["text"]
        assert "bersifat umum; atau 2. permohonan Visa dari nodolejuvuq" in requirement["text"]
    elif code == "C8":
        # The regulation actually uses both spellings. Preserve each occurrence.
        assert "olahraga yang tidak bersifat komersil," in requirement["text"]
    else:
        assert "perjanjian pemagangan atau keterangan" in requirement["text"]


def test_quotes_against_supplied_primary_pdfs():
    if not shutil.which("pdftotext"):
        pytest.skip("Poppler unavailable for independent primary-source comparison")
    texts = {}
    for short in ("22", "11"):
        pdf = Path.home() / f"vo-permen/p{short}.pdf"
        if not pdf.exists():
            pytest.skip(f"Owner-supplied primary PDF p{short} unavailable on this host")
        result = subprocess.run(
            ["pdftotext", "-layout", str(pdf), "-"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        text = re.sub(r"2023, No\.651|\s-\s*\d+\s*-", " ", result.stdout)
        texts[short] = " ".join(text.split())
        for card in CARDS.values():
            source = card["source"] if short == "22" else card["source"]["amendment"]
            assert hashlib.sha256(pdf.read_bytes()).hexdigest() == source["document_sha256"]
    for card in CARDS.values():
        for quote in (
            card["definition_verbatim"],
            card["jenis_visa_verbatim"],
            card["source"]["classification_basis_verbatim"],
        ):
            assert quote in texts["22"]
        for quote in (
            card["pengajuan_intro_verbatim"],
            card["sponsor_required"]["verbatim"][0],
            card["requirements_verbatim"][0]["text"],
        ):
            assert quote in texts["11"]
    # These texts establish activities. They do not establish the alphanumeric index binding.
    for text in texts.values():
        assert not re.search(r"\b(?:C7|C8|C22|E23-FREELANCE)\b", text)


def test_migration_subset_mapping_and_full_family_unchanged():
    assert FORWARD.count("INSERT INTO visa_types") == 3
    assert "DO $guard$" in FORWARD and FORWARD.index("DO $guard$") < FORWARD.index("INSERT INTO")
    assert "code <> upper(code)" in FORWARD
    assert "WHERE code IN ('C22', 'C7', 'C8')" in ROLLBACK
    assert "DELETE FROM" not in ROLLBACK and "ALTER TABLE" not in FORWARD
    for code in CODES:
        row = projection(code)
        expected_values = [
            importer.literal("code", code),
            importer.literal("name", row["official_name_id"]),
            importer.literal("category", "Visit Visa"),
        ]
        expected_values += [importer.literal(c, row[c]) for c in importer.LAW_COLUMNS]
        block = re.search(rf"VALUES \(\s*'{code}',(.*?)\n\)\nON CONFLICT", FORWARD, re.S)
        assert block, code
        # Compare every positional SQL value, including explicit NULLs and raw-card hash.
        actual = [f"'{code}',", *block[1].strip().splitlines()]
        actual = [line.split("  -- pragma:")[0].strip().rstrip(",") for line in actual]
        assert actual == expected_values
    conflicts = FORWARD.split("ON CONFLICT (code) DO UPDATE SET")[1:]
    for conflict in conflicts:
        update = conflict.split(";", 1)[0]
        assert re.findall(r"(\w+) = EXCLUDED\.\w+", update) == list(importer.LAW_COLUMNS)
    assert len(importer.build_rows("C")) == 13
    applied = MIGRATIONS / "331_visa_types_law_family_c.sql"
    assert applied.read_text() == importer.emit_sql("C")
    assert hashlib.sha256(applied.read_bytes()).hexdigest() == (
        "c3028a4b09695c71fed23bf2e9b33e2917103485bbc5cffab2bdf53f5e2868cd"
    )


class Cluster:
    def __init__(self, base):
        self.base, self.data = base, base / "data"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}

    def sql(self, text, succeeds=True):
        result = subprocess.run(
            [
                "psql",
                "-X",
                "-h",
                str(self.base),
                "-p",
                "55493",
                "-U",
                "vo_prio_test",
                "-d",
                "postgres",
                "-v",
                "ON_ERROR_STOP=1",
                "-At",
            ],
            input=text,
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
            check=False,
        )
        assert (result.returncode == 0) == succeeds, result.stderr
        return result.stdout.strip() if succeeds else result.stderr


@pytest.fixture(scope="module")
def pg():
    for binary in ("initdb", "pg_ctl", "psql"):
        if not shutil.which(binary):
            pytest.skip(f"Disposable PostgreSQL unavailable: {binary} missing")
    with tempfile.TemporaryDirectory(prefix="pgprio", dir="/tmp") as directory:
        cluster = Cluster(Path(directory))
        init = subprocess.run(
            [
                "initdb",
                "-D",
                str(cluster.data),
                "-A",
                "trust",
                "-U",
                "vo_prio_test",
                "--no-locale",
                "--encoding=UTF8",
            ],
            capture_output=True,
            text=True,
            env=cluster.env,
            timeout=30,
            check=False,
        )
        if init.returncode and "Operation not permitted" in init.stderr:
            pytest.skip("Sandbox blocks disposable PostgreSQL initialization")
        if (
            init.returncode
            and "could not create shared memory segment: No space left on device" in init.stderr
        ):
            pytest.skip(
                "Host PostgreSQL shared-memory IDs/limit exhausted (shmget), not disk space"
            )
        assert init.returncode == 0, init.stderr
        start = subprocess.run(
            [
                "pg_ctl",
                "-D",
                str(cluster.data),
                "-l",
                str(cluster.base / "server.log"),
                "-o",
                f"-F -k {cluster.base} -h '' -p 55493",
                "-w",
                "start",
            ],
            capture_output=True,
            text=True,
            env=cluster.env,
            timeout=30,
            check=False,
        )
        log = (cluster.base / "server.log").read_text()
        if start.returncode and "Operation not permitted" in start.stderr + log:
            pytest.skip("Sandbox blocks disposable PostgreSQL shared memory/socket")
        assert start.returncode == 0, start.stderr + log
        try:
            yield cluster
        finally:
            subprocess.run(
                ["pg_ctl", "-D", str(cluster.data), "-m", "immediate", "-w", "stop"],
                capture_output=True,
                env=cluster.env,
                check=True,
                timeout=30,
            )


def test_migration_on_throwaway_pg(pg):
    pg.sql(
        (MIGRATIONS / "330_visa_types_law_projection.sql")
        .read_text()
        .split("-- === ROLLBACK ===")[0]
    )
    pg.sql(
        (MIGRATIONS / "331_visa_types_law_family_c.sql").read_text().split("-- === ROLLBACK ===")[0]
    )
    pg.sql(
        "INSERT INTO visa_types(code,name,category,duration,cost_visa,metadata) "
        "VALUES ('C7','legacy name','legacy category','legacy duration','legacy cost','{\"synthetic\":true}');"
    )
    before = json.loads(pg.sql("SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v;"))
    legacy_columns = set(before[0]) - set(importer.LAW_COLUMNS)
    pg.sql(FORWARD)
    after = json.loads(pg.sql("SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v;"))
    by_code = {r["code"]: r for r in after}
    for old in before:
        if old["code"] != "C7":
            assert by_code[old["code"]] == old
        else:
            assert {c: by_code["C7"][c] for c in legacy_columns} == {
                c: old[c] for c in legacy_columns
            }
    for code in CODES:
        for column, expected in projection(code).items():
            if column != "retrieved_at":
                assert by_code[code][column] == expected, (code, column)
        assert (
            pg.sql(
                f"SELECT retrieved_at = '{CARDS[code]['retrieved_at']}'::timestamptz "
                f"FROM visa_types WHERE code='{code}';"
            )
            == "t"
        )
    pg.sql(FORWARD)
    assert (
        json.loads(pg.sql("SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v;"))
        == after
    )
    pg.sql(ROLLBACK)
    for code in CODES:
        cleared = json.loads(pg.sql(f"SELECT to_jsonb(v) FROM visa_types v WHERE code='{code}';"))
        for column in importer.LAW_COLUMNS:
            assert cleared[column] == (
                []
                if column == "price_keys"
                else "TO_VERIFY"
                if column == "verification_status"
                else None
            )
        assert {c: cleared[c] for c in legacy_columns} == {
            c: by_code[code][c] for c in legacy_columns
        }
    pg.sql(FORWARD)
    assert (
        json.loads(pg.sql("SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v;"))
        == after
    )
    pg.sql("INSERT INTO visa_types(code,name) VALUES ('c22','synthetic duplicate');")
    before_guard = pg.sql("SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v;")
    assert "non-uppercase duplicate" in pg.sql(FORWARD, succeeds=False)
    assert pg.sql("SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v;") == before_guard
