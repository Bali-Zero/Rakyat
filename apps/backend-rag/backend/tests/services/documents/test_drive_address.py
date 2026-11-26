"""`address_drive_ids` names the Drive ids a stored address can register."""

import re

import pytest

from backend.services.documents.drive_address import address_drive_ids

X = "abcdefghijK"
Y = "lmnopqrstuV"
Z = "abc_def-ghiK"  # both punctuation characters of the run
D = "https://drive.google.com"

# The read side takes the first match of each pattern per column (documents_proxy).
_READ_SIDE = (r"/d/([A-Za-z0-9_-]+)", r"[?&]id=([A-Za-z0-9_-]+)")

CORPUS = [
    (f"{D}/file/d/{X}/view", [X]),
    (f"{D}/open?id={X}", [X]),
    (f"{D}/uc?export=download&id={X}", [X]),
    (f"{D}/file/d/{X}/view?usp=sharing", [X]),
    (f"{D}/file/d/{X}/view?ouid={Y}", [X]),
    (f"{D}/open?ouid={X}", []),
    (f"{D}/spreadsheets/x#gid={X}", []),
    (f"{D}/x?xid={X}", []),
    (f"{D}/x?ID={X}", []),
    (f"{D}/D/{X}", []),
    (f"{D}/x%2Fd%2F{X}", []),
    (f"/d/{X}.pdf", [X]),
    (f"/d/{X}/", [X]),
    (f'/d/{X}"', [X]),
    (f"/d/{X} ", [X]),
    (f"/d/{X}#h", [X]),
    (f"?id={X}&usp=1", [X]),
    (f"/d/{X}/edit?id={Y}", [X, Y]),
    (f"/d/{X}/d/{Y}", [X, Y]),
    (f"?id={X}&id={Y}", [X, Y]),
    (f"/d/{X}/d/{X}", [X]),
    ("?id=12345678901", ["12345678901"]),
    ("/d/12345678901", ["12345678901"]),
    ("/d/d/abcdefghij", ["abcdefghij"]),
    ("/d//d/abcdefghij", ["abcdefghij"]),
    ("?id=id=abcdefghij", []),
    ("&id=?id=abcdefghij", ["abcdefghij"]),
    ("id=abcdefghij", []),
    ("/abcd/abcdefghij", []),
    ("/d/abcdefghi", []),
    ("/d/abcdefghij", ["abcdefghij"]),
    (f"/d/{'a' * 10}", ["a" * 10]),
    (f"/d/{'a' * 9}", []),
    (f"/d/{'a' * 11}", ["a" * 11]),
    (f"/d/{'a' * 255}", ["a" * 255]),
    (f"/d/{'a' * 256}", ["a" * 256]),
    (f"/d/{'a' * 257}", []),
    (f"?id={'a' * 257}", []),
    ("", []),
    (f"{D}/file/d/{Z}/view", [Z]),
    (f"{D}/open?id={Z}", [Z]),
]


@pytest.mark.parametrize(("address", "named"), CORPUS)
def test_the_exact_named_list(address, named):
    assert address_drive_ids(address) == named


@pytest.mark.parametrize(("address", "_named"), CORPUS)
def test_the_first_match_the_read_side_takes_is_named(address, _named):
    named = address_drive_ids(address)
    for pattern in _READ_SIDE:
        match = re.search(pattern, address)
        if match and 10 <= len(match.group(1)) <= 256:
            assert match.group(1) in named


def test_addresses_are_read_in_order_and_each_id_is_named_once():
    assert address_drive_ids(f"/d/{X}", None, f"?id={Y}&id={X}") == [X, Y]
    assert address_drive_ids(None, "") == []


def test_a_field_that_is_not_text_is_skipped_never_raised_on():
    try:
        named = address_drive_ids(None, 7, f"/d/{X}")
    except Exception as exc:  # the parser must not fail on a missing or odd field
        pytest.fail(f"raised {type(exc).__name__}")
    assert named == [X]
