import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@/i18n";

const { trackVisaCTA } = vi.hoisted(() => ({ trackVisaCTA: vi.fn() }));

vi.mock("@/lib/analytics", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/analytics")>()),
  trackVisaCTA,
}));

import { SecondHomeLanding } from "./SecondHomeLanding";

/**
 * Guard: the two fit-check CTAs (hero + footer → /visa/second-home/studio)
 * each emit visa_cta_click with their position. Before this guard they
 * emitted no funnel event, so the page's main conversion click was invisible.
 */
describe("SecondHomeLanding fit-check CTA funnel event", () => {
  beforeEach(() => {
    localStorage.clear();
    trackVisaCTA.mockClear();
  });

  it.each([
    ["hero-fit-check-cta", "hero"],
    ["footer-fit-check-cta", "footer"],
  ])("%s emits visa_cta_click with position=%s", (testId, position) => {
    render(
      <I18nProvider>
        <SecondHomeLanding />
      </I18nProvider>,
    );

    const cta = screen.getByTestId(testId);
    expect(cta).toHaveAttribute("href", "/visa/second-home/studio");
    fireEvent.click(cta);

    expect(trackVisaCTA).toHaveBeenCalledTimes(1);
    expect(trackVisaCTA).toHaveBeenCalledWith("cta_click", {
      source: "second_home",
      position,
    });
  });
});
