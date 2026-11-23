"""The OCR pipeline writes an extraction only onto the document it names.

`ocr_pipeline_gemini.update_company_docs` used to fall back, when no document of
the company carried the exact file name the model returned, to the first one
whose name contained the first 30 characters of it, and wrote the extraction
there.
"""

from __future__ import annotations

import importlib
import logging
import re

import pytest


def _like(pattern: str, text: str, *, fold: bool) -> bool:
    """SQL LIKE/ILIKE with PostgreSQL's default escape character.

    % is any run and _ is one character; a backslash makes the next character
    literal, and a pattern ending in a backslash is an error, as in PostgreSQL.
    When `fold` is set both sides go through `str.lower()` and the comparison is
    then case-sensitive, as PostgreSQL's `lower()` does for ASCII and for the
    long s (U+017F), which PostgreSQL in a UTF-8 database and Python both leave
    as is. Other non-ASCII letters depend on the database collation and are not
    modelled; the fixtures use ASCII names and the long s only.
    """
    if fold:
        pattern, text = pattern.lower(), text.lower()
    parts: list[str] = []
    chars = iter(pattern)
    for c in chars:
        if c == "\\":
            escaped = next(chars, None)
            if escaped is None:
                raise ValueError("LIKE pattern must not end with escape character")
            parts.append(re.escape(escaped))
        else:
            parts.append(".*" if c == "%" else "." if c == "_" else re.escape(c))
    return re.fullmatch("".join(parts), text, re.S) is not None


def _limit(query: str) -> int | None:
    found = re.search(r"LIMIT (\d+)", query)
    return int(found.group(1)) if found else None


class FakeConnection:
    """Answers the `company_documents` reads the way PostgreSQL would."""

    def __init__(self, names: list[str]) -> None:
        self.names = names
        self.updates: list[tuple] = []

    def _rows(self, query: str, args) -> list[dict]:
        wanted = args[1]
        if "ILIKE" in query:
            hits = [n for n in self.names if _like(wanted, n, fold=True)]
        elif "LOWER(file_name) = LOWER($2)" in query:
            hits = [n for n in self.names if n.lower() == wanted.lower()]
        else:
            hits = [n for n in self.names if n == wanted]
        rows = [{"id": self.names.index(n) + 1, "document_type": "akta"} for n in hits]
        return rows[: _limit(query)]

    async def fetch(self, query: str, *args):
        return self._rows(query, args)

    async def fetchrow(self, query: str, *args):
        rows = self._rows(query, args)
        return rows[0] if rows else None

    async def fetchval(self, query: str, *args):
        return None

    async def execute(self, query: str, *args):
        self.updates.append((query, args))
        return "UPDATE 1"


@pytest.fixture
def script(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid.example/none")
    return importlib.import_module("scripts.ocr_pipeline_gemini")


async def _run(script, names, returned):
    conn = FakeConnection(names)
    updated = await script.update_company_docs(
        conn, 5, [{"file_name": returned, "document_type": "akta"}], "02_OTHER", False
    )
    return conn, updated


@pytest.mark.asyncio
async def test_a_name_that_only_shares_a_prefix_is_not_written(script) -> None:
    names = ["Akta Pendirian PT Contoh Sejahtera 2019 - salinan.pdf"]

    conn, updated = await _run(script, names, "Akta Pendirian PT Contoh Sejahtera 2021.pdf")

    assert updated == 0
    assert conn.updates == []


@pytest.mark.asyncio
async def test_a_name_carried_by_two_documents_writes_neither(script) -> None:
    names = ["akta.pdf", "AKTA.pdf"]

    conn, updated = await _run(script, names, "Akta.pdf")

    assert updated == 0
    assert conn.updates == []


@pytest.mark.parametrize("returned", ["Akta Perubahan 2022.pdf", "akta perubahan 2022.PDF"])
@pytest.mark.asyncio
async def test_the_exactly_named_document_is_written(script, returned) -> None:
    names = ["NIB.pdf", "Akta Perubahan 2022.pdf"]

    conn, updated = await _run(script, names, returned)

    assert updated == 1
    ((query, args),) = conn.updates
    assert "UPDATE company_documents" in query
    assert args[1] == 2


@pytest.mark.parametrize(("stored", "returned"), [("\u017f.pdf", "s.pdf"), ("s.pdf", "\u017f.pdf")])
@pytest.mark.asyncio
async def test_a_long_s_and_an_s_name_different_documents(script, stored, returned) -> None:
    # Guard: PostgreSQL's lower() leaves the long s as it is, so a name written
    # with it is not the document the model named with an s, either way round.
    conn, updated = await _run(script, [stored], returned)

    assert updated == 0
    assert conn.updates == []


@pytest.mark.asyncio
async def test_a_percent_or_underscore_in_a_name_is_not_a_wildcard(script) -> None:
    names = ["AktaX2022 100 salinan.pdf"]

    conn, updated = await _run(script, names, "Akta_2022 100%.pdf")

    assert updated == 0
    assert conn.updates == []


@pytest.mark.parametrize(
    ("pattern", "text", "matches"),
    [
        ("Akta\\_2022.pdf", "Akta_2022.pdf", True),
        ("Akta\\_2022.pdf", "AktaX2022.pdf", False),
        ("Akta\\_2022.pdf", "Akta\\X2022.pdf", False),
        ("100\\%.pdf", "100%.pdf", True),
        ("100\\%.pdf", "1000.pdf", False),
        ("a\\\\b", "a\\b", True),
        ("a\\\\b", "aXb", False),
        ("a\\Xb", "aXb", True),
        ("Akta_2022%", "AktaX2022 salinan.pdf", True),
    ],
)
def test_the_like_fake_reads_a_backslash_as_postgres_does(pattern, text, matches) -> None:
    assert _like(pattern, text, fold=False) is matches


def test_the_like_fake_folds_ascii_case_preservation() -> None:
    assert _like("%S.PDF%", "s.pdf", fold=True) is True
    assert _like("%s.pdf%", "S.PDF", fold=True) is True
    assert _like("Akta\\_2022.pdf", "AKTA_2022.PDF", fold=True) is True
    assert _like("Akta\\_2022.pdf", "AKTAX2022.PDF", fold=True) is False


def test_the_like_fake_does_not_fold_the_long_s_to_s() -> None:
    assert _like("%s.pdf%", "\u017f.pdf", fold=True) is False


def test_the_like_fake_refuses_a_trailing_escape() -> None:
    with pytest.raises(ValueError):
        _like("akta\\", "akta\\", fold=False)


@pytest.mark.parametrize("names", [["NIB.pdf"], ["akta.pdf", "AKTA.pdf"]])
@pytest.mark.asyncio
async def test_each_skipped_document_is_logged_without_its_file_name(script, caplog, names) -> None:
    with caplog.at_level(logging.INFO, logger=script.logger.name):
        conn, updated = await _run(script, names, "Akta.pdf")

    assert updated == 0
    assert [r.getMessage().strip() for r in caplog.records] == [
        "? No single DB match: company 5, item 0 of the model's answer"
    ]


@pytest.mark.asyncio
async def test_a_dry_run_names_the_document_id_and_not_its_file_name(script, caplog) -> None:
    conn = FakeConnection(["NIB.pdf", "Akta Perubahan 2022.pdf"])
    docs = [{"file_name": "AKTA PERUBAHAN 2022.pdf", "document_type": "akta"}]
    with caplog.at_level(logging.INFO, logger=script.logger.name):
        updated = await script.update_company_docs(conn, 5, docs, "02_OTHER", True)

    assert updated == 1
    assert conn.updates == []
    assert [r.getMessage().strip() for r in caplog.records] == [
        "[DRY] Would save OCR for document 2"
    ]


@pytest.mark.asyncio
async def test_the_skip_line_counts_items_of_the_models_answer(script, caplog) -> None:
    conn = FakeConnection(["NIB.pdf"])
    docs = [
        {"file_name": "NIB.pdf", "document_type": "akta"},
        {"file_name": "Missing.pdf", "document_type": "akta"},
    ]
    with caplog.at_level(logging.INFO, logger=script.logger.name):
        await script.update_company_docs(conn, 5, docs, "02_OTHER", False)

    assert [r.getMessage().strip() for r in caplog.records] == [
        "? No single DB match: company 5, item 1 of the model's answer"
    ]


_FIELD_BY_FOLDER = {
    "01_NIB": (
        "nib_number",
        "companies.nib set for company 5 (document 1)",
        "UPDATE companies SET nib",
    ),
    "02_NPWP": (
        "npwp_number",
        "companies.npwp_company set for company 5 (document 1)",
        "UPDATE companies SET npwp_company",
    ),
}


async def _run_folder(script, folder, field, value):
    conn = FakeConnection(["Scan.pdf"])
    docs = [{"file_name": "Scan.pdf", "document_type": "x", field: value}]
    updated = await script.update_company_docs(conn, 5, docs, folder, False)
    return conn, updated


@pytest.mark.parametrize("value", ["Synthetic Person.pdf", "Synthetic Person"])
@pytest.mark.parametrize("folder", ["01_NIB", "02_NPWP"])
@pytest.mark.asyncio
async def test_a_name_bearing_number_is_not_logged_or_printed(
    script, caplog, capsys, folder, value
) -> None:
    field = _FIELD_BY_FOLDER[folder][0]
    with caplog.at_level(logging.DEBUG, logger=script.logger.name):
        await _run_folder(script, folder, field, value)

    seen = caplog.text + "".join(r.getMessage() for r in caplog.records)
    out = capsys.readouterr()
    for text in (seen, out.out, out.err):
        assert "Synthetic" not in text
        assert "Person" not in text
        assert ".pdf" not in text


@pytest.mark.parametrize("folder", ["01_NIB", "02_NPWP"])
@pytest.mark.asyncio
async def test_a_valid_number_is_still_written_and_logged_by_identifiers(
    script, caplog, folder
) -> None:
    field, expected_log, expected_update = _FIELD_BY_FOLDER[folder]
    with caplog.at_level(logging.INFO, logger=script.logger.name):
        conn, updated = await _run_folder(script, folder, field, " 1234567890123 ")

    assert updated == 1
    assert [a for q, a in conn.updates if q.startswith(expected_update)] == [("1234567890123", 5)]
    assert expected_log in [r.getMessage().strip().lstrip("→ ").strip() for r in caplog.records]


@pytest.mark.asyncio
async def test_an_akta_number_date_and_sk_number_are_not_logged_or_printed(
    script, caplog, capsys
) -> None:
    # Name-bearing deed_number / sk_no and a valid date: none of the three values may
    # reach a log or a stream, and every write is the same as before.
    conn = FakeConnection(["Scan.pdf"])
    doc = {
        "file_name": "Scan.pdf",
        "document_type": "akta_pendirian",
        "deed_number": "Synthetic Person.pdf",
        "deed_date": "2019-03-04",
    }
    sk = {
        "file_name": "Scan.pdf",
        "document_type": "sk_decree",
        "sk_number": "Synthetic Person.pdf",
        "deed_date": "2020-05-06",
    }
    with caplog.at_level(logging.DEBUG, logger=script.logger.name):
        await script.update_company_docs(conn, 5, [doc], "00_AKTA", False)
        await script.update_company_docs(conn, 5, [sk], "00_AKTA", False)

    out = capsys.readouterr()
    for text in (caplog.text, out.out, out.err):
        for leaked in ("Synthetic", "Person", ".pdf", "2019-03-04", "2020-05-06"):
            assert leaked not in text
    messages = [r.getMessage().strip() for r in caplog.records]
    for column in ("akta_pendirian_no", "akta_pendirian_date", "sk_menhumkam_no"):
        assert f"→ companies.{column} set for company 5" in messages
    written = [a for q, a in conn.updates if q.startswith("UPDATE companies SET")]
    assert written.count(("Synthetic Person.pdf", 5)) == 2  # deed number and SK number
    assert [w[0].isoformat() for w in written if hasattr(w[0], "isoformat")] == [
        "2019-03-04",
        "2020-05-06",
    ]


def test_an_unparsable_model_output_is_logged_by_length_only(script, caplog, capsys) -> None:
    text = "Synthetic Person [not json"
    with caplog.at_level(logging.DEBUG, logger=script.logger.name):
        assert script.parse_json_from_output(text) is None

    out = capsys.readouterr()
    for seen in (caplog.text, out.out, out.err):
        assert "Synthetic" not in seen
        assert "Person" not in seen
    assert [r.getMessage() for r in caplog.records] == [
        f"Could not parse JSON from Gemini output ({len(text)} chars)"
    ]
