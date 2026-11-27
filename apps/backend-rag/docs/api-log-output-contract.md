# API log output contract

The API logging bootstrap must protect patterned personal identifiers at each Python logging destination. Operators must still be able to tell whether a delivery happened and which internal CRM row an event refers to. This contract covers normal standard-library logging delivery inside the API process.

| Input or situation | Required observation |
| --- | --- |
| Patterned email, phone, NPWP, external client code or labelled document identifier | The original literal disappears from the emitted message, exception, stack text or structured field. A harmless event marker survives. |
| Root console, rotating file, ring, Uvicorn or a standard handler attached or replaced after startup | The destination observes the protection, including fields its own filter adds. The ring retains its existing omission of structured context. |
| Internal CRM row reference | A positive 32-bit integer under the exact `client_id` key survives. External `CL-…` codes, text and long identifier-shaped numbers under that key are masked. |
| Invoice delivery state | The boolean values of `email_attempted`, `email_sent`, `accounting_notified`, `drive_uploaded` and `delivery_may_have_occurred` retain their type and value. The exemption applies only to booleans. Contact values keep their normal protection. |
| Ordinary diagnostics | Preserve event markers, durations, prices, timestamps, numeric paths and existing safe redaction markers. Preserve exception-bearing records and the sanitized type, summary and rendered stack in the application outputs. |
| Sentry events and breadcrumbs | Keep existing event grouping where the SDK requires the original exception. Sanitize event and breadcrumb payloads before export. |
| Deep, cyclic or unrenderable fields | Bound traversal. Withhold the affected subtree or record if safe rendering fails. |

Acceptance requires captured output from the application's normal initializer in production and development settings, dedicated Uvicorn and handler-enrichment checks, and the complete hosted backend test suite. Integration checks must run with the API boundary active and exercise invoice persistence, migration reporting, background exceptions, CRM correlation and both email delivery failures. The actual network sends in those cases are mocked.

The worker and cron entry points that configure their own logging are tracked separately from the API. Bare personal names and unlabelled documents in arbitrary free text have no patterned match in this contract.
