# Public editorial enrollment

An immigration news article may remain in canonical `visa` staging while an admin
explicitly enrolls its reviewed public content for News Room. Enrollment preserves
the ID, category, source, source-type deduplication and publication operation ID.
It neither submits another intake item nor publishes anything.

## Authorization and binding

`POST /api/intel/staging/visa/{item_id}/workspace-editorial` requires the existing
admin authorization used by the canonical publisher. A workspace marketing key or
ordinary scraper key is insufficient. The body is:

```json
{
  "confirmation": "REVIEWED_PUBLIC_EDITORIAL",
  "expected_source_url": "https://public.example/article",
  "expected_fingerprint": "<64 lowercase hex characters>"
}
```

Review the exact item from the authenticated staging preview. Compute its digest
with `backend.services.intel.intel_public_editorial.editorial_fingerprint` from
this release. Do not reconstruct the record from a truncated workspace projection.
The item must still be pending, unpublished, `scraper_auto` / `editorial_manual`,
with matching ID, source and digest. Repeating an unchanged enrollment is idempotent.
Errors are closed: 401/403 for authorization, 404 for missing item, 409 for changed
or ineligible state. Do not edit staging JSON to work around any rejection.

Only this enrolled ID becomes accessible through existing News Room list, get,
editorial update, cover, publish, status and confirm-live operations. An unrelated
source edit invalidates enrollment. Authorized workspace edits renew the binding
under the canonical item lock. Covers use content-addressed filenames; publication
checks the binding when acquiring its lease and again when loading its snapshot.
Indexing uses that reviewed snapshot rather than reloading a concurrently edited item.

## Release and activation

1. Review and gate the candidate commit independently. Land through a Rakyat PR
   after its required Actions and cross-family gate pass; use the existing Fly
   deployment workflow and verify that its deployed revision contains the change.
2. Use an already-authorized admin session or configured admin API credential.
   Keep credentials in process memory, never in command arguments or saved output.
   No admin access means activation is blocked; do not create credentials or use
   a weaker authorization check to proceed.
3. Read and verify the selected public article through the official preview,
   calculate its fingerprint, then call the enrollment endpoint once with the
   fixed confirmation above. On an uncertain result, read the item before retrying.
4. Through the workspace connector, verify the same ID, unchanged category/source
   and complete approved text. Verify another non-enrolled visa ID remains 404.
   Only then continue the owner's separately authorized editorial workflow.

Rollback: stop workspace actions for the selected item. A normal source edit
invalidates its binding; an application rollback restores the news-only projection.
Neither rollback publishes, duplicates, reclassifies or removes the canonical item.

Local validation: `python -m pytest backend/tests/unit/routers/test_public_editorial_enrollment.py
backend/tests/unit/routers/test_workspace_marketing_bridge.py
backend/tests/services/intel/test_intel_staging_service.py -q --override-ini='addopts='`.
