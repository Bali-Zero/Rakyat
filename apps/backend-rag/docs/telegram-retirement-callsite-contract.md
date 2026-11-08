# Telegram retirement: business call-site contract

This contract defines the bounded implementation after independent review exposed an unspecified notification failure boundary. It incorporates the planning reviews; none of their BLOCK receipts is source acceptance. Release requires a fresh foreign-family gate on the exact final head, normal hosted CI and live proof.

## Scope and invariants

1. Retire all Telegram startup, outbound HTTP/polling/relays and approval callbacks, including stale-credential cases. Preserve historical conversation reads, internal attention, customer email, WhatsApp and human approval requirements. Add no replacement provider or publishing authority.
2. A removed optional Telegram output must not alter a completed business action or enter a second reply/audit/escalation path.
3. Remove the onboarding Telegram notification call entirely. The `if onboarding_result` branch awaits exactly `whatsapp_service.send_message`, then `_persist_audit_messages`, and returns. It calls no notifier/helper directly or indirectly. Do not move the old call into a guarded helper or use the conversation-log helper as a replacement.
4. Under the real default policy, stale and absent bot credentials with an admin destination are separate cases. Both produce exactly one WhatsApp send, one audit, no AI context/reply, no escalation, no conversation-log notification and no HTTP client construction.
5. Configured synthetic notifier exceptions (`RuntimeError`, `ValueError`, `httpx.HTTPError`) each produce zero notification attempts and the same exact business counts. These test removal, not an unexercised catch. An AST/source test independently checks the onboarding two-await allowlist and terminal return.
6. A genuine detector exception before confirmation still falls through to exactly one normal AI reply, one audit and the existing conversation-log attempt. Never blanket-return from detector failure.
7. Audit every singleton invocation and passed-in alias by actual file/line, method and exception boundary. Distinguish invocations, delegation and constructors; do not infer coverage from direct API strings alone.
8. Keep intentionally retired Garuda staff pages durable/undispatched without retry consumption, outside active backlog alarms, but visible as `retired_internal_attention`. Overdue or exhausted customer email must still degrade health. Review expiry, assignment/cache invalidation and research continue without Telegram credentials.

## Baseline to account for

The baseline contains 11 singleton call sites in six modules plus one pass-through and four separate constructors. The implementation removes the onboarding call, leaving ten calls whose failure boundaries must be recorded individually:

- `whatsapp_chat.py`: conversation-log helper has a local try; onboarding notification is removed.
- `intel_approval_service.py`: `send_photo` and `send_message`, each under the per-chat try; approval stays pending on failure.
- `intel_cover_handler.py`: two download sites under the caller try, two notification sites under local tries; only reached from the retired inbound route.
- `alert_wiring.py`: passes the singleton to an adapter and invokes it there; `alert_dispatcher.py` owns the per-channel try and must not mark a failed send delivered.
- `human_escalation_notifier.py`: local try returns failure; `visa_oracle.py`: local try preserves the WhatsApp handoff URL.
- Separate constructors: channel adapter, LKPM notifier, omnichannel assignment and credit sentinel. Guard or local failure isolation is required.

The pre-existing conversation-log call passes an unsupported `disable_notification` keyword. Record this honestly; do not repair unrelated legacy behavior in this correction.

## Acceptance

- The unchanged blocked head demonstrated six red cases and one genuine-detector-failure positive control. The bounded implementation must turn all seven green with real default policy, not global test opt-outs.
- Strengthen the old mocked onboarding test to exact one send/audit and zero Telegram awaits. Avoid fragile string-path monkeypatch resolution in newly added legacy protocol fixtures.
- Verify the caller matrix, affected legacy modules, actual loopback PostgreSQL TEMP hold/active-email controls, offline production-build footer browser test and hosted full-schema CI. Missing schema is not a pass. Diagnose actual failures before any retry.
- The owner already authorized retirement. Standalone spec-only approval rounds were an extra planning step; they grant no release authority and are not treated as source acceptance. All concrete feedback is incorporated in this contract. The final foreign-family source gate and every normal CI/live requirement remain mandatory.
- If this specification-driven implementation still violates these explicit business invariants, suspend source release with the concrete defect instead of speculative further patches.
- Remove hosted credentials only in the controlled final release, then prove exact deployed image and per-machine disabled policy/credential absence. Provider-account revocation is separate and requires no replacement token.
