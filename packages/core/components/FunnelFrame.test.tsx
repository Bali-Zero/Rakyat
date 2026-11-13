import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { FunnelFrame } from "./FunnelFrame";

// Wire values of the backend LeadSource enum, read from the Python source so a
// frontend tag that the backend taxonomy does not know fails here (DAI-133).
const LEAD_SOURCE_PY = path.resolve(
  __dirname,
  "../../../apps/backend-rag/backend/services/lead_capture/source.py",
);
function leadSourceValues(): Set<string> {
  const text = readFileSync(LEAD_SOURCE_PY, "utf8");
  const body = text
    .split("class LeadSource(str, Enum):")[1]
    .split("\n    @")[0];
  return new Set(
    [...body.matchAll(/^\s+[A-Z_]+ = "([a-z_]+)"$/gm)].map((m) => m[1]),
  );
}

function waSource(container: HTMLElement): string | undefined {
  const href = container
    .querySelector('a[href^="https://wa.me/"]')
    ?.getAttribute("href");
  const text = decodeURIComponent(
    new URL(href ?? "https://x").searchParams.get("text") ?? "",
  );
  return /\[source:([^\]]+)\]/.exec(text)?.[1];
}

describe("FunnelFrame", () => {
  it("renders children, TrustBand, and CTAHandoff", () => {
    const { getByText, getByRole } = render(
      <FunnelFrame
        funnel="visa"
        sessionId="abc"
        step={{ current: 2, total: 5 }}
        trust={{ rating: "4.9", reviewCount: 1234 }}
      >
        <div>QUIZ_BODY</div>
      </FunnelFrame>,
    );
    expect(getByText("QUIZ_BODY")).toBeTruthy();
    expect(getByText(/4\.9/)).toBeTruthy();
    expect(getByRole("group", { name: /next actions/i })).toBeTruthy();
  });

  it("sets data-funnel on the container", () => {
    const { container } = render(
      <FunnelFrame funnel="kbli" sessionId="x">
        <span>body</span>
      </FunnelFrame>,
    );
    expect(container.firstChild).toHaveAttribute("data-funnel", "kbli");
  });

  it.each(["visa", "kbli", "tax", "property"] as const)(
    "tags the %s WhatsApp handoff with a LeadSource value",
    (funnel) => {
      const values = leadSourceValues();
      expect(values.size).toBeGreaterThan(5); // blindness guard on the parse
      const { container } = render(
        <FunnelFrame funnel={funnel} sessionId="s1">
          <span>body</span>
        </FunnelFrame>,
      );
      const source = waSource(container);
      expect(source).toBeDefined();
      expect(values.has(source!)).toBe(true);
    },
  );

  it("keeps the funnel name in the WhatsApp handoff payload", () => {
    const { container } = render(
      <FunnelFrame funnel="tax" sessionId="s1">
        <span>body</span>
      </FunnelFrame>,
    );
    const href = container
      .querySelector('a[href^="https://wa.me/"]')!
      .getAttribute("href")!;
    expect(decodeURIComponent(href)).toContain('"funnel":"tax"');
  });
});
