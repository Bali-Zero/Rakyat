# Session identity on the client (kita): the bearer binding

Scope: how a page in `apps/mouth` that shows personal data (a staff member's
own HR card, "My card") decides WHO is signed in, and what happens when that
changes while a request is in flight. The identity for such a page is the
BEARER TOKEN, not the profile. Code: `session-signal.ts`,
`session-identity.ts`; writers: `src/lib/api/client.ts`,
`src/lib/api/public-auth.ts`, `src/lib/utils/token.ts`.

This is deliberately narrow. An earlier attempt (PR #207) tried to make the
whole client session coherent (the profile, `getProfile`, the in-memory profile
copy, `safeStorage`) and every fix opened a new surface. This document covers
only the bearer binding; everything else is listed in S5 as unchanged.

## The identity sources, and which one counts

1. The bearer token, key `auth_token`, in `safeStorage` (localStorage, or an
   in-tab memory fallback when storage is blocked). THIS is the identity.
2. `user_profile` in storage: not an identity source here. It can outlive the
   token; this document does not change that (S5).
3. The HttpOnly cookie `nz_access_token`, `Domain=.balizero.com` in
   production. JS cannot read it. It is shared with every balizero.com
   subdomain, so a login elsewhere can replace it. With no `Authorization`
   header a request authenticates by it; when a Bearer header is present the
   backend uses the header (`backend/middleware/hybrid_auth.py`). So a
   cookie-only request can come back as somebody else's data.

## Rules

**S1. Identity.** For any page that shows personal data, the signed-in
identity is the `sub` of the currently valid bearer token in THIS TAB's
storage, and nothing else. No valid bearer token means nobody is signed in for
that purpose: the page shows no personal value, even though the cookie would
still authenticate a request. "Valid" = present, decodable as a JWT with a
non-empty string `sub`, and not expired by `isTokenExpired` with the one shared
`TOKEN_CLOCK_SKEW_SECONDS` of `utils/token.ts` (30 s; do not introduce a
second number). The identity of a RESPONSE is the `sub` of the bearer that
request carried (S3), never the identity at the time the response is handled.
The client does not verify the signature (the backend does) and never logs or
renders the token or the `sub`.

**S2. Signal and epoch, on bearer token changes only.** A same-tab window event
`bz:session-changed` is dispatched, and a per-tab in-memory epoch is bumped,
after every write or removal of the bearer token (`auth_token`), from the
single places that do it: `ApiClientBase.setToken` and `clearToken` (which
removes the token and the profile together and signals once), the same two on
`PublicAuthClient`, the expiry removal in `getValidToken` (for key
`auth_token`), and `ApiClientBase.getToken`'s expiry clear (which goes through
`clearToken`). A `user_profile` write does NOT signal and does NOT move the
epoch: the identity here is the token, so a profile write must not reload a
bound page (this also removes any loop between a page's load path and a
profile write). Other tabs reach this tab as a `storage` event on `auth_token`
(a `null` key, i.e. `storage.clear()`, counts), which bumps the epoch too. When
localStorage is blocked the in-tab memory fallback has no cross-tab signal;
same-tab signalling still works.

**S3. Credential sent is the proof; the epoch is an extra same-tab guard.** The
epoch cannot prove which credential left the tab. Another tab's change reaches
this tab only as a `storage` event, and those events are queued: their order
relative to a fetch continuation is implementation-defined. Sequence that
breaks an epoch-only rule: identity read A at epoch E; another tab sets B;
`request()` reads and sends bearer B; the other tab restores A; the response
is handled BEFORE the queued events, so the identity still reads A/E and B's
data would be shown under A. So the proof is the credential actually sent:
`requestWithCredential(endpoint, options?, timeout?)` on `ApiClientBase` and
`PublicAuthClient` (same code path as `request()`, which still returns only
data and throws the same errors) returns `{data, sentToken, sentSub}`: the
exact bearer in the FINAL headers object passed to `fetch`
(`bearerTokenFromHeaders`), whatever produced it, the managed token or a
caller-supplied `Authorization`; `null` if the request went out without a
Bearer; and its decoded `sub`. Only a value of exactly `Bearer <token68>`
counts (scheme spelled `Bearer`, one space, nothing trimmed): that is the shape
every reader, the backend and the Next route handlers, takes as a bearer, and
any other value may make them fall back to the cookie. Any other value, or two
spellings of the header (fetch would join them), gives `null`/`null`. The token
is returned there and nowhere else; never log or render it. The member is
optional on `IApiClient` (test doubles may lack it; a page that needs the
binding uses a client that has it).

**S4. How a page binds.** `session-identity.ts` exports
`readSessionIdentity(): {sub, epoch} | null` (pure read of storage: removes
nothing, dispatches nothing, never throws), `subscribeSessionChange(cb)`
(signal + `storage` + `focus` + `visibilitychange` to visible, returns the
unsubscribe), `useSessionIdentity()` (null on the server and first render, then
live; it also re-reads when the token reaches its expiry, with no removal) and
`acceptResponse(response, displayed, owner?)`. A page binds data to identity by:

1. issuing a request only when an identity exists, through
   `requestWithCredential`, never through the cookie alone;
2. accepting a response carrying personal data only if (a) it was sent with a
   bearer (`sentSub` non-null: a cookie-only response is never accepted), (b) a
   FRESH synchronous read of the stored token NOW gives a valid token whose sub
   equals `sentSub` (storage read directly, not the epoch, not events), (c)
   when the response names its owner (e.g. `team_member_id`) the owner equals
   `sentSub`, and (d) the identity the page displays (`useSessionIdentity`) has
   `sub` equal to `sentSub` and its epoch is still the tab's epoch: all four
   are `acceptResponse`;
3. clearing its data the moment the identity changes or disappears.

**S5. Out of scope: unchanged pre-existing behaviour, and follow-ups.** None of
the following is changed by this PR, and none is made worse by it: `auth.api.ts`
and `utils/storage.ts` are byte-identical to `main`, and `client.ts` differs
only by the signal hooks in `setToken`/`clearToken` and the credential capture
in the request path.

- `AuthApi.getProfile()` writes `user_profile` unconditionally, so a stale
  response can rewrite the profile after a logout.
- `ApiClientBase` keeps an in-memory copy of the profile that `isAdmin`,
  `isBoard` and `getAdminHeaders` read.
- `safeStorage`'s quota fallback: after a failed write its reads prefer
  localStorage. This can affect tokens too.
- Components that copy the profile or `isAdmin` into their own state.
- The cookie's domain-wide scope (see above).
- A person physically holding the device.
- The existing pages `/hr/payroll` and the `/hr` dashboard are not migrated to
  this binding; only a page that uses S4 is protected.

Each is a follow-up with its own PR; fixing them here is what made #207 grow.

**S6. Cross-tab freshness: an accepted residual (owner decision, 2026-11-12).**
Every "fresh synchronous read" above (S3, S4 2(b)) reads THIS TAB'S view
of storage. That view is not guaranteed to be current with other tabs: the HTML
standard gives no cross-process synchronization for localStorage, and Chromium
serves reads from a per-renderer cache that another process's write reaches
only later, together with the `storage` event. So in the interval between
another tab's sign-out or sign-in and the delivery of its `storage` event here,
this tab can still accept a response and show the data of the identity whose
bearer it sent, i.e. the person who was signed in in this tab when it sent the
request, and `getProfile()` can still write that person's profile. When the
event is delivered, S4 rule 3 clears the page; the
state converges. What the client guarantees: it never shows data for an
identity this tab did not send a bearer for (S4 2(a), 2(c)), and every
divergence is bounded by the browser's own propagation delay. What it does not
guarantee: strict cross-tab consistency inside that delay; no client-side
mechanism can. The owner accepted this residual on 2026-11-12. The larger
shared-device risk, a session left open on an office computer or phone, is not
addressed here and belongs to a kita-wide follow-up (lock on inactivity).

## Where each rule is proved

- S2 signals and epoch (token writes and removals, both expiry removals, no
  signal on profile writes): `session-signal.test.ts`.
- S3 credential capture: `src/lib/api/client-credential.test.ts`.
- S1/S4 helpers: `session-identity.test.tsx`.
- S4 and the races through a bound page and the real `ApiClient`, including the
  delayed cross-tab `storage` events: `session-bound-page.test.tsx`.
