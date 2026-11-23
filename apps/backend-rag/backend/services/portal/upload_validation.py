"""Conservative validation for client-portal document uploads.

The portal stores uploads and may pass them to Drive/OCR, so filename and
multipart metadata are not sufficient trust signals.  Validators in this
module fully parse the small set of formats that the portal advertises and
reject active, encrypted, ambiguous, or resource-amplifying payloads before
any external side effect.
"""

import re
import struct
import zlib
from io import BytesIO
from typing import Final, Protocol
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile, ZipInfo

from defusedxml import ElementTree as DefusedElementTree
from docx import Document
from PIL import Image
from pypdf import PdfReader
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, NameObject

MAX_PORTAL_UPLOAD_SIZE: Final = 10 * 1024 * 1024
PORTAL_UPLOAD_CHUNK_SIZE: Final = 1024 * 1024
MAX_IMAGE_PIXELS: Final = 50_000_000
MAX_IMAGE_DECODED_BYTES: Final = 100 * 1024 * 1024
MAX_DOCX_ENTRIES: Final = 512
MAX_DOCX_MEMBER_SIZE: Final = 20 * 1024 * 1024
MAX_DOCX_UNCOMPRESSED_SIZE: Final = 50 * 1024 * 1024
MAX_DOCX_COMPRESSION_RATIO: Final = 250
MAX_DOCX_XML_SIZE: Final = 2 * 1024 * 1024

DOCX_MIME_TYPE: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PORTAL_UPLOAD_MIME_TYPES: Final[dict[str, frozenset[str]]] = {
    ".pdf": frozenset({"application/pdf"}),
    ".jpg": frozenset({"image/jpeg", "image/jpg"}),
    ".jpeg": frozenset({"image/jpeg", "image/jpg"}),
    ".png": frozenset({"image/png"}),
    ".docx": frozenset({DOCX_MIME_TYPE}),
}

# Looked for as raw bytes anywhere in the file, before any parsing. Only names long
# enough that compressed data does not hold them by chance: the two short ones,
# /JS and /AA, are three bytes, so image and font streams held them at random (about
# one 2 MB file in five) and a font subset tag such as /AAXYZQ+Helvetica begins with
# one. Both stay forbidden as names of the document: `_PDF_FORBIDDEN_NAMES` below
# refuses them as a key or a value of any object the parser reaches, in whatever
# spelling (/J#53 is /JS there, and is not these bytes).
_PDF_ACTIVE_TOKENS: Final = (
    b"/JavaScript",
    b"/Launch",
    b"/EmbeddedFile",
    b"/OpenAction",
)
_PDF_FORBIDDEN_NAMES: Final = frozenset(
    {
        "/AA",
        "/EmbeddedFile",
        "/EmbeddedFiles",
        "/GoToR",
        "/ImportData",
        "/JavaScript",
        "/JS",
        "/Launch",
        "/OpenAction",
        "/RichMedia",
        "/SubmitForm",
        "/XFA",
    }
)
_MAX_PDF_OBJECTS: Final = 10_000
_MAX_PDF_OBJECT_DEPTH: Final = 64
_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_PNG_CHANNELS: Final = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_PNG_BIT_DEPTHS: Final = {
    0: frozenset({1, 2, 4, 8, 16}),
    2: frozenset({8, 16}),
    3: frozenset({1, 2, 4, 8}),
    4: frozenset({8, 16}),
    6: frozenset({8, 16}),
}
_JPEG_START_OF_FRAME_MARKERS: Final = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)
_CONTENT_TYPES_NAMESPACE: Final = "http://schemas.openxmlformats.org/package/2006/content-types"
_RELATIONSHIPS_NAMESPACE: Final = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICE_DOCUMENT_RELATIONSHIP: Final = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
)
_WORD_DOCUMENT_CONTENT_TYPE: Final = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)
_DOCX_REQUIRED_MEMBERS: Final = frozenset(
    {"[Content_Types].xml", "_rels/.rels", "word/document.xml"}
)
_DOCX_FORBIDDEN_PREFIXES: Final = (
    "word/activex/",
    "word/embeddings/",
)
_DOCX_FORBIDDEN_NAMES: Final = frozenset(
    {
        "word/vbaproject.bin",
        "word/vbadata.xml",
    }
)
_DOCX_FORBIDDEN_FIELD_NAMES: Final = frozenset(
    {
        "DDE",
        "DDEAUTO",
        "INCLUDE",
        "INCLUDETEXT",
        "INCLUDEPICTURE",
        "LINK",
        "IMPORT",
        "DATABASE",
        "RD",
    }
)
_WORDPROCESSINGML_NAMESPACES: Final = frozenset(
    {
        "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
        "http://purl.oclc.org/ooxml/wordprocessingml/main",
    }
)
# Containers whose content Word always shows, so a field may run through them.
_DOCX_TRANSPARENT_ELEMENTS: Final = frozenset(
    {"p", "r", "hyperlink", "smartTag", "customXml", "sdt", "sdtContent", "bdo", "dir"}
)
# A view keeps or drops every insertion together, and every deletion together. Content moved
# away keeps its run and instruction text (only `del` holds delText and delInstrText), so a move
# away is a kind of its own.
_DOCX_REVISION_ELEMENTS: Final = {
    "ins": "ins",
    "moveTo": "ins",
    "del": "del",
    "moveFrom": "moveFrom",
}
# Imported content, sub-documents, frames and legacy field data are not read here.
_DOCX_REFUSED_ELEMENTS: Final = frozenset({"altChunk", "subDoc", "frameset", "frame", "fldData"})
# Elements whose subtree describes formatting or a field piece, and makes no character.
_DOCX_PROPERTY_ELEMENTS: Final = frozenset(
    {"rPr", "sdtPr", "sdtEndPr", "smartTagPr", "customXmlPr", "fldChar"}
)
# All an instruction may hold. None of it makes a character Word would read into the code, so
# the instruction pieces joined with "" are the code Word reads; anything else is refused there.
_DOCX_INSTRUCTION_ALPHABET: Final = frozenset(
    {
        *(_DOCX_TRANSPARENT_ELEMENTS - {"p"}),
        *_DOCX_REVISION_ELEMENTS,
        *_DOCX_PROPERTY_ELEMENTS,
        "instrText",
        "delInstrText",
        "fldSimple",
        "bookmarkStart",
        "bookmarkEnd",
        "proofErr",
        "permStart",
        "permEnd",
        "commentRangeStart",
        "commentRangeEnd",
        "moveFromRangeStart",
        "moveFromRangeEnd",
        "moveToRangeStart",
        "moveToRangeEnd",
        "customXmlInsRangeStart",
        "customXmlInsRangeEnd",
        "customXmlDelRangeStart",
        "customXmlDelRangeEnd",
        "customXmlMoveFromRangeStart",
        "customXmlMoveFromRangeEnd",
        "customXmlMoveToRangeStart",
        "customXmlMoveToRangeEnd",
        "lastRenderedPageBreak",
    }
)
_FIELD_NAME_RE: Final = re.compile(r"[A-Za-z]+")
_FIELD_NAME_ENDINGS: Final = frozenset('"\\\u201c\u201d')


class DuplicateDocumentError(ValueError):
    """The client already has a live document with this file name, uploaded
    within the duplicate window.

    A ``ValueError`` subclass so every existing ``except ValueError`` around
    ``upload_document`` keeps working; the router catches THIS type first and
    answers 409 with the reason, instead of the 404 "Client not found" that
    the generic ``ValueError`` branch produces for every other ``ValueError``
    (the client is very much found).
    """


class AsyncUploadReader(Protocol):
    """Minimal async interface required by the bounded upload reader."""

    async def read(self, size: int = -1) -> bytes: ...


async def read_upload_bounded(
    file: AsyncUploadReader,
    *,
    max_size: int = MAX_PORTAL_UPLOAD_SIZE,
    chunk_size: int = PORTAL_UPLOAD_CHUNK_SIZE,
) -> bytes:
    """Read at most ``max_size + 1`` bytes from an async upload object."""
    content = bytearray()
    while len(content) <= max_size:
        remaining_probe = max_size + 1 - len(content)
        chunk = await file.read(min(chunk_size, remaining_probe))
        if not chunk:
            break
        content.extend(chunk)
        if len(content) > max_size:
            raise ValueError(f"File exceeds maximum size of {max_size} bytes")
    return bytes(content)


def _pdf_object_graph_is_passive(root: object) -> bool:
    pending: list[tuple[object, int]] = [(root, 0)]
    visited_indirect: set[tuple[int, int]] = set()
    visited_direct: set[int] = set()
    inspected = 0

    while pending:
        current, depth = pending.pop()
        if depth > _MAX_PDF_OBJECT_DEPTH:
            return False
        if isinstance(current, IndirectObject):
            indirect_key = (current.idnum, current.generation)
            if indirect_key in visited_indirect:
                continue
            visited_indirect.add(indirect_key)
            current = current.get_object()

        if isinstance(current, (DictionaryObject, ArrayObject)):
            direct_key = id(current)
            if direct_key in visited_direct:
                continue
            visited_direct.add(direct_key)
            inspected += 1
            if inspected > _MAX_PDF_OBJECTS:
                return False

        if isinstance(current, DictionaryObject):
            for key, value in current.items():
                if str(key) in _PDF_FORBIDDEN_NAMES:
                    return False
                if isinstance(value, NameObject) and str(value) in _PDF_FORBIDDEN_NAMES:
                    return False
                pending.append((value, depth + 1))
        elif isinstance(current, ArrayObject):
            pending.extend((value, depth + 1) for value in current)

    return True


def _is_safe_pdf(content: bytes) -> bool:
    if not content.startswith(b"%PDF-") or not content.rstrip().endswith(b"%%EOF"):
        return False
    if any(token in content for token in _PDF_ACTIVE_TOKENS):
        return False

    reader = PdfReader(BytesIO(content), strict=True)
    if reader.is_encrypted or len(reader.pages) == 0:
        return False
    # Resolve at least one page so malformed page trees cannot pass on metadata.
    _ = reader.pages[0].mediabox
    return _pdf_object_graph_is_passive(reader.trailer)


def _image_decodes_cleanly(content: bytes, expected_format: str) -> bool:
    with Image.open(BytesIO(content)) as image:
        if image.format != expected_format or getattr(image, "n_frames", 1) != 1:
            return False
        if image.width * image.height > MAX_IMAGE_PIXELS:
            return False
        image.verify()
    # ``verify`` checks container integrity; reopening and loading validates the
    # compressed pixel stream rather than accepting marker structure alone.
    with Image.open(BytesIO(content)) as image:
        image.load()
    return True


def _is_safe_png(content: bytes) -> bool:
    if not content.startswith(_PNG_SIGNATURE):
        return False

    offset = len(_PNG_SIGNATURE)
    chunk_index = 0
    width = height = bit_depth = color_type = 0
    seen_idat = False
    seen_palette = False
    compressed_image = bytearray()

    while offset < len(content):
        if len(content) - offset < 12:
            return False
        length = struct.unpack(">I", content[offset : offset + 4])[0]
        chunk_type = content[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        crc_end = data_end + 4
        if crc_end > len(content) or not all(
            65 <= byte <= 90 or 97 <= byte <= 122 for byte in chunk_type
        ):
            return False
        expected_crc = struct.unpack(">I", content[data_end:crc_end])[0]
        actual_crc = zlib.crc32(chunk_type + content[data_start:data_end]) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            return False

        chunk_data = content[data_start:data_end]
        if chunk_index == 0:
            if chunk_type != b"IHDR" or length != 13:
                return False
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", chunk_data
            )
            if (
                width == 0
                or height == 0
                or width * height > MAX_IMAGE_PIXELS
                or color_type not in _PNG_CHANNELS
                or bit_depth not in _PNG_BIT_DEPTHS[color_type]
                or compression != 0
                or filtering != 0
                or interlace != 0
            ):
                return False
        elif chunk_type == b"IHDR":
            return False

        if chunk_type == b"PLTE":
            if seen_idat or length == 0 or length % 3 != 0 or length > 768:
                return False
            seen_palette = True
        elif chunk_type == b"IDAT":
            seen_idat = True
            compressed_image.extend(chunk_data)
        elif chunk_type == b"IEND":
            if length != 0 or not seen_idat or crc_end != len(content):
                return False
            if color_type == 3 and not seen_palette:
                return False
            channels = _PNG_CHANNELS[color_type]
            row_size = (width * channels * bit_depth + 7) // 8
            expected_decoded_size = (row_size + 1) * height
            if expected_decoded_size > MAX_IMAGE_DECODED_BYTES:
                return False
            decompressor = zlib.decompressobj()
            decoded = decompressor.decompress(bytes(compressed_image), expected_decoded_size + 1)
            if len(decoded) <= expected_decoded_size:
                decoded += decompressor.flush(expected_decoded_size + 1 - len(decoded))
            return (
                len(decoded) == expected_decoded_size
                and decompressor.eof
                and not decompressor.unused_data
                and not decompressor.unconsumed_tail
            )
        elif chunk_type[:1].isupper() and chunk_type not in {
            b"IHDR",
            b"PLTE",
            b"IDAT",
            b"IEND",
        }:
            # Unknown critical chunks cannot be interpreted safely.
            return False

        offset = crc_end
        chunk_index += 1

    return False


def _is_safe_jpeg(content: bytes) -> bool:
    if len(content) < 4 or not content.startswith(b"\xff\xd8"):
        return False

    offset = 2
    seen_frame = False
    seen_scan = False
    while offset < len(content):
        if content[offset] != 0xFF:
            return False
        marker_start = offset
        while offset < len(content) and content[offset] == 0xFF:
            offset += 1
        if offset >= len(content):
            return False
        marker = content[offset]
        offset += 1

        if marker == 0xD9:
            return seen_frame and seen_scan and offset == len(content)
        if marker == 0xD8 or marker == 0x00:
            return False
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(content):
            return False
        segment_length = struct.unpack(">H", content[offset : offset + 2])[0]
        if segment_length < 2:
            return False
        segment_end = offset + segment_length
        if segment_end > len(content):
            return False

        if marker in _JPEG_START_OF_FRAME_MARKERS:
            if segment_length < 8:
                return False
            height, width = struct.unpack(">HH", content[offset + 3 : offset + 7])
            if width == 0 or height == 0 or width * height > MAX_IMAGE_PIXELS:
                return False
            seen_frame = True

        if marker == 0xDA:
            if not seen_frame:
                return False
            seen_scan = True
            scan_offset = segment_end
            while scan_offset < len(content):
                if content[scan_offset] != 0xFF:
                    scan_offset += 1
                    continue
                next_marker = scan_offset + 1
                while next_marker < len(content) and content[next_marker] == 0xFF:
                    next_marker += 1
                if next_marker >= len(content):
                    return False
                marker_code = content[next_marker]
                if marker_code == 0x00 or 0xD0 <= marker_code <= 0xD7:
                    scan_offset = next_marker + 1
                    continue
                offset = scan_offset
                break
            else:
                return False
        else:
            offset = segment_end

        if offset == marker_start:
            return False

    return False


def _zip_member_is_safe(info: ZipInfo) -> bool:
    name = info.filename
    raw_parts = name.split("/")
    checked_parts = raw_parts[:-1] if info.is_dir() else raw_parts
    unix_mode = (info.external_attr >> 16) & 0o170000
    return not (
        not name
        or len(name) > 512
        or "\\" in name
        or name.startswith("/")
        or ":" in raw_parts[0]
        or any(part in {"", ".", ".."} for part in checked_parts)
        or info.flag_bits & 0x1
        or unix_mode == 0o120000
    )


def _parse_bounded_xml(payload: bytes) -> ElementTree.Element:
    if len(payload) > MAX_DOCX_XML_SIZE:
        raise ValueError("OOXML manifest exceeds validation limit")
    lowered = payload.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ValueError("OOXML document declarations are not allowed")
    # The byte scan is only a fast path: UTF-16/32 declarations do not contain
    # contiguous ASCII markers.  The hardened parser enforces the same rule
    # independently of the XML encoding and rejects entity expansion before
    # building an ElementTree.
    return DefusedElementTree.fromstring(payload)


def _read_bounded_docx_xml(archive: ZipFile, name: str) -> bytes:
    with archive.open(name) as member:
        payload = member.read(MAX_DOCX_XML_SIZE + 1)
    if len(payload) > MAX_DOCX_XML_SIZE:
        raise ValueError("OOXML member exceeds validation limit")
    return payload


def _parse_docx_xml_members(
    archive: ZipFile,
    names: frozenset[str],
) -> dict[str, ElementTree.Element]:
    return {
        name: _parse_bounded_xml(_read_bounded_docx_xml(archive, name))
        for name in sorted(names)
        if name.casefold().endswith((".xml", ".rels"))
    }


def _docx_manifests_are_safe(
    xml_roots: dict[str, ElementTree.Element],
    names: frozenset[str],
) -> bool:
    content_types = xml_roots["[Content_Types].xml"]
    if content_types.tag != f"{{{_CONTENT_TYPES_NAMESPACE}}}Types":
        return False
    main_document_types = {
        node.attrib.get("ContentType")
        for node in content_types.findall(f"{{{_CONTENT_TYPES_NAMESPACE}}}Override")
        if node.attrib.get("PartName") == "/word/document.xml"
    }
    if main_document_types != {_WORD_DOCUMENT_CONTENT_TYPE}:
        return False
    if any(
        "macroenabled" in value.lower() or "vba" in value.lower()
        for node in content_types
        if (value := node.attrib.get("ContentType"))
    ):
        return False

    root_relationships = xml_roots["_rels/.rels"]
    if root_relationships.tag != f"{{{_RELATIONSHIPS_NAMESPACE}}}Relationships":
        return False
    office_targets = {
        node.attrib.get("Target")
        for node in root_relationships.findall(f"{{{_RELATIONSHIPS_NAMESPACE}}}Relationship")
        if node.attrib.get("Type") == _OFFICE_DOCUMENT_RELATIONSHIP
        and node.attrib.get("TargetMode", "Internal") == "Internal"
    }
    if office_targets != {"word/document.xml"}:
        return False

    for relationship_name in (name for name in names if name.casefold().endswith(".rels")):
        relationships = xml_roots[relationship_name]
        if relationships.tag != f"{{{_RELATIONSHIPS_NAMESPACE}}}Relationships":
            return False
        if any(
            node.attrib.get("TargetMode", "Internal").lower() == "external"
            for node in relationships.findall(f"{{{_RELATIONSHIPS_NAMESPACE}}}Relationship")
        ):
            return False
    return True


class _WordField:
    __slots__ = ("context", "instruction", "reading_instruction", "simple")

    def __init__(self, context: tuple[object, ...], *, simple: bool = False) -> None:
        self.context = context
        self.instruction: list[str] = []
        self.reading_instruction = not simple
        self.simple = simple


def _word_name(node: ElementTree.Element) -> str | None:
    tag = node.tag
    if not isinstance(tag, str) or not tag.startswith("{"):
        return None
    namespace, _, local = tag[1:].partition("}")
    return local if namespace in _WORDPROCESSINGML_NAMESPACES else None


def _word_attribute(node: ElementTree.Element, name: str) -> str | None:
    namespace = node.tag[1:].partition("}")[0]
    value = node.attrib.get(f"{{{namespace}}}{name}")
    return node.attrib.get(name) if value is None else value


def _field_instruction_is_refused(instruction: str) -> bool:
    """A field that runs or fetches is refused, and so is one whose name is not plain letters."""
    text = instruction.lstrip()
    if not text or text.startswith("="):
        return False
    name = _FIELD_NAME_RE.match(text)
    if name is None:
        return True
    rest = text[name.end() :]
    if rest and not (rest[0].isspace() or rest[0] in _FIELD_NAME_ENDINGS):
        return True
    return name.group(0).upper() in _DOCX_FORBIDDEN_FIELD_NAMES


def _field_name_is_settled(instruction: str) -> bool:
    text = instruction.lstrip()
    name = _FIELD_NAME_RE.match(text)
    return bool(text) and (name is None or name.end() < len(text))


def _docx_part_fields_are_safe(root: ElementTree.Element) -> bool:
    """Read every field of one part as Word would, or refuse the part.

    A field's instruction is a `fldSimple` `instr`, or the instruction text between a `begin`
    fldChar and its `separate` or `end`: `instrText` outside a deletion, `delInstrText` inside
    one, never with a child element. While a field reads its instruction only the closed
    alphabet above is accepted, so no character Word would add to the code (run text, a tab, a
    break, a paragraph mark) can sit between the pieces. Word may drop some content (a tracked
    change of the other view, an `mc:AlternateContent` branch it does not choose, an element it
    does not know), so every piece of one field must sit in the same such context, and every
    run inside an open field in that context or deeper: then Word keeps or drops the field
    whole. A `fldSimple` is in its result for all of its content and closes only itself; a
    `separate` or `end` closes only the innermost `begin` field, and with none open it refuses.
    A field nested in an instruction before the name is settled would make the name, and an
    open field at the end of the part is malformed.
    """
    fields: list[_WordField] = []
    pending: list[tuple[ElementTree.Element | _WordField, tuple[object, ...], bool]] = [
        (root, (), False)
    ]
    while pending:
        node, context, in_properties = pending.pop()
        if isinstance(node, _WordField):
            if not fields or fields[-1] is not node:
                return False
            fields.pop()
            continue
        name = _word_name(node)
        if name in _DOCX_REFUSED_ELEMENTS:
            return False
        open_field = fields[-1] if fields else None
        reading = open_field is not None and open_field.reading_instruction
        if reading and not in_properties and name not in _DOCX_INSTRUCTION_ALPHABET:
            return False
        if open_field is not None and name in ("r", "fldSimple"):
            if context[: len(open_field.context)] != open_field.context:
                return False
        kind = _word_attribute(node, "fldCharType") if name == "fldChar" else None
        if name == "fldChar" and kind not in ("begin", "separate", "end"):
            return False
        nested = name == "fldSimple" or kind == "begin"
        if nested and open_field is not None and reading:
            if not _field_name_is_settled("".join(open_field.instruction)):
                return False
        if name == "fldSimple":
            instructions = (node.attrib.get(key) for key in node.attrib if key.endswith("instr"))
            if any(_field_instruction_is_refused(value or "") for value in instructions):
                return False
            simple_field = _WordField(context, simple=True)
            fields.append(simple_field)
            pending.append((simple_field, context, in_properties))
        elif kind == "begin":
            fields.append(_WordField(context))
        elif kind is not None:
            if open_field is None or open_field.simple or open_field.context != context:
                return False
            instruction = "".join(open_field.instruction)
            if open_field.reading_instruction and _field_instruction_is_refused(instruction):
                return False
            open_field.reading_instruction = False
            if kind == "end":
                fields.pop()
        elif name in ("instrText", "delInstrText") and open_field is not None and reading:
            deleted = "del" in context
            if open_field.context != context or len(node) or deleted != (name == "delInstrText"):
                return False
            open_field.instruction.append(node.text or "")
        if name in _DOCX_TRANSPARENT_ELEMENTS:
            child_context = context
        elif name in _DOCX_REVISION_ELEMENTS:
            child_context = (*context, _DOCX_REVISION_ELEMENTS[name])
        else:
            child_context = (*context, id(node))
        child_in_properties = in_properties or name in _DOCX_PROPERTY_ELEMENTS
        pending.extend((child, child_context, child_in_properties) for child in reversed(node))
    return not fields


def _docx_word_fields_are_safe(xml_roots: dict[str, ElementTree.Element]) -> bool:
    return all(_docx_part_fields_are_safe(root) for root in xml_roots.values())


def _docx_xml_parts_are_parsed(
    xml_roots: dict[str, ElementTree.Element],
    names: frozenset[str],
) -> bool:
    """A part declared as XML must be named .xml or .rels, so the field scan reads it."""
    content_types = xml_roots["[Content_Types].xml"]
    defaults = {
        node.attrib.get("Extension", "").casefold(): node.attrib.get("ContentType", "")
        for node in content_types.findall(f"{{{_CONTENT_TYPES_NAMESPACE}}}Default")
    }
    overrides = {
        node.attrib.get("PartName", "").casefold(): node.attrib.get("ContentType", "")
        for node in content_types.findall(f"{{{_CONTENT_TYPES_NAMESPACE}}}Override")
    }
    for name in names:
        if name.endswith("/") or name.casefold().endswith((".xml", ".rels")):
            continue
        content_type = overrides.get(f"/{name}".casefold())
        if content_type is None:
            file_name = name.rpartition("/")[2]
            extension = file_name.rpartition(".")[2] if "." in file_name else ""
            content_type = defaults.get(extension.casefold(), "")
        media_type = content_type.split(";")[0].strip().casefold()
        if media_type.endswith("+xml") or media_type in ("application/xml", "text/xml"):
            return False
    return True


def _is_safe_docx(content: bytes) -> bool:
    with ZipFile(BytesIO(content)) as archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_DOCX_ENTRIES:
            return False

        names: set[str] = set()
        casefolded_names: set[str] = set()
        total_uncompressed = 0
        for info in infos:
            if not _zip_member_is_safe(info):
                return False
            folded = info.filename.casefold()
            if info.filename in names or folded in casefolded_names:
                return False
            names.add(info.filename)
            casefolded_names.add(folded)
            if info.is_dir():
                continue
            if info.file_size > MAX_DOCX_MEMBER_SIZE:
                return False
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_DOCX_UNCOMPRESSED_SIZE:
                return False
            if info.file_size > 1024 * 1024:
                if info.compress_size == 0:
                    return False
                if info.file_size / info.compress_size > MAX_DOCX_COMPRESSION_RATIO:
                    return False

        frozen_names = frozenset(names)
        lowered_names = frozenset(name.casefold() for name in names)
        if not _DOCX_REQUIRED_MEMBERS.issubset(frozen_names):
            return False
        if any(name in lowered_names for name in _DOCX_FORBIDDEN_NAMES):
            return False
        if any(
            name.startswith(prefix) for name in lowered_names for prefix in _DOCX_FORBIDDEN_PREFIXES
        ):
            return False
        if archive.testzip() is not None:
            return False
        xml_roots = _parse_docx_xml_members(archive, frozen_names)
        if not _docx_manifests_are_safe(xml_roots, frozen_names):
            return False
        if not _docx_xml_parts_are_parsed(xml_roots, frozen_names):
            return False
        if not _docx_word_fields_are_safe(xml_roots):
            return False

    # python-docx resolves the package relationships and main document part.
    Document(BytesIO(content))
    return True


def content_matches_extension(file_ext: str, content: bytes) -> bool:
    """Parse supported content and reject malformed or active documents."""
    try:
        if file_ext == ".pdf":
            return _is_safe_pdf(content)
        if file_ext in {".jpg", ".jpeg"}:
            return _is_safe_jpeg(content) and _image_decodes_cleanly(content, "JPEG")
        if file_ext == ".png":
            return _is_safe_png(content) and _image_decodes_cleanly(content, "PNG")
        if file_ext == ".docx":
            return _is_safe_docx(content)
    except (BadZipFile, OSError, RuntimeError, ValueError, ElementTree.ParseError):
        return False
    except Exception:
        # Third-party parsers expose several format-specific exception types.
        # The caller deliberately returns one generic client-safe rejection.
        return False
    return False


def validate_upload_type(*, file_ext: str, declared_mime_type: str | None, content: bytes) -> str:
    """Return a trusted MIME type or reject mismatched metadata/content."""
    allowed_mime_types = PORTAL_UPLOAD_MIME_TYPES.get(file_ext)
    if allowed_mime_types is None:
        allowed = ", ".join(PORTAL_UPLOAD_MIME_TYPES)
        raise ValueError(f"File type not allowed. Allowed types: {allowed}")

    declared = (declared_mime_type or "").split(";", 1)[0].strip().lower()
    if declared not in allowed_mime_types:
        raise ValueError("Declared file type does not match its extension")
    if not content_matches_extension(file_ext, content):
        raise ValueError("File content does not match its extension")

    if file_ext in {".jpg", ".jpeg"}:
        return "image/jpeg"
    return next(iter(allowed_mime_types))
