import * as Sentry from "@sentry/nextjs";

import { dropProtectedUiBreadcrumb } from "@/lib/sentry-breadcrumbs";
import { IGNORE_ERRORS, isKnownNoise } from "@/lib/sentry-noise";
import { scrubSentryUrls } from "@/lib/sentry-url-scrub";

Sentry.init({
  dataCollection: {
    userInfo: false,
    cookies: false,
    httpHeaders: false,
    httpBodies: [],
    urlQueryParams: false,
    graphQL: { document: false, variables: false },
    genAI: { inputs: false, outputs: false },
    mcp: { inputs: false, outputs: false },
    databaseQueryData: false,
    queues: false,
    stackFrameVariables: false,
  },
  dsn: process.env.NEXT_PUBLIC_SENTRY_DSN,
  environment: process.env.NODE_ENV,
  tracesSampleRate: process.env.NODE_ENV === "production" ? 0.1 : 1.0,
  // Session Replay is OFF on purpose (no replayIntegration, no replays*SampleRate): it records
  // every navigation URL (`/clients?q=<typed text>` from the command palette) in the replay
  // event and the recording, and `beforeSend` never runs for replay. A test fails if it returns.
  ignoreErrors: [...IGNORE_ERRORS],
  // Clicks and keys inside a page's protected HR region leave no breadcrumb.
  beforeBreadcrumb(breadcrumb, hint) {
    const kept = dropProtectedUiBreadcrumb(breadcrumb, hint);
    return kept ? scrubSentryUrls(kept) : kept;
  },
  beforeSend(event) {
    // Non inviare errori in development
    if (process.env.NODE_ENV === "development") {
      return null;
    }
    // 28% of this org's production errors were dropped for quota over 7 days
    // (measured 2026-08-28), and which ones get dropped is decided by arrival
    // order rather than importance. src/lib/sentry-noise.ts says what is
    // filtered and — the load-bearing half — what deliberately is not: 401/403
    // stays, because muting it is how #5096 would come back invisibly.
    return isKnownNoise(event) ? null : scrubSentryUrls(event);
  },
});
