# Telegram retired — 2026-11-07

Owner direction: retire every Telegram bot and integration. `backend.core.telegram_retirement.telegram_is_retired()` is a source policy, with no environment opt-in. Stale cached tokens cannot enable startup, HTTP sends or approval callbacks. Historical conversation reads remain available; public Telegram contact links are removed.

Garuda `staff_page_*` money-anomaly jobs now deliver through the internal email channel (`EmailStaffPageSender` / `BrevoEmailSender`) under `telegram_is_retired()`, leaving `RETIRED_TELEGRAM_JOB_TYPES` empty while preserving the consumer's hold predicate (`retired_internal_attention`) for any future retired transport types. Customer email and other outbox jobs continue normally. No schema change or customer-row cleanup is involved.

Review SLA expiry continues without a Telegram adapter or chat ID. Other senders retain local logging and non-Telegram functions. Legacy protocol tests explicitly opt into mocked transports only; new tests enforce the retired production default even with injected credentials.

Verification includes negative HTTP/client-construction/approval-callback tests, adapter startup absence, preserved review expiry, and a loopback PostgreSQL TEMP-table proof that six held staff jobs keep attempts/dispatched state while email dispatches. The full-schema integration counterpart runs in hosted CI; a local database missing historical tables is not acceptance for that suite.

Compatibility tests preserve the underlying non-Telegram contracts: quota-alert cooldown uses the real alert service with a mocked supported channel, attendance-query positives remain separate from the retired reminder transport, and owner-ID configuration/privacy is tested independently of network delivery. Stale Telegram configuration must not construct a client. Historical mocked protocol tests do not establish production delivery authority.

Deployment must remove all Telegram secrets, apply the gated image, and verify every serving machine's exact build and Telegram-key absence without printing environments. Dedicated Telegram schedules and OpenClaw channel state are retired separately by the operations track. Local deletion does not revoke provider credentials; owner-account revocation is still required, with no replacement token.

Bites: running backend processes never construct a Telegram transport under the retired source policy; Garuda counts retained attention work while non-Telegram jobs continue, and public footers offer no Telegram contact. Negative tests and the final deployed-image receipt establish these observations.
