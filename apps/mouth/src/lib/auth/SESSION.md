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

**S3. Epoch.** An in-memory per-tab counter (`getSessionEpoch()`) is bumped on
every signal above and on every session-key `storage` event from another tab.
`AuthApi.getProfile()` reads the epoch before issuing the request and writes
`user_profile` only if the epoch is unchanged when the response arrives: a
response issued before a logout, login, token change or expiry is handed back
to its caller but never written. `login` and `verifyMagicLink` are NOT guarded
on purpose: they establish a new session and the last completed login wins;
guarding them would drop a legitimate login whenever `request()` itself clears
an expired token on the way out.

**S4. Helper for pages.** `session-identity.ts` exports
`readSessionIdentity(): {sub, epoch} | null` (pure read: removes nothing,
dispatches nothing, never throws), `subscribeSessionChange(cb)` (signal +
`storage` + `focus` + `visibilitychange`, returns the unsubscribe),
`useSessionIdentity()` (null on the server and first render, then live; it also
re-reads when the token reaches its expiry, so a page learns about an expiry
nobody removed), and two helpers `isSameIdentity` / `acceptForIdentity`. A page
binds data to identity by:
1. issuing a request only when an identity exists;
2. accepting the response only if a FRESH `readSessionIdentity()` at response
   time equals the identity at issue time (sub AND epoch) AND, when the
   response names its owner (e.g. `team_member_id`), the owner equals `sub`
   (`acceptForIdentity`);
3. clearing its data the moment the identity changes or disappears.
Because the epoch moves on every profile write too, a same-user profile
refresh makes a bound page refetch once; that is the price of a rule with no
exceptions.

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

## Where each rule is proved

- S2/S3 signals and epoch: `session-signal.test.ts`.
- S3 getProfile guard, real `AuthApi` + `ApiClientBase`:
  `src/lib/api/auth/auth.api.session.test.ts`.
- S1/S4 and the two races through a bound page: `session-identity.test.tsx`.
