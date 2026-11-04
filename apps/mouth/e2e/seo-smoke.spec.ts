import { test, expect } from "@playwright/test";
import { assertInitialSsrHtml } from "../src/lib/seo/ssr-html-contract";

/**
 * SEO Smoke Test - SSR & Schema Validation (BZ-025)
 * Verifies raw server-rendered wire HTML (before hydration) and hydrated DOM parity.
 */

const BASE_URL = process.env.E2E_BASE_URL || "http://localhost:3000";
const BOT_UA =
  "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)";

test.describe("SEO Smoke Tests - SSR & Schema Validation @offline", () => {
  test("Wire SSR HTML contains headings, pricing, microdata, and JSON-LD before hydration", async ({
    request,
  }) => {
    for (const slug of ["visa", "company", "tax", "property"] as const) {
      const response = await request.get(`${BASE_URL}/services/${slug}`, {
        headers: { "User-Agent": BOT_UA },
      });
      expect(response.status()).toBe(200);
      expect(response.headers()["content-type"]).toContain("text/html");
      assertInitialSsrHtml(await response.text(), slug);
    }
  });

  test("Hydrated DOM parity on /services/visa", async ({ page }) => {
    await page.goto(`${BASE_URL}/services/visa`);
    await expect(page).toHaveTitle(/Visa.*Bali/i);
    await expect(page.locator("h1")).toContainText(/Visa|Immigration/i);
    await expect(page.locator("dl[itemscope]")).toHaveCount(1);

    const metaDescription = await page
      .locator('meta[name="description"]')
      .getAttribute("content");
    expect(metaDescription?.length).toBeGreaterThan(50);

    const canonical = await page
      .locator('link[rel="canonical"]')
      .getAttribute("href");
    expect(canonical).toBe("https://balizero.com/services/visa");
  });
});
