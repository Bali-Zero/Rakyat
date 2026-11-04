/**
 * Pure validator for initial server-rendered HTML (SSR contract — BZ-025).
 * Strips all <script> blocks (including application/ld+json) before asserting visible markup;
 * JSON-LD is parsed separately from rawHtml via extractJsonLdBlocks.
 */

export interface SsrValidationOptions {
  requireExactOgUrl?: boolean;
}

const JSON_LD_SCRIPT_RE =
  /<script\b[^>]*\btype=["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi;
const ALL_SCRIPTS_RE = /<script\b[\s\S]*?<\/script>/gi;

export function stripExecutableScripts(rawHtml: string): string {
  return rawHtml.replace(ALL_SCRIPTS_RE, "");
}

export function extractJsonLdBlocks(rawHtml: string): unknown[] {
  const blocks: unknown[] = [];
  const re = new RegExp(JSON_LD_SCRIPT_RE.source, "gi");
  let match: RegExpExecArray | null;
  while ((match = re.exec(rawHtml)) !== null) {
    const body = match[1]?.trim();
    if (body) blocks.push(JSON.parse(body));
  }
  return blocks;
}

function hasMetaOrLink(
  html: string,
  tag: "link" | "meta",
  attrA: string,
  valA: string,
  attrB: string,
  valB: string,
): boolean {
  const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const a = `\\b${attrA}=["']${esc(valA)}["']`;
  const b = `\\b${attrB}=["']${esc(valB)}["']`;
  return (
    new RegExp(`<${tag}\\b[^>]*${a}[^>]*${b}`, "i").test(html) ||
    new RegExp(`<${tag}\\b[^>]*${b}[^>]*${a}`, "i").test(html)
  );
}

export function assertInitialSsrHtml(
  rawHtml: string,
  serviceSlug: string,
  options: SsrValidationOptions = {},
): void {
  if (!rawHtml || typeof rawHtml !== "string") {
    throw new Error("Empty or non-string initial HTML response");
  }

  const visibleMarkup = stripExecutableScripts(rawHtml);

  if (!/<h1\b[^>]*>[\s\S]*?<\/h1>/i.test(visibleMarkup)) {
    throw new Error(
      `Missing server-rendered <h1> heading in initial HTML for /services/${serviceSlug}`,
    );
  }
  if (!/<h[23]\b[^>]*>[\s\S]*?Pricing[\s\S]*?<\/h[23]>/i.test(visibleMarkup)) {
    throw new Error(
      `Missing server-rendered "Pricing" heading in visible HTML for /services/${serviceSlug}`,
    );
  }
  if (
    !/<h[23]\b[^>]*>[\s\S]*?Frequently Asked Questions[\s\S]*?<\/h[23]>/i.test(
      visibleMarkup,
    )
  ) {
    throw new Error(
      `Missing server-rendered "Frequently Asked Questions" heading in visible HTML for /services/${serviceSlug}`,
    );
  }

  if (serviceSlug === "visa") {
    const prices = ["2.300.000", "3.600.000", "17.000.000", "13.000.000"];
    if (!prices.some((p) => visibleMarkup.includes(p))) {
      throw new Error(
        "Missing server-rendered IDR price values in visible HTML for /services/visa",
      );
    }
  }

  if (
    !/<dl\b/i.test(visibleMarkup) ||
    !/\bitemscope\b/i.test(visibleMarkup) ||
    !/\bitemprop=/i.test(visibleMarkup) ||
    !/\bitemtype=["']https:\/\/schema\.org\/(GovernmentService|ProfessionalService)["']/i.test(
      visibleMarkup,
    )
  ) {
    throw new Error(
      `Missing semantic <dl> microdata attributes in initial HTML for /services/${serviceSlug}`,
    );
  }
  for (const prop of ["serviceType", "hoursAvailable", "areaServed"]) {
    if (!visibleMarkup.includes(prop)) {
      throw new Error(
        `Missing microdata property "${prop}" in initial HTML for /services/${serviceSlug}`,
      );
    }
  }

  const expectedUrl = `https://balizero.com/services/${serviceSlug}`;
  if (
    !hasMetaOrLink(
      visibleMarkup,
      "link",
      "rel",
      "canonical",
      "href",
      expectedUrl,
    )
  ) {
    throw new Error(
      `Missing or mismatched canonical link (${expectedUrl}) in initial HTML for /services/${serviceSlug}`,
    );
  }

  if (
    options.requireExactOgUrl &&
    !hasMetaOrLink(
      visibleMarkup,
      "meta",
      "property",
      "og:url",
      "content",
      expectedUrl,
    )
  ) {
    throw new Error(
      `Missing or mismatched og:url (${expectedUrl}) in initial HTML for /services/${serviceSlug}`,
    );
  }

  const jsonLdBlocks = extractJsonLdBlocks(rawHtml);
  if (jsonLdBlocks.length === 0) {
    throw new Error(
      `No <script type="application/ld+json"> blocks found in initial HTML for /services/${serviceSlug}`,
    );
  }

  const serializedJsonLd = JSON.stringify(jsonLdBlocks);
  if (!serializedJsonLd.includes("https://schema.org")) {
    throw new Error(
      `Missing https://schema.org @context in JSON-LD for /services/${serviceSlug}`,
    );
  }
  if (
    !serializedJsonLd.includes('"GovernmentService"') &&
    !serializedJsonLd.includes('"ProfessionalService"')
  ) {
    throw new Error(
      `Missing GovernmentService or ProfessionalService JSON-LD schema in initial HTML for /services/${serviceSlug}`,
    );
  }
  if (
    !serializedJsonLd.includes('"FAQPage"') ||
    !serializedJsonLd.includes('"Question"') ||
    !serializedJsonLd.includes('"Answer"')
  ) {
    throw new Error(
      `Missing FAQPage / Question / Answer JSON-LD schema in initial HTML for /services/${serviceSlug}`,
    );
  }
}
