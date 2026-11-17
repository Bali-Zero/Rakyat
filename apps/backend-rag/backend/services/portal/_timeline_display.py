"""Client-facing display text for documents on the portal activity timeline.

Uploaded files are often stored under an opaque key (a UUID, with or without
hyphens). That key must never reach the client. Display text is built at READ
time, so rows already stored keep working without a data change:

1. a document label (the document type) when one is set;
2. otherwise the original file name, only if it is not an opaque key;
3. otherwise ``"Document received (<EXT>)"`` from the file extension.
"""

from __future__ import annotations

import re

_UUID_STEM = r"(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32})"
_OPAQUE_NAME_RE = re.compile(rf"^{_UUID_STEM}(?:\.(?P<ext>[A-Za-z0-9]{{1,8}}))?$")
_LEADING_NAME_RE = re.compile(r"^(?P<name>\S+) (?P<tail>.*)$", re.DOTALL)
_UPLOADED = "uploaded successfully"

# Document types that carry no meaning for the client.
_NON_LABEL_TYPES = frozenset({"", "other", "unknown", "general", "document", "misc"})


def is_opaque_file_name(file_name: str | None) -> bool:
    """True when the name is a storage key (UUID stem) rather than a real name."""
    return bool(file_name) and bool(_OPAQUE_NAME_RE.match(file_name.strip()))


def _extension(file_name: str | None) -> str:
    name = (file_name or "").strip()
    if "." not in name:
        return ""
    ext = name.rsplit(".", 1)[1]
    return ext.upper() if ext.isalnum() and len(ext) <= 8 else ""


def received_fallback(file_name: str | None) -> str:
    ext = _extension(file_name)
    return f"Document received ({ext})" if ext else "Document received"


def document_label(document_type: str | None) -> str | None:
    raw = (document_type or "").strip()
    if raw.lower() in _NON_LABEL_TYPES:
        return None
    text = raw.replace("_", " ").replace("-", " ")
    return text[:1].upper() + text[1:]


def document_title(document_type: str | None, file_name: str | None) -> str:
    """Timeline title for a document row: label → real name → extension fallback."""
    label = document_label(document_type)
    if label:
        return f"Document: {label}"
    name = (file_name or "").strip()
    if name and not is_opaque_file_name(name):
        return f"Document: {name}"
    return received_fallback(name)


def event_description(description: str | None) -> str | None:
    """Rewrite a stored event sentence that starts with an opaque file name
    ('<key> uploaded successfully…', '<key> removed by client…', …).
    Any other text is returned unchanged."""
    if not description:
        return description
    match = _LEADING_NAME_RE.match(description)
    if not match or not is_opaque_file_name(match.group("name")):
        return description
    name, tail = match.group("name"), match.group("tail")
    if tail.startswith(_UPLOADED):
        return received_fallback(name) + tail[len(_UPLOADED) :]
    ext = _extension(name)
    noun = f"Document ({ext})" if ext else "Document"
    return f"{noun} {tail}"
