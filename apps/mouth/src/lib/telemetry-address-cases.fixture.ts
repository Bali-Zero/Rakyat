import { TOKEN_PATH_RULES, type TokenPathRule } from "./sentry-url-scrub";

export const TELEMETRY_PATH_REDACTION = "<redacted>";

type TelemetryAddressCase = {
  input: string;
  sentry: string;
  ga4: string;
};

export const TELEMETRY_ADDRESS_CASES: readonly TelemetryAddressCase[] = [
  {
    input: "https://host.example?q=a/b",
    sentry: "https://host.example/",
    ga4: "https://host.example/",
  },
  {
    input: "https://host.example#x/y",
    sentry: "https://host.example/",
    ga4: "https://host.example/",
  },
  {
    input: "https://host.example/x?utm_term=a/b&utm_source=ok",
    sentry: "https://host.example/x",
    ga4: "https://host.example/x?utm_term=[redacted]&utm_source=ok",
  },
  {
    input: "/clients?next=https://host.example/a/b",
    sentry: "/clients",
    ga4: "[redacted]",
  },
  { input: "/x#a?b", sentry: "/x", ga4: "[redacted]" },
  { input: "/a//b/c", sentry: "/a//b/c", ga4: "[redacted]" },
  { input: "//host.example", sentry: "//host.example/", ga4: "[redacted]" },
  {
    input: "//host.example/visa/clock/[hash]",
    sentry: "//host.example/visa/clock/[hash]",
    ga4: "[redacted]",
  },
  {
    input: "https://host.example/api/visa/clock/{hash}?x=1",
    sentry: "https://host.example/api/visa/clock/{hash}",
    ga4: "https://host.example/api/visa/clock/{hash}",
  },
  {
    input: "https://u:p@host.example/x",
    sentry: "https://host.example/x",
    ga4: "https://host.example/x",
  },
  {
    input: "http://[::1]:8080/visa/clock/zq-synthetic",
    sentry: "http://[::1]:8080/visa/clock/[REDACTED]",
    ga4: "http://[::1]:8080/visa/clock/[redacted]",
  },
  {
    input: "https:///u:p@host.example/x",
    sentry: "[REDACTED]",
    ga4: "[redacted]",
  },
  {
    input: "https:////host.example/x",
    sentry: "[REDACTED]",
    ga4: "[redacted]",
  },
  {
    input: "/visa/clo\nck/zq-synthetic",
    sentry: "[REDACTED]",
    ga4: "[redacted]",
  },
  {
    input: "/visa/clo\tck/zq-synthetic",
    sentry: "[REDACTED]",
    ga4: "[redacted]",
  },
  {
    input: "/visa/x/../clock/zq-synthetic",
    sentry: "[REDACTED]",
    ga4: "[redacted]",
  },
  { input: " https://host.example/x", sentry: "[REDACTED]", ga4: "[redacted]" },
  { input: "visa/clock/zq-synthetic", sentry: "[REDACTED]", ga4: "[redacted]" },
  {
    input: "\\visa\\clock\\zq-synthetic",
    sentry: "[REDACTED]",
    ga4: "[redacted]",
  },
  {
    input: "https:\\host.example\\visa\\clock\\zq-synthetic",
    sentry: "[REDACTED]",
    ga4: "[redacted]",
  },
  {
    input: "blob:https://host.example/0000",
    sentry: "blob:[REDACTED]",
    ga4: "[redacted]",
  },
  {
    input: "data:image/png;base64,AAAA",
    sentry: "data:image/png",
    ga4: "[redacted]",
  },
  { input: "data:,x", sentry: "data:[REDACTED]", ga4: "[redacted]" },
  { input: "about:blank", sentry: "about:blank", ga4: "[redacted]" },
  { input: "mailto:n@host.example", sentry: "[REDACTED]", ga4: "[redacted]" },
  {
    input: "/visa/clock/zq-a/../zq-b",
    sentry: "[REDACTED]",
    ga4: "[redacted]",
  },
  {
    input: "https://host.example/visa/clock/zq-a/%2e%2e/zq-b",
    sentry: "[REDACTED]",
    ga4: "[redacted]",
  },
  { input: "/visa/clock/zq-a/./zq-b", sentry: "[REDACTED]", ga4: "[redacted]" },
  {
    input: "https://host.example/visa/clock/zq-a/../zq-b",
    sentry: "[REDACTED]",
    ga4: "[redacted]",
  },
  {
    input: "https://host.example/visa/clock/zq-a/./zq-b",
    sentry: "[REDACTED]",
    ga4: "[redacted]",
  },
  {
    input: "/api/visa%2Fclock/zq-synthetic",
    sentry: "[REDACTED]",
    ga4: "[redacted]",
  },
  {
    input: "https://host.example/api/visa/clo%63k/zq-synthetic",
    sentry: "[REDACTED]",
    ga4: "[redacted]",
  },
  {
    input: "https://host.example/x%20y/%E2%9C%93",
    sentry: "https://host.example/x%20y/%E2%9C%93",
    ga4: "https://host.example/x%20y/%E2%9C%93",
  },
  { input: "http:/host.example/x", sentry: "[REDACTED]", ga4: "[redacted]" },
  {
    input: "HTTPS://HOST.EXAMPLE/visa/clock/zq-synthetic",
    sentry: "https://host.example/visa/clock/[REDACTED]",
    ga4: "https://host.example/visa/clock/[redacted]",
  },
  { input: "//host.example?x=1", sentry: "//host.example/", ga4: "[redacted]" },
  {
    input: "https://host.example/visa/clo\tck/zq-synthetic",
    sentry: "[REDACTED]",
    ga4: "[redacted]",
  },
  {
    input: "https://host.example/visa/clo\rck/zq-synthetic",
    sentry: "[REDACTED]",
    ga4: "[redacted]",
  },
  {
    input: "data:application/zq-synthetic,x",
    sentry: "data:[REDACTED]",
    ga4: "[redacted]",
  },
] as const;

type TelemetryFreeTextCase = { input: string; expected: string };

export const TELEMETRY_FREE_TEXT_CASES: readonly TelemetryFreeTextCase[] = [
  {
    input: "GET https://host.example/visa/x/../clock/zq-synthetic failed",
    expected: "GET [REDACTED] failed",
  },
  {
    input: "fetch /api/visa%2Fclock/zq-synthetic",
    expected: "fetch [REDACTED]",
  },
  { input: "visa/clo\tck/zq-synthetic", expected: "visa/clo\tck/[REDACTED]" },
  { input: "visa/clo\rck/zq-synthetic", expected: "visa/clo\rck/[REDACTED]" },
  { input: "visa/clo\nck/zq-synthetic", expected: "visa/clo\nck/[REDACTED]" },
  {
    input: "\\visa\\clock\\zq-synthetic",
    expected: "\\visa\\clock\\[REDACTED]",
  },
  { input: "visa/clock/zq-synthetic", expected: "visa/clock/[REDACTED]" },
  {
    input: "see v1.2.3 and /clients/new",
    expected: "see v1.2.3 and /clients/new",
  },
  { input: "a/b ./c", expected: "a/b [REDACTED]" },
] as const;

export const TEST_TOKEN_PATH_RULES: readonly TokenPathRule[] =
  TOKEN_PATH_RULES.map((rule) =>
    rule.prefix === "/prime/proposal/"
      ? {
          ...rule,
          // Synthetic test-only static child.
          staticChildren: ["synthetic-static-child"],
        }
      : rule,
  );

type TelemetryPathCase = {
  path: string;
  expected: string;
  rules?: readonly TokenPathRule[];
};

export const TELEMETRY_PATH_CASES: readonly TelemetryPathCase[] = [
  { path: "/visa/clock/[hash]", expected: "/visa/clock/[hash]" },
  { path: "/visa/clock/[...rest]", expected: "/visa/clock/[...rest]" },
  {
    path: "/api/visa/clock/{hash}",
    expected: "/api/visa/clock/{hash}",
  },
  { path: "/api/visa/clock/:hash", expected: "/api/visa/clock/:hash" },
  { path: "/api/visa/clock/email", expected: "/api/visa/clock/email" },
  {
    path: "/prime/proposal/synthetic-static-child",
    expected: "/prime/proposal/synthetic-static-child",
    rules: TEST_TOKEN_PATH_RULES,
  },
  ...TOKEN_PATH_RULES.flatMap(({ prefix }) => [
    {
      path: `${prefix}zq-synthetic`,
      expected: `${prefix}${TELEMETRY_PATH_REDACTION}`,
    },
    {
      path: `${prefix}unknown-child`,
      expected: `${prefix}${TELEMETRY_PATH_REDACTION}`,
    },
  ]),
] as const;

export const TELEMETRY_CAMPAIGN_CASES = [
  [
    "utm_content=https%3A%2F%2Fexample.test%2Fportal%2Fmagic%3Ftoken%3Dzq-synthetic",
    "utm_content=[redacted]",
  ],
  [
    "utm_content=https%253A%252F%252Fexample.test%252Fportal%252Fmagic%253Ftoken%253Dzq-synthetic",
    "utm_content=[redacted]",
  ],
  [
    "utm_content=https%25253A%25252F%25252Fexample.test%25252Fportal%25252Fmagic%25253Ftoken%25253Dzq-synthetic",
    "utm_content=[redacted]",
  ],
  [
    "utm_content=https%3A%2F%2Fexample.test%2Fportal%2Fmagic%3Ftoken%3Dzq-synthetic+with+text",
    "utm_content=[redacted]",
  ],
  [
    "utm_content=HTTPS%3A%2F%2FEXAMPLE.TEST%2FPORTAL%2FMAGIC%3FTOKEN%3DZQ-SYNTHETIC",
    "utm_content=[redacted]",
  ],
  ["utm_term=a%3Ab", "utm_term=[redacted]"],
  ["utm_term=a/b", "utm_term=[redacted]"],
  ["utm_content=x%2By", "utm_content=[redacted]"],
  ["utm_content=%E0%A4", "utm_content=[redacted]"],
  ["utm_content=a%2520b", "utm_content=[redacted]"],
  ["utm_medium=email", "utm_medium=email"],
  ["utm_source=balizero_web", "utm_source=balizero_web"],
  ["utm_campaign=whatsapp_cta", "utm_campaign=whatsapp_cta"],
  ["utm_campaign=Bali+Zero", "utm_campaign=Bali+Zero"],
  ["utm_content=spring-sale_2026", "utm_content=spring-sale_2026"],
  ["gclid=Cj0KCQ-abc_123", "gclid=Cj0KCQ-abc_123"],
  ["not_allowed=value", ""],
] as const;
