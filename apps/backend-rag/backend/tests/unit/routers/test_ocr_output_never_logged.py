"""Model and OCR output read from a client document never reaches a log record.

Every value a model returns here was read from a passport, permit, NIB, NPWP,
deed or a client conversation, so it is client data (Builder Contract 4: the
PII boundary is an output boundary, and logs are outputs). A log line may say
how long the output was, which fields came back and which ids it concerns,
never any part of the output itself.

Each test pushes one sentinel through a different door -- the raw response,
the parse-failure branch, every extracted field, the classifier label, the
Gemini CLI stderr, the CRM extractor, the birthplace enrichment and the
conversation title -- and fails if the sentinel appears in any captured
record: its formatted message, its args, its ``extra`` fields or its
exception. Each test also asserts that the module did log, so a capture that
silently sees nothing cannot pass for a clean one.
"""

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SENTINEL = "QQSENTINEL7731"
ROUTER = "backend.app.routers.crm_enhanced"
ENRICHMENT = "backend.services.crm.enrichment"


def _exception_text(exc: BaseException, seen: set[int]) -> str:
    # str(exc) is not the whole exception: JSONDecodeError keeps the full
    # document in .doc, and a handler or Sentry may serialize attributes or
    # the chained cause, so every attribute and link is inspected too.
    if id(exc) in seen:
        return ""
    seen.add(id(exc))
    parts = [repr(exc), repr(exc.args), repr(vars(exc))]
    for linked in (exc.__cause__, exc.__context__):
        if linked is not None:
            parts.append(_exception_text(linked, seen))
    return " ".join(parts)


def _leaks(caplog: pytest.LogCaptureFixture) -> list[str]:
    leaked = []
    for record in caplog.records:
        parts = [record.getMessage(), *(repr(v) for v in vars(record).values())]
        args = record.args if isinstance(record.args, tuple) else (record.args or {},)
        seen: set[int] = set()
        for arg in args:
            if isinstance(arg, BaseException):
                parts.append(_exception_text(arg, seen))
        if record.exc_info and record.exc_info[1] is not None:
            parts.append(_exception_text(record.exc_info[1], seen))
            parts.append(logging.Formatter().formatException(record.exc_info))
        if SENTINEL in " ".join(parts):
            leaked.append(f"{record.name}:{record.lineno} {record.levelname}")
    return leaked


def _logged_from(caplog: pytest.LogCaptureFixture, module: str) -> int:
    return sum(1 for record in caplog.records if record.name == module)


def _sentinel_fields(*names: str) -> dict[str, Any]:
    return {name: f"{SENTINEL}-{name}" for name in names}


OCR_CASES = [
    (
        "_auto_ocr_passport",
        _sentinel_fields(
            "passport_number",
            "expiry_date",
            "full_name",
            "gender",
            "date_of_birth",
            "birthplace",
            "nationality",
        ),
    ),
    (
        "_auto_ocr_visa",
        _sentinel_fields(
            "visa_type",
            "visa_number",
            "expiry_date",
            "issue_date",
            "full_name",
            "sponsor",
            "telex_number",
        ),
    ),
    (
        "_auto_ocr_nib",
        {
            **_sentinel_fields("nib_number", "company_name", "address", "issue_date"),
            "kbli_codes": [f"{SENTINEL}-kbli"],
        },
    ),
    (
        "_auto_ocr_npwp",
        _sentinel_fields("npwp_number", "full_name", "address", "kpp_name", "registration_date"),
    ),
    (
        "_auto_ocr_company_profile",
        {
            **_sentinel_fields(
                "company_name",
                "nib_number",
                "npwp_number",
                "akta_no",
                "authorized_capital",
                "shareholders",
                "notaris",
                "registered_address",
                "company_status",
                "jangka_waktu",
            ),
            "kbli_codes": [f"{SENTINEL}-kbli"],
        },
    ),
]


COMPLETION_LOG = {
    "_auto_ocr_passport": "Auto OCR completed for client 42",
    "_auto_ocr_visa": "Auto OCR visa completed for client 42",
    "_auto_ocr_nib": "Auto OCR NIB completed for client 42",
    "_auto_ocr_npwp": "Auto OCR NPWP completed for client 42",
    "_auto_ocr_company_profile": "Auto OCR company profile: updated company 7",
}


def _ocr_patches(response_text: str, extracted: dict[str, Any] | None) -> list[Any]:
    return [
        patch(
            f"{ROUTER}._download_drive_file",
            new=AsyncMock(return_value=(b"x", "image/jpeg")),
        ),
        patch(f"{ROUTER}._gemini_ocr", new=AsyncMock(return_value=response_text)),
        patch(f"{ROUTER}.extract_json_from_llm_response", return_value=extracted),
        patch(f"{ROUTER}.to_jsonb", return_value="{}"),
    ]


async def _run_ocr(name: str, pool: MagicMock, patches: list[Any]) -> dict[str, Any]:
    from backend.app.routers import crm_enhanced

    for p in patches:
        p.start()
    try:
        kwargs = {} if name == "_auto_ocr_passport" else {"doc_id": 5}
        return await getattr(crm_enhanced, name)(pool, 42, "file-1", **kwargs)
    finally:
        for p in reversed(patches):
            p.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [case[0] for case in OCR_CASES])
async def test_parse_failure_never_logs_the_raw_response(
    name: str,
    mock_db_pool: MagicMock,
    mock_db_conn: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_db_conn.fetchrow = AsyncMock(return_value={"full_name": "X", "company_id": 7})
    caplog.set_level(logging.DEBUG)

    raw = f'not json, but the document said "{SENTINEL}"'
    result = await _run_ocr(name, mock_db_pool, _ocr_patches(raw, None))

    assert result["success"] is False
    assert _logged_from(caplog, ROUTER) > 0
    assert _leaks(caplog) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "extracted"), OCR_CASES)
async def test_extracted_fields_and_response_never_reach_a_log(
    name: str,
    extracted: dict[str, Any],
    mock_db_pool: MagicMock,
    mock_db_conn: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Rows shaped as each helper reads them, so every case reaches its write
    # and its completion log: the client row (passport), the linked company
    # row by "id" (NIB, NPWP), and for the company profile the company id
    # followed by its existing custom_fields JSON.
    mock_db_conn.fetchrow = AsyncMock(return_value={"full_name": "X", "id": 7})
    mock_db_conn.fetchval = AsyncMock(side_effect=[7, '{"old": "v"}'])
    caplog.set_level(logging.DEBUG)

    raw = f'{{"note": "{SENTINEL}"}}'
    result = await _run_ocr(name, mock_db_pool, _ocr_patches(raw, dict(extracted, confidence=0.9)))

    assert result["success"] is True
    assert _leaks(caplog) == []
    assert any(COMPLETION_LOG[name] in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_classifier_parse_failure_never_logs_the_raw_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from backend.app.routers.crm_enhanced import _auto_classify_content

    caplog.set_level(logging.DEBUG)
    with (
        patch(
            f"{ROUTER}._download_drive_file",
            new=AsyncMock(return_value=(b"x", "image/jpeg")),
        ),
        patch(f"{ROUTER}._gemini_ocr", new=AsyncMock(return_value=f"prose {SENTINEL}")),
        patch(f"{ROUTER}.extract_json_from_llm_response", return_value=None),
    ):
        result = await _auto_classify_content("file-1")

    assert "error" in result
    assert _logged_from(caplog, ROUTER) > 0
    assert _leaks(caplog) == []


@pytest.mark.asyncio
async def test_classifier_logs_only_known_labels(caplog: pytest.LogCaptureFixture) -> None:
    from backend.app.routers.crm_enhanced import _auto_classify_content

    caplog.set_level(logging.DEBUG)
    label = {
        "document_type": f"{SENTINEL}-type",
        "confidence": 0.9,
        "language": f"{SENTINEL}-lang",
        "reasoning": f"{SENTINEL}-why",
    }
    with (
        patch(
            f"{ROUTER}._download_drive_file",
            new=AsyncMock(return_value=(b"x", "image/jpeg")),
        ),
        patch(f"{ROUTER}._gemini_ocr", new=AsyncMock(return_value="{}")),
        patch(f"{ROUTER}.extract_json_from_llm_response", return_value=label),
    ):
        result = await _auto_classify_content("file-1")

    assert result["document_type"] == f"{SENTINEL}-type"
    assert _leaks(caplog) == []
    assert any("type=other" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_classifier_keeps_a_known_label_readable(caplog: pytest.LogCaptureFixture) -> None:
    from backend.app.routers.crm_enhanced import _auto_classify_content

    caplog.set_level(logging.DEBUG)
    label = {"document_type": "passport", "confidence": 0.9, "language": "en"}
    with (
        patch(
            f"{ROUTER}._download_drive_file",
            new=AsyncMock(return_value=(b"x", "image/jpeg")),
        ),
        patch(f"{ROUTER}._gemini_ocr", new=AsyncMock(return_value="{}")),
        patch(f"{ROUTER}.extract_json_from_llm_response", return_value=label),
    ):
        await _auto_classify_content("file-1")

    assert any(
        "type=passport" in r.getMessage() and "language=en" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_gemini_cli_failure_logs_rc_and_length_not_stderr(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from backend.app.routers import crm_enhanced

    process = MagicMock()
    process.returncode = 1
    process.communicate = AsyncMock(return_value=(b"", f"echo of input {SENTINEL}".encode()))
    genai = MagicMock()
    genai.is_available = False
    caplog.set_level(logging.DEBUG)
    with (
        patch("backend.llm.ollama_client.is_ollama_available", new=AsyncMock(return_value=False)),
        patch(
            "backend.services.multimodal.cloud_vision_gate.cloud_vision_allowed",
            return_value=True,
        ),
        patch("shutil.which", return_value="/usr/local/bin/gemini"),
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)),
        patch("backend.llm.genai_client.get_genai_client", return_value=genai),
        pytest.raises(RuntimeError),
    ):
        await crm_enhanced._gemini_ocr(b"\xff\xd8\xff", "image/jpeg", "extract")

    assert any("Gemini CLI failed (rc=1" in r.getMessage() for r in caplog.records)
    assert _leaks(caplog) == []


@pytest.mark.asyncio
async def test_crm_extractor_parse_failure_never_logs_the_model_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from backend.services.crm.enrichment import AICRMExtractor

    class _Client:
        async def conversational(self, prompt: str, persona: str, max_tokens: int) -> dict:
            return {"text": f"not json: the client wrote {SENTINEL}"}

    caplog.set_level(logging.DEBUG)
    extracted = await AICRMExtractor(ai_client=_Client()).extract_from_conversation(
        [{"role": "user", "content": "hello"}],
    )

    assert extracted["client"]["confidence"] == 0.0
    assert _logged_from(caplog, ENRICHMENT) > 0
    assert _leaks(caplog) == []


@pytest.mark.asyncio
async def test_birthplace_enrichment_never_logs_the_birthplace_or_the_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from backend.services.crm.enrichment import BirthplaceEnrichmentService

    service = BirthplaceEnrichmentService(MagicMock())
    # Braces on both sides so the regex matches and json.loads really fails:
    # this drives the parse-failure log, not just the early return.
    service.call_ollama = AsyncMock(return_value=f"{{ broken {SENTINEL} }}")
    caplog.set_level(logging.DEBUG)

    ok = await service.enrich_client({"id": 42, "birthplace": f"{SENTINEL}-town"})

    assert ok is False
    assert _leaks(caplog) == []
    assert any("Failed to parse Ollama response as JSON" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["ollama", "gemini"])
async def test_generated_title_is_never_logged(
    provider: str, caplog: pytest.LogCaptureFixture
) -> None:
    from backend.services.crm import enrichment

    caplog.set_level(logging.DEBUG)
    title = f"{SENTINEL} visa question"
    if provider == "ollama":
        with patch("backend.llm.ollama_client.ollama_chat", new=AsyncMock(return_value=title)):
            result = await enrichment._generate_title_via_ollama("conv-1", "prompt", 50)
    else:
        genai = MagicMock()
        genai.is_available = True
        genai.generate_content = AsyncMock(return_value={"text": title})
        with patch("backend.llm.genai_client.get_genai_client", return_value=genai):
            result = await enrichment._generate_title_via_gemini("conv-1", "prompt", 50)

    assert result == title
    assert any("Generated title for conv conv-1" in r.getMessage() for r in caplog.records)
    assert _leaks(caplog) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "extracted"), OCR_CASES)
async def test_a_failure_echoing_a_document_value_is_logged_by_type_only(
    name: str,
    extracted: dict[str, Any],
    mock_db_pool: MagicMock,
    mock_db_conn: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # asyncpg's DataError and the parsers quote the offending value in the
    # message, so an exception raised while writing OCR fields carries them.
    mock_db_conn.fetchrow = AsyncMock(return_value={"full_name": "X", "company_id": 7})
    mock_db_conn.fetchval = AsyncMock(return_value=7)
    mock_db_conn.execute = AsyncMock(
        side_effect=RuntimeError(f"invalid input for query argument $1: '{SENTINEL}'")
    )
    caplog.set_level(logging.DEBUG)

    await _run_ocr(name, mock_db_pool, _ocr_patches("{}", dict(extracted, confidence=0.9)))

    assert _leaks(caplog) == []
    assert any("RuntimeError" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_classifier_failure_on_a_bad_confidence_is_logged_by_type_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from backend.app.routers.crm_enhanced import _auto_classify_content

    caplog.set_level(logging.DEBUG)
    label = {"document_type": "passport", "confidence": f"{SENTINEL}"}
    with (
        patch(
            f"{ROUTER}._download_drive_file",
            new=AsyncMock(return_value=(b"x", "image/jpeg")),
        ),
        patch(f"{ROUTER}._gemini_ocr", new=AsyncMock(return_value="{}")),
        patch(f"{ROUTER}.extract_json_from_llm_response", return_value=label),
    ):
        result = await _auto_classify_content("file-1")

    assert "error" in result
    assert _leaks(caplog) == []
    assert any("ValueError" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_enrichment_write_failure_is_logged_by_type_only(
    mock_db_pool: MagicMock,
    mock_db_conn: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from backend.services.crm.enrichment import BirthplaceEnrichmentService

    mock_db_conn.execute = AsyncMock(side_effect=TypeError(f"cannot encode '{SENTINEL}'"))
    service = BirthplaceEnrichmentService(mock_db_pool)
    service.call_ollama = AsyncMock(return_value='{"region": "R"}')
    caplog.set_level(logging.DEBUG)

    ok = await service.enrich_client({"id": 42, "birthplace": "B"})

    assert ok is False
    assert _leaks(caplog) == []
    assert any("TypeError" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_ollama_vision_json_error_never_logs_the_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import json

    from backend.app.routers import crm_enhanced

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def post(self, *args: Any, **kwargs: Any) -> MagicMock:
            response = MagicMock()
            response.json = MagicMock(
                side_effect=json.JSONDecodeError("Expecting value", f"model said {SENTINEL}", 0)
            )
            return response

    caplog.set_level(logging.DEBUG)
    with (
        patch("backend.llm.ollama_client.is_ollama_available", new=AsyncMock(return_value=True)),
        patch("httpx.AsyncClient", _Client),
        patch(
            "backend.services.multimodal.cloud_vision_gate.cloud_vision_allowed",
            return_value=False,
        ),
        pytest.raises(RuntimeError),
    ):
        await crm_enhanced._gemini_ocr(b"\xff\xd8\xff", "image/jpeg", "extract")

    assert _leaks(caplog) == []
    assert any("Ollama vision error: JSONDecodeError" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ValueError, RuntimeError])
async def test_crm_extractor_provider_error_is_logged_by_type_only(
    error: type[Exception], caplog: pytest.LogCaptureFixture
) -> None:
    from backend.services.crm.enrichment import AICRMExtractor

    class _Client:
        async def conversational(self, prompt: str, persona: str, max_tokens: int) -> dict:
            raise error(f"provider echoed the conversation: {SENTINEL}")

    caplog.set_level(logging.DEBUG)
    await AICRMExtractor(ai_client=_Client()).extract_from_conversation(
        [{"role": "user", "content": "hello"}],
    )

    assert _leaks(caplog) == []
    assert any(error.__name__ in r.getMessage() for r in caplog.records)


NUMERIC_SENTINEL = 77311234


def _numeric_leaks(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if str(NUMERIC_SENTINEL) in r.getMessage()]


@pytest.mark.asyncio
async def test_crm_extractor_logs_confidence_only_inside_its_range(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import json

    from backend.services.crm.enrichment import AICRMExtractor

    class _Client:
        async def conversational(self, prompt: str, persona: str, max_tokens: int) -> dict:
            return {"text": json.dumps({"client": {"confidence": NUMERIC_SENTINEL}})}

    caplog.set_level(logging.DEBUG)
    await AICRMExtractor(ai_client=_Client()).extract_from_conversation(
        [{"role": "user", "content": "hello"}],
    )

    assert _numeric_leaks(caplog) == []
    assert any("out_of_range client confidence" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_classifier_logs_confidence_only_inside_its_range(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from backend.app.routers.crm_enhanced import _auto_classify_content

    caplog.set_level(logging.DEBUG)
    label = {"document_type": "passport", "confidence": NUMERIC_SENTINEL, "language": "en"}
    with (
        patch(
            f"{ROUTER}._download_drive_file",
            new=AsyncMock(return_value=(b"x", "image/jpeg")),
        ),
        patch(f"{ROUTER}._gemini_ocr", new=AsyncMock(return_value="{}")),
        patch(f"{ROUTER}.extract_json_from_llm_response", return_value=label),
    ):
        await _auto_classify_content("file-1")

    assert _numeric_leaks(caplog) == []
    assert any("confidence=out_of_range" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [lambda msg: httpx_error(msg), lambda msg: RuntimeError(msg)],
    ids=["http", "unexpected"],
)
async def test_ollama_request_failure_is_logged_by_type_only(
    error: Any, caplog: pytest.LogCaptureFixture
) -> None:
    from backend.services.crm.enrichment import BirthplaceEnrichmentService

    client = MagicMock()
    client.post = AsyncMock(side_effect=error(f"request carried {SENTINEL}"))
    service = BirthplaceEnrichmentService(MagicMock())
    service._get_client = MagicMock(return_value=client)
    caplog.set_level(logging.DEBUG)

    assert await service.call_ollama(f"prompt with {SENTINEL}") is None
    assert _leaks(caplog) == []
    assert any("Ollama request failed" in r.getMessage() for r in caplog.records)


def httpx_error(message: str) -> Exception:
    import httpx

    return httpx.HTTPError(message)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["ollama", "gemini"])
@pytest.mark.parametrize("error", [ValueError, RuntimeError])
async def test_title_provider_error_is_logged_by_type_only(
    provider: str, error: type[Exception], caplog: pytest.LogCaptureFixture
) -> None:
    from backend.services.crm import enrichment

    caplog.set_level(logging.DEBUG)
    failure = error(f"provider echoed the message: {SENTINEL}")
    if provider == "ollama":
        with patch("backend.llm.ollama_client.ollama_chat", new=AsyncMock(side_effect=failure)):
            result = await enrichment._generate_title_via_ollama("conv-1", "prompt", 50)
    else:
        genai = MagicMock()
        genai.is_available = True
        genai.generate_content = AsyncMock(side_effect=failure)
        with patch("backend.llm.genai_client.get_genai_client", return_value=genai):
            result = await enrichment._generate_title_via_gemini("conv-1", "prompt", 50)

    assert result is None
    assert _leaks(caplog) == []
    assert any(error.__name__ in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "confidence", [10**400, "high", None, [0.9]], ids=["overflow", "str", "none", "list"]
)
async def test_crm_extractor_falls_back_as_before_on_a_confidence_it_cannot_format(
    confidence: Any,
) -> None:
    # Behaviour parity with the old f-string: a confidence that cannot be
    # formatted as a float still makes the extraction fall back to empty.
    import json

    from backend.services.crm.enrichment import AICRMExtractor

    class _Client:
        async def conversational(self, prompt: str, persona: str, max_tokens: int) -> dict:
            payload = '{"client": {"full_name": "N", "confidence": CONF}}'
            return {"text": payload.replace("CONF", json.dumps(confidence))}

    extracted = await AICRMExtractor(ai_client=_Client()).extract_from_conversation(
        [{"role": "user", "content": "hello"}],
    )

    assert extracted["client"]["confidence"] == 0.0
    assert "_low_confidence" not in extracted
