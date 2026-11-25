import { TOKEN_PATH_RULES, type TokenPathRule } from "./sentry-url-scrub";

export const TELEMETRY_PATH_REDACTION = "<redacted>";

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
