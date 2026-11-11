# Session identity on the client (kita)

Scope: how a page in `apps/mouth` that shows personal data (a staff member's
own HR card, own pay) decides WHO is signed in, and what happens when that
changes while a request is in flight. Code: `session-signal.ts`,
`session-identity.ts`; writers: `src/lib/api/client.ts`,
`src/lib/api/public-auth.ts`, `src/lib/utils/token.ts`; guarded writer:
`AuthApi.getProfile` in `src/lib/api/auth/auth.api.ts`.

## The three identity sources, and why they diverge

1. `user_profile` in storage: written by `setUserProfile`, removed by
   `clearToken` (client.ts, public-auth.ts). It can outlive the token.
2. The bearer token, key `auth_token`, in `safeStorage` (localStorage, or an
   in-tab memory fallback when storage is blocked). `getValidToken`
   (`utils/token.ts`) removes an expired token ALONE (no profile, no signal
   before this change); `ApiClientBase.getToken` removes it through
   `clearToken` (both keys).
3. The HttpOnly cookie `nz_access_token`, `Domain=.balizero.com` in
   production (`app/api/auth/login/route.ts`, and the same attributes in
   `app/api/[...path]/route.ts`). JS cannot read it. It is shared with every
   balizero.com subdomain, so a login elsewhere can replace it. With no
   `Authorization` header a request authenticates by it; when a Bearer header
   is present the backend uses the header and a bad header is a hard failure,
   it does not fall back to the cookie (`backend/middleware/hybrid_auth.py`,
   "Priority 2" before "Priority 3").

## Rules

**S1. Identity.** For any page that shows personal data, the signed-in
identity is the `sub` of the CURRENTLY VALID bearer token in this tab's
storage, and nothing else. No valid bearer token means nobody is signed in
for that purpose: the page shows no personal value, even though the cookie
would still authenticate a request. The cookie and `user_profile` are not
identity sources for this purpose. "Valid" = present, decodable as a JWT with
a non-empty string `sub`, and not expired by `isTokenExpired` (30 s skew). The
identity of a RESPONSE is the `sub` of the bearer that request carried (S3),
never the identity at the time the response is handled. The
`sub` is the `team_members` primary key as a string, the same value as
`user_profile.id` (backend `routers/auth.py` login, magic link, refresh). The
client does not verify the signature (the backend does) and never logs or
renders the token or the `sub`.

**S2. Signal.** A same-tab window event `bz:session-changed` is dispatched
after EVERY write or removal of the bearer token or of `user_profile`, from
the single places that do it: `ApiClientBase.setToken/setUserProfile/
clearToken`, the same three on `PublicAuthClient`, and the expiry removal in
`getValidToken` (for key `auth_token`). `clearToken` removes both keys and
signals once. Other tabs learn through the `storage` event on `auth_token` /
`user_profile` (a `null` key, i.e. `storage.clear()`, counts). When localStorage
is blocked the in-tab memory fallback has no cross-tab signal; same-tab
signalling still works.

**S3. Credential sent, then epoch.** The epoch is not proof of which credential
left the tab, and the spec's first version treated it as proof. It is wrong
because another tab's change reaches this tab only as a `storage` event, and
those events are queued: their order relative to a fetch continuation is
implementation-defined. Sequence that broke it: identity read A at epoch E;
another tab sets B; `request()` reads and sends bearer B; the other tab
restores A; the response is handled BEFORE the queued events, so the identity
still reads A/E. So the proof is the credential actually sent:
`ApiClientBase.requestWithCredential(endpoint, options?, timeout?)` (same code
path as `request()`, which still returns only data) returns
`{data, sentToken, sentSub}`: the exact bearer placed in the Authorization
header at send time, `null` if the request went out cookie-only, and its
decoded `sub`. It is read from the FINAL headers object passed to `fetch`
(`bearerTokenFromHeaders`), whatever produced them: the managed token or a
caller-supplied `Authorization`. A non-Bearer scheme, a malformed value, or two
spellings of the header (fetch would join them) give `null`/`null`. The token is returned there and nowhere else; never log or
render it. The in-memory per-tab epoch (`getSessionEpoch()`) is kept: it is
bumped on every signal above and on every session-key `storage` event, and is
an extra same-tab guard, no longer the proof.

`AuthApi.getProfile()` uses that path and writes `user_profile` only if the
token was sent (`sentToken` non-null), AND a fresh synchronous read of storage
gives a CURRENTLY VALID token (`readStoredValidToken()`, the one shared
`TOKEN_CLOCK_SKEW_SECONDS` of `utils/token.ts`, also used by `isTokenExpired`
and the identity) equal to the sent one, AND the epoch is unchanged. Equality
alone is not enough: a token sent with 31 s left is expired under the 30 s
skew two seconds later and identifies nobody. If the valid stored token is not
the one sent (changed, removed or expired), nothing is written or returned from
the response, and the call returns the stored profile only when that profile's `id`
is the `sub` of the currently valid stored token; otherwise it throws the
exported `StaleSessionError`. A cookie-only response is returned as before and
never written. If the token is still valid and unchanged but the epoch moved (a same-tab
write in between), the response is returned unwritten: it belongs to the
credential still in force. Every caller of `getProfile()` was read (44 call
sites): each sits in a try/catch or has a `.catch`, so a throw reads as a
failed profile read: no crash, no redirect loop (workspace layout re-throws to
its login redirect, which is correct when the session ended mid-flight; the
analytics layout asks `hasSession()`; chat shows a default name; HR layout and
the owner gate fail closed). `login` and `verifyMagicLink` are NOT guarded on
purpose: they establish a new session and the last completed login wins.
Test doubles of the client that lack `requestWithCredential` fall back to the
epoch guard only; both real clients implement it.

**S4. Helper for pages.** `session-identity.ts` exports
`readSessionIdentity(): {sub, epoch} | null` (pure read of storage: removes
nothing, dispatches nothing, never throws), `subscribeSessionChange(cb)`
(signal + `storage` + `focus` + `visibilitychange`, returns the unsubscribe),
`useSessionIdentity()` (null on the server and first render, then live; it also
re-reads when the token reaches its expiry), `isSameIdentity`, and
`acceptResponse(response, displayed, owner?)`. Fresh reads live in
`session-credential.ts`. A page binds data to identity by:
1. issuing a request only when an identity exists, through
   `requestWithCredential`, never through the cookie alone;
2. accepting a response carrying personal data only if (a) it was sent with a
   bearer (`sentSub` non-null: a cookie-only response is never accepted, the
   server may have used someone else's cookie), (b) a FRESH synchronous read of
   the stored token NOW gives a valid token whose sub equals `sentSub` (storage
   read directly, not the epoch, not events), (c) when the response names its
   owner (e.g. `team_member_id`) the owner equals `sentSub`, and (d) the
   identity the page displays (`useSessionIdentity`) equals `sentSub` and its
   epoch is still the tab's epoch: all four are `acceptResponse`;
3. clearing its data the moment the identity changes or disappears.
The epoch moves on every token and profile write, and the displayed identity
carries it, so a profile write makes a bound page reload. Condition: a bound
page's load path must NOT call `getProfile()` or anything that calls
`setUserProfile`, otherwise each load writes the profile, moves the epoch and
triggers the next load, a loop. A page that needs the profile reads
`user_profile` or gets it from its parent; it does not refetch it in the load
effect.

**S5. Out of scope, stated honestly.** (a) The cookie's domain-wide scope is
not changed here; with S1 a page bound to the bearer identity never shows data
for a session it did not see, but a request that falls back to the cookie
(no bearer) can still be answered for another user, and the page must not
display it. (b) A person who holds the device and its session can read that
session's data by other means. (c) Existing pages that show own pay data are
NOT migrated by this change and still have no identity binding: `/hr/payroll`
and the `/hr` dashboard. They are follow-ups, as is the HR "my card" page
(PR 206), which should be rebuilt on `useSessionIdentity`. (d) Login, logout
network behaviour, redirects, cookie attributes and token lifetime are
unchanged.

**S6. Profile invariant (self-healing).** `user_profile` belongs to the sub of
the currently valid stored token, or is absent. `enforceProfileInvariant()`
(`session-credential.ts`) removes a profile that breaks it and signals the
removal (kind `enforced`, which also drops the `ApiClientBase` in-memory copy).
It runs on every session signal, every `storage` event on the session keys,
`focus`, and `visibilitychange` to visible. It is installed once per page load
by `installProfileInvariantEnforcer()`, called at module load from
`lib/api/client.ts` (every page that uses the ApiClient singleton, which the
workspace and portal layouts do) and from `lib/api/public-auth.ts` (the portal
login, which deliberately does not import the ApiClient), and from
`session-identity.ts`. It cannot loop: with nothing to remove it does nothing
and signals nothing, so the signal fired by its own removal ends it.
Login order: `AuthApi.login`, `verifyMagicLink` and the public client write the
TOKEN first and the profile second. The token write fires a token-kind signal:
a previous user's profile is removed there (correct), then the profile write
fires a profile-kind signal and the new profile matches the new token, so it
stays. A profile-kind signal additionally tolerates a profile with no valid
token (a client that wrote the profile first, and the client's own unit of
work); a profile that contradicts a VALID token is removed on every trigger. A
valid token whose `sub` cannot be decoded (not a JWT; the backend never issues
one) cannot be judged and keeps the profile. Consequence, stated plainly: a
profile written with no valid token survives until the next token signal,
storage event, focus or visibility change.

## Where each rule is proved

- S2/S3 signals and epoch: `session-signal.test.ts`.
- S3 getProfile guard, real `AuthApi` + `ApiClientBase`:
  `src/lib/api/auth/auth.api.session.test.ts`; the credential capture:
  `src/lib/api/client-credential.test.ts`.
- S6 enforcer and login order: `session-credential.test.ts`.
- S1/S4 and the races through a bound page and the real transport, including
  the delayed cross-tab `storage` events: `session-identity.test.tsx`,
  `session-credential-race.test.tsx`.
