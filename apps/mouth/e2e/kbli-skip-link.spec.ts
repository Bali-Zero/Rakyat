import { expect, test } from "@playwright/test";

test.describe("KBLI skip link page Page", () => {
  test("the first Tab reveals the link and Enter focuses the content", async ({
    page,
    browserName,
  }) => {
    await page.goto("/kbli/55203");
    const skip = page.getByRole("link", { name: "Skip to main content" });
    await expect(skip).not.toBeInViewport();
    // With macOS WebKit's default keyboard preferences, Option+Tab includes links.
    const tab =
      browserName === "webkit" && process.platform === "darwin"
        ? "Alt+Tab"
        : "Tab";
    await page.keyboard.press(tab);
    await expect(skip).toBeFocused();
    await expect(skip).toBeInViewport({ ratio: 1 });
    await expect(skip).toHaveCSS("background-color", "rgb(164, 75, 54)");
    await expect(skip).toHaveCSS("color", "rgb(255, 252, 247)");
    await expect(skip).toHaveCSS("outline-style", "solid");
    await skip.click({ trial: true }); // the fixed header must not cover it
    await page.keyboard.press("Enter");
    await expect(page.locator("#main-content")).toBeFocused();
  });
});
