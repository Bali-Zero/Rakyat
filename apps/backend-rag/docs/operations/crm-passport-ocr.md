# CRM passport OCR

The Fly Drive worker consumes passport OCR jobs created when a CRM document enters
`pending`. Archive registration leaves OCR unset. Existing pending documents require
explicit selection. Job creation time and lease availability are recorded separately
from document creation time.

Staff can read terminal status, extracted fields and review reasons for any single
document at `GET /api/crm/clients/{client_id}/ocr-status?document_id={document_id}`.
The document list also returns
`ocr_review_reason`. A completed result belongs to the document, client and Drive file
that the worker claimed. Staff reconcile identity differences before updating client records.

## Historical recovery

Use the read-only production role to identify an active, unreviewed passport attached to
one active client. Check file ownership and current Drive metadata. Defer a family
attachment, conflicting file or content hash, prior intake lineage, or an existing
client-level result for the same file to staff review.

For an approved document, run the following in the deployed application environment.

```sh
python -m backend.workers.passport_ocr --document-id DOCUMENT_ID
```

The command reports how many jobs it created. Inspect the staff result before selecting
another document. Stop on unexpected storage state, attribution or worker behavior.
Replaying a completed document creates zero new jobs. After three interrupted or
unsuccessful attempts the worker records a reason for review. The worker uses the
existing CRM OCR provider settings.
