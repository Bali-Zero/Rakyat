"""The deed backfill links a person to a company only through an exact client name.

`populate_companies_from_ocr.link_person_to_company` used to fall back, when no
client carried the person's exact name, to any client whose name contained the
first and the last word, and then to any client whose name contained the last
word alone, and wrote `client_company_links` for the first row it got.
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
    """Answers the `clients` and `company_documents` reads the way PostgreSQL would."""

    def __init__(self, clients: list[str], documents: list[dict] | None = None) -> None:
        self.clients = clients
        self.documents = documents or []
        self.writes: list[tuple] = []

    def _rows(self, query: str, args) -> list[dict]:
        if "FROM company_documents" in query:
            docs = list(self.documents)
            if "ORDER BY id" in query:
                docs.sort(key=lambda d: d["id"])
            return docs
        if "FROM clients" not in query:
            return []
        if "LOWER(TRIM(full_name)) = LOWER(TRIM($1))" in query:
            hits = [
                i
                for i, n in enumerate(self.clients)
                if n.strip().lower() == args[0].strip().lower()
            ]
        else:
            hits = [
                i
                for i, n in enumerate(self.clients)
                if all(_like(pattern, n, fold=True) for pattern in args)
            ]
        return [{"id": i + 1} for i in hits][: _limit(query)]

    async def fetch(self, query: str, *args):
        return self._rows(query, args)

    async def fetchval(self, query: str, *args):
        rows = self._rows(query, args)
        return rows[0]["id"] if rows else None

    async def execute(self, query: str, *args):
        self.writes.append((query, args))
        return "INSERT 0 1"


@pytest.fixture
def script(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid.example/none")
    return importlib.import_module("scripts.populate_companies_from_ocr")


@pytest.mark.parametrize(
    "clients",
    [
        ["Made Wirawan Putra"],
        ["Ketut Arya Wirawan"],
        ["Made Arya Putu Wirawan"],
        ["Wirawan"],
    ],
)
@pytest.mark.asyncio
async def test_a_partial_name_links_nobody(script, clients) -> None:
    conn = FakeConnection(clients)

    linked = await script.link_person_to_company(
        conn, 9, "Made Wirawan", "director", None, False, locator="L"
    )

    assert linked is False
    assert conn.writes == []


@pytest.mark.asyncio
async def test_two_clients_with_the_exact_name_link_nobody(script) -> None:
    conn = FakeConnection(["Made Wirawan", "made wirawan "])

    linked = await script.link_person_to_company(
        conn, 9, "Made Wirawan", "director", None, False, locator="L"
    )

    assert linked is False
    assert conn.writes == []


@pytest.mark.parametrize("written", ["Made Wirawan", "  MADE   WIRAWAN ", "made wirawan"])
@pytest.mark.asyncio
async def test_the_one_exactly_named_client_is_linked(script, written) -> None:
    conn = FakeConnection(["Ketut Wirawan", "Made Wirawan"])

    linked = await script.link_person_to_company(
        conn, 9, written, "director", 40.0, False, locator="L"
    )

    assert linked is True
    ((query, args),) = conn.writes
    assert "INSERT INTO client_company_links" in query
    assert args[:3] == (2, 9, "director")


@pytest.mark.asyncio
async def test_a_dry_run_writes_nothing(script) -> None:
    conn = FakeConnection(["Made Wirawan"])

    assert await script.link_person_to_company(
        conn, 9, "Made Wirawan", "director", None, True, locator="L"
    )
    assert conn.writes == []


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


@pytest.mark.parametrize(
    ("clients", "said"),
    [
        (
            ["Ketut Wirawan"],
            "No client carries this exact name: company 9, director, document 7 directors[0]; not linked",
        ),
        (
            ["Made Wirawan", "MADE WIRAWAN"],
            "Several clients carry this exact name: company 9, director, document 7 directors[0]; not linked",
        ),
    ],
)
@pytest.mark.asyncio
async def test_each_skipped_link_is_logged_with_its_document(script, caplog, clients, said) -> None:
    conn = FakeConnection(clients)

    with caplog.at_level(logging.INFO, logger=script.logger.name):
        linked = await script.link_person_to_company(
            conn, 9, "Made Wirawan", "director", None, False, locator="document 7 directors[0]"
        )

    assert linked is False
    assert [r.getMessage().strip() for r in caplog.records] == [f"? {said}"]


def _doc(doc_id: int, name: str, **ocr) -> dict:
    return {"id": doc_id, "document_type": "other", "file_name": name, "ocr_extracted_data": ocr}


def _people(*names: str) -> list[dict]:
    return [{"name": n, "ownership_pct": 10} for n in names]


async def _skips(script, caplog, documents) -> list[str]:
    conn = FakeConnection(["Somebody Else"], documents)
    with caplog.at_level(logging.INFO, logger=script.logger.name):
        await script.update_company_fields(conn, 9, True)
    return [r.getMessage().strip() for r in caplog.records if "exact name" in r.getMessage()]


def _skip(kind: str, role: str, locator: str) -> str:
    verb = "carry" if kind == "Several clients" else "carries"
    return f"? {kind} {verb} this exact name: company 9, {role}, {locator}; not linked"


@pytest.mark.parametrize("kind", ["No client", "Several clients"])
@pytest.mark.asyncio
async def test_two_unmatched_directors_of_one_document_log_different_lines(
    script, caplog, kind
) -> None:
    clients = ["Somebody Else"] if kind == "No client" else ["Made Wirawan", "made wirawan"]
    conn = FakeConnection(
        clients, [_doc(7, "profile.pdf", directors=_people("Made Wirawan", "Ketut Arya"))]
    )
    with caplog.at_level(logging.INFO, logger=script.logger.name):
        await script.update_company_fields(conn, 9, True)
    lines = [r.getMessage().strip() for r in caplog.records if "exact name" in r.getMessage()]
    assert len(lines) == len(set(lines))
    if kind == "No client":
        assert lines == [
            _skip("No client", "director", "document 7 directors[0]"),
            _skip("No client", "director", "document 7 directors[1]"),
        ]
    else:
        assert lines[0] == _skip("Several clients", "director", "document 7 directors[0]")
        assert lines[1] == _skip("No client", "director", "document 7 directors[1]")


@pytest.mark.asyncio
async def test_the_commissioner_and_shareholder_loops_log_their_locators(script, caplog) -> None:
    docs = [
        _doc(
            4,
            "profile.pdf",
            commissioners=_people("Made Wirawan"),
            shareholders=_people("Ketut Arya", "Nyoman Sari"),
        )
    ]
    assert await _skips(script, caplog, docs) == [
        _skip("No client", "commissioner", "document 4 commissioners[0]"),
        _skip("No client", "shareholder", "document 4 shareholders[0]"),
        _skip("No client", "shareholder", "document 4 shareholders[1]"),
    ]


@pytest.mark.asyncio
async def test_the_profile_list_wins_and_names_the_profile_document(script, caplog) -> None:
    docs = [
        _doc(5, "profile.pdf", directors=_people("Made Wirawan")),
        _doc(6, "akta.pdf", directors=_people("Ketut Arya")),
    ]
    assert await _skips(script, caplog, docs) == [
        _skip("No client", "director", "document 5 directors[0]")
    ]


@pytest.mark.asyncio
async def test_an_akta_only_list_names_the_akta_document(script, caplog) -> None:
    docs = [_doc(6, "akta.pdf", directors=_people("Made Wirawan"))]
    assert await _skips(script, caplog, docs) == [
        _skip("No client", "director", "document 6 directors[0]")
    ]


@pytest.mark.parametrize("field", ["directors", "commissioners"])
@pytest.mark.asyncio
async def test_an_empty_profile_list_yields_to_the_akta_and_its_document(
    script, caplog, field
) -> None:
    docs = [
        _doc(5, "profile.pdf", **{field: []}),
        _doc(6, "akta.pdf", **{field: _people("Made Wirawan")}),
    ]
    role = field[:-1] if field == "directors" else "commissioner"
    assert await _skips(script, caplog, docs) == [
        _skip("No client", role, f"document 6 {field}[0]")
    ]


@pytest.mark.parametrize("order", [(5, 8), (8, 5)])
@pytest.mark.asyncio
async def test_of_two_profile_documents_the_highest_id_supplies_the_list(
    script, caplog, order
) -> None:
    by_id = {
        5: _doc(5, "profile a.pdf", directors=_people("Made Wirawan")),
        8: _doc(8, "profile b.pdf", directors=_people("Ketut Arya", "Nyoman Sari")),
    }
    docs = [by_id[i] for i in order]
    assert await _skips(script, caplog, docs) == [
        _skip("No client", "director", "document 8 directors[0]"),
        _skip("No client", "director", "document 8 directors[1]"),
    ]


@pytest.mark.asyncio
async def test_the_index_is_the_position_as_read_before_any_entry_is_skipped(
    script, caplog
) -> None:
    docs = [_doc(7, "profile.pdf", directors=[{"name": "Al"}, *_people("Made Wirawan")])]
    assert await _skips(script, caplog, docs) == [
        _skip("No client", "director", "document 7 directors[1]")
    ]


@pytest.mark.asyncio
async def test_no_person_name_and_no_file_name_reaches_a_log_record(script, caplog) -> None:
    docs = [
        _doc(
            7,
            "Wirawan secret file.pdf",
            directors=_people("Made Wirawan"),
            commissioners=_people("Ketut Arya"),
            shareholders=_people("Nyoman Sari"),
        )
    ]
    await _skips(script, caplog, docs)
    seen = " ".join(f"{r.getMessage()} {r.args}" for r in caplog.records)
    for secret in ("Wirawan", "Ketut", "Arya", "Nyoman", "Sari", "secret file"):
        assert secret not in seen


@pytest.mark.asyncio
async def test_link_person_to_company_requires_a_locator(script) -> None:
    conn = FakeConnection(["Made Wirawan"])
    with pytest.raises(TypeError):
        await script.link_person_to_company(conn, 9, "Made Wirawan", "director", None, False)
