"""A `case-delivery:` key may be stored only over the file whose SHA-256 it names (both writers)."""

import base64
import hashlib
from unittest.mock import MagicMock

import pytest
from fastapi import BackgroundTasks, HTTPException

from backend.app.routers import crm_enhanced_documents as docs

A, B = b"%PDF-1.7\nbytes of A", b"%PDF-1.7\nbytes of B"


def marker(content, key="document_2", company=0, tail=""):
    return f"case-delivery:7:{company}:{key}:{hashlib.sha256(content).hexdigest()}{tail}"


@pytest.mark.parametrize(
    "key",
    [
        None,
        "",
        "crm-upload:41",
        "repair:abc",
        marker(A),
        marker(A, company=5),
        marker(A, tail=":345"),
    ],
)
def test_other_keys_and_a_true_marker_pass(key):
    docs._refuse_forged_delivery_marker(key, A)


@pytest.mark.parametrize(
    "key",
    [marker(B), marker(B, tail=":9"), "case-delivery:7:0:document_2:", "case-delivery:garbage"],
)
def test_a_marker_over_other_bytes_is_refused(key):
    with pytest.raises(HTTPException) as refused:
        docs._refuse_forged_delivery_marker(key, A)
    assert refused.value.status_code == 409
    assert refused.value.detail == "Delivery marker does not match the file"


def untouched_pool():
    pool = MagicMock()
    pool.acquire.side_effect = AssertionError("nothing may be read or written for a forged marker")
    return pool


@pytest.mark.asyncio
async def test_client_endpoint_refuses_a_forged_marker_before_anything_is_written():
    data = docs.DocumentUploadBase64(
        file=base64.b64encode(B).decode(),
        file_name="ITK.pdf",
        document_type="itk",
        practice_id=7,
        intake_idempotency_key=marker(A),
    )
    with pytest.raises(HTTPException, match="Delivery marker does not match"):
        await docs.upload_document_base64(
            31,
            data,
            pool=untouched_pool(),
            current_user={"email": "x@example.test"},
            background_tasks=BackgroundTasks(),
            access_already_verified=True,
            uploaded_source="team",
        )


@pytest.mark.asyncio
async def test_company_endpoint_refuses_a_forged_marker_before_anything_is_written():
    data = docs.CompanyDocumentUploadBase64(
        file=base64.b64encode(B).decode(),
        file_name="ITK.pdf",
        document_type="itk",
        intake_idempotency_key=marker(A, company=5),
    )
    with pytest.raises(HTTPException, match="Delivery marker does not match"):
        await docs._upload_company_document_impl(
            company_ref=5,
            data=data,
            pool=untouched_pool(),
            current_user={"email": "x@example.test"},
            access_already_verified=True,
        )
