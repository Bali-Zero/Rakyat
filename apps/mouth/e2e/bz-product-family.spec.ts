import { test, expect, type Page } from "@playwright/test";
import { tokenFor } from "../src/lib/auth/session-test-utils";
import AxeBuilder from "@axe-core/playwright";

const E2E_PORT = process.env.BZ_E2E_PORT ?? "3000";
const KITA_ORIGIN = `http://kita.localhost:${E2E_PORT}`;
const MY_ORIGIN = `http://my.localhost:${E2E_PORT}`;

const syntheticPortalProfile = {
  id: "ui-test-client",
  email: "client@example.test",
  name: "Portal Test",
  role: "admin",
};

async function seedSyntheticPortalSession(
  page: Page,
  bearer = "synthetic-ui-test-token",
): Promise<void> {
  // A real login sets the HttpOnly session cookie as well as localStorage, and
  // proxy.ts gates workspace paths on that cookie server-side (SAETTA W-C R-A):
  // seeding localStorage alone is an anonymous visitor to the server.
  await page.context().addCookies(
    [KITA_ORIGIN, MY_ORIGIN].map((url) => ({
      name: "nz_access_token",
      value: bearer,
      url,
      httpOnly: true,
      sameSite: "Lax" as const,
    })),
  );
  await page.addInitScript(
    ({ profile, bearer }) => {
      localStorage.setItem("auth_token", bearer);
      localStorage.setItem("user_profile", JSON.stringify(profile));
    },
    { profile: syntheticPortalProfile, bearer },
  );

  await page.route("**/api/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;

    if (pathname === "/api/dashboard/summary") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          user: { email: syntheticPortalProfile.email, role: "admin" },
          stats: {},
          data: { practices: [], interactions: [] },
          system_status: "healthy",
          total_clients: 0,
          total_practices: 0,
        }),
      });
      return;
    }

    if (pathname === "/api/portal/dashboard") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          data: {
            visa: {
              status: "active",
              type: "Sample KITAS",
              expiryDate: "2027-04-18",
              daysRemaining: 263,
            },
            company: {
              status: "active",
              primaryCompanyName: "Sample Indonesia PT",
              totalCompanies: 1,
            },
            taxes: {
              status: "compliant",
              nextDeadline: "2026-09-30",
              daysToDeadline: 63,
            },
            documents: { total: 3, pending: 0 },
            messages: { unread: 0 },
            actions: [],
          },
        }),
      });
      return;
    }

    if (pathname === "/api/portal/dashboard/summary") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          activePractices: 1,
          pendingDocuments: 0,
          unreadMessages: 0,
        }),
      });
      return;
    }

    if (pathname === "/api/portal/messages") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          data: { messages: [], total: 0, unreadCount: 0 },
        }),
      });
      return;
    }

    if (pathname === "/api/portal/notifications") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          data: { notifications: [], unread_count: 0 },
        }),
      });
      return;
    }

    if (pathname === "/api/team/members") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify([]),
      });
      return;
    }

    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ success: true, data: {} }),
    });
  });
}

async function expectNoHorizontalOverflow(page: Page): Promise<void> {
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    )
    .toBe(true);
}

async function expectNoBlockingA11y(page: Page): Promise<void> {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  const blocking = results.violations.filter(
    (violation) =>
      violation.impact === "serious" || violation.impact === "critical",
  );

  expect(
    blocking.map(({ id, impact, help, nodes }) => ({
      id,
      impact,
      help,
      nodes: nodes.map(({ target, html, failureSummary }) => ({
        target,
        html,
        failureSummary,
      })),
    })),
  ).toEqual([]);
}

const syntheticPractice = {
  id: 707,
  client_id: 17,
  client_name: "Sample Client",
  assigned_to: "setup@example.test",
  assignee_member_id: "operator-setup",
  assignee_color_index: 1,
  assignee_name: "Setup Operator",
  assignee_department: "setup",
  can_edit_process: true,
  practice_type_code: "visa",
  practice_type_name: "Visa Extension",
  status: "waiting_payment",
  priority: "normal",
  payment_status: "unpaid",
  quoted_price: 2500000,
  start_date: "2026-11-10",
  created_at: "2026-11-08T00:00:00.000Z",
  updated_at: "2026-11-18T00:00:00.000Z",
};

const syntheticUnknownPractice = {
  ...syntheticPractice,
  id: 708,
  practice_type_name: "SPT Filing",
  assigned_to: "tax@example.test",
  assignee_member_id: "operator-tax",
  assignee_color_index: 2,
  assignee_name: "Tax Operator",
  assignee_department: "tax",
  status: "legacy_manual_review",
};

async function seedSyntheticProcessBoard(page: Page): Promise<void> {
  await seedSyntheticPortalSession(page, tokenFor("synthetic-operator"));
  await page.route("**/api/crm/practices**", async (route) => {
    const catalog = new URL(route.request().url()).pathname.endsWith(
      "/types/catalog",
    );
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        catalog
          ? { categories: [], total_services: 0 }
          : [
              syntheticPractice,
              syntheticUnknownPractice,
              {
                ...syntheticPractice,
                id: 709,
                client_name: "Sample Company",
                practice_type_name: "Company Setup",
                status: "completed",
                payment_status: "paid",
                paid_amount: 9500000,
                completion_date: "2026-11-18",
              },
              {
                ...syntheticUnknownPractice,
                id: 710,
                client_name: "Sample Studio",
                practice_type_name: "Tax Registration",
                status: "on_process",
                payment_status: "paid",
                paid_amount: 3750000,
                priority: "high",
              },
              {
                ...syntheticPractice,
                id: 711,
                client_name: "Sample Applicant",
                practice_type_name: "Stay Permit",
                status: "waiting_documents",
              },
              {
                ...syntheticPractice,
                id: 712,
                practice_type_name: "Work KITAS Renewal",
                client_name: "Sample Business",
                assignee_member_id: "operator-support",
                assignee_color_index: 4,
                assignee_name: "Case Officer",
                status: "on_process",
                payment_status: "paid",
                paid_amount: 12000000,
                priority: "urgent",
              },
            ],
      ),
    });
  });
}

function visualReportPath(view: string) {
  return process.env.CI
    ? `playwright-report/${test.info().project.name}-${view}.png`
    : test.info().outputPath(`${view}.png`);
}

test.describe("Bali Zero product-family noqata page Page", () => {
  test("Kita mobile shell stays within the viewport", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await seedSyntheticPortalSession(page);

    await page.goto(`${KITA_ORIGIN}/dashboard`, {
      waitUntil: "domcontentloaded",
    });
    await expect(page.locator("#bz-page-title")).toBeVisible({
      timeout: 30000,
    });

    await expectNoHorizontalOverflow(page);
    await expectNoBlockingA11y(page);
  });

  test("Kita desktop shell shows the homepage Bali Zero logo", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1440, height: 960 });
    await seedSyntheticPortalSession(page);

    await page.goto(`${KITA_ORIGIN}/dashboard`, {
      waitUntil: "domcontentloaded",
    });
    const logo = page
      .getByRole("link", { name: "Bali Zero — workspace home" })
      .locator("img");

    await expect(logo).toBeVisible({ timeout: 30000 });
    // The rail shows the round logo of the public homepage header
    // (components/r19/home/Entry.tsx), served through next/image.
    await expect(logo).toHaveAttribute(
      "src",
      /assets(%2F|\/)r19(%2F|\/)logo\.webp/,
    );
    await expectNoBlockingA11y(page);
  });

  test("An employee opens the next required document from their personal bonus card", async ({
    page,
  }) => {
    await seedSyntheticPortalSession(page, tokenFor("synthetic-operator"));
    const service = "Visa Extension";
    await page.route("**/api/auth/profile", (route) =>
      route.fulfill({
        json: {
          ...syntheticPortalProfile,
          id: "synthetic-operator",
          role: "consultant",
        },
      }),
    );
    await page.route(
      "**/api/crm/practices/invoice-company/suggestion?**",
      (route) =>
        route.fulfill({
          json: {
            autofill: false,
            suggested_company_id: null,
            companies: [],
          },
        }),
    );
    await page.route("**/api/hr/me/delivery-desk", (route) =>
      route.fulfill({
        json: {
          team_member_id: "synthetic-operator",
          on_payroll: true,
          month: "2026-11-01",
          target: 8,
          milestones: 5,
          holds: [
            {
              practice_id: 704,
              service: "Dependent KITAS",
              slot_key: "document_3",
              slot_label: "IMK — Re-entry Permit",
              due_on: "2026-11-21",
              payout_from_month: "2026-12-01",
              next_action: "upload_permit",
              email_state: "not_sent",
            },
          ],
          recent: [
            {
              practice_id: 703,
              service: "C1 Tourism",
              event_kind: "electronic_visa_issued",
              earned_at: "2026-11-18T08:00:00Z",
              awarded_at: "2026-11-18T08:00:00Z",
              status: "approved",
              amount_idr: 100000,
            },
            {
              practice_id: 702,
              service: "Tax Registration",
              event_kind: "service_delivered",
              earned_at: "2026-11-12T08:00:00Z",
              awarded_at: "2026-11-12T08:00:00Z",
              status: "pending",
              amount_idr: 150000,
            },
          ],
          totals: {
            pending: 150000,
            approved: 100000,
            paid: 0,
            awaiting_rate: 0,
            payroll_locked: 0,
          },
          next_case: {
            practice_id: 707,
            service,
            amount_idr: 50000,
            already_earned: false,
          },
        },
      }),
    );
    await page.route("**/api/crm/practices/707/delivery", (route) =>
      route.fulfill({
        json: {
          status: "on_process",
          needs_titles: false,
          slots: [{ key: "itk", label: "ITK", document_id: null }],
        },
      }),
    );
    await page.route("**/api/crm/practices/707/required-documents", (route) =>
      route.fulfill({ json: [] }),
    );
    await page.route("**/api/crm/practices/707", (route) =>
      route.fulfill({
        json: {
          ...syntheticPractice,
          assigned_to: syntheticPortalProfile.email,
          status: "on_process",
          payment_status: "paid",
          can_manage_delivery: true,
        },
      }),
    );
    await page.goto(`${KITA_ORIGIN}/dashboard`);
    const card = page.getByRole("region", { name: "My delivery bonuses" });
    const next = card.getByRole("link", { name: /Visa Extension.*ITK/ });
    await expect(next).toHaveAttribute("href", "/process/707#final-delivery");
    for (const [width, view] of [
      [1280, "desktop"],
      [390, "mobile"],
    ] as const) {
      await page.setViewportSize({ width, height: 900 });
      await expect(next).toBeVisible();
      // Capture the complete statement at this width without the fixed shell header
      // overlapping the first line when the mobile statement spans several scrolls.
      const height = await card.evaluate((element) =>
        Math.ceil(element.getBoundingClientRect().height),
      );
      if (height > 740)
        await page.setViewportSize({ width, height: height + 160 });
      await card.evaluate((element) =>
        element.scrollIntoView({ block: "center", behavior: "instant" }),
      );
      await card.screenshot({ path: visualReportPath(`bonus-${view}`) });
      await page.setViewportSize({ width, height: 900 });
    }
    const approved = card.getByRole("button", { name: /Approved/ });
    await approved.click();
    await expect(approved).toHaveAttribute("aria-pressed", "true");
    await expect(card.getByText("C1 Tourism", { exact: true })).toBeVisible();
    await expect(
      card.getByText("Tax Registration", { exact: true }),
    ).toHaveCount(0);
    await approved.click();
    await expect(approved).toHaveAttribute("aria-pressed", "false");
    await next.click();
    await expect(page).toHaveURL(/\/process\/707#final-delivery$/);
    await expect(page.locator("#final-delivery")).toContainText("ITK");
  });

  test("An operator understands the renewal upload and completes only after the permit is saved", async ({
    page,
  }) => {
    await seedSyntheticPortalSession(page, tokenFor("synthetic-operator"));
    await page.route("**/api/auth/profile", (route) =>
      route.fulfill({
        json: {
          ...syntheticPortalProfile,
          id: "synthetic-operator",
          role: "consultant",
        },
      }),
    );
    await page.route(
      "**/api/crm/practices/invoice-company/suggestion?**",
      (route) =>
        route.fulfill({
          json: { autofill: false, suggested_company_id: null, companies: [] },
        }),
    );
    let status = "on_process";
    let uploaded = false;
    let uploadCount = 0;
    const label = "Renewed ITK — Visit Stay Permit";
    const state = () => ({
      status,
      bonus_slot: null,
      bonus_event: null,
      bonus_trigger: "service_delivered",
      needs_titles: false,
      visa_followup: null,
      slots: [
        {
          key: "document_1",
          label,
          condition: null,
          document_id: uploaded ? 91 : null,
          file_name: uploaded ? "renewed-itk.pdf" : null,
          waiver_reason: null,
          reusable_document_id: null,
          issued_on: null,
        },
      ],
    });
    await page.route("**/api/crm/practices/713", (route) =>
      route.fulfill({
        json: {
          ...syntheticPractice,
          id: 713,
          practice_type_code: "ext_b1_voa",
          practice_type_name: "Extension B1 - VOA",
          assigned_to: syntheticPortalProfile.email,
          status,
          payment_status: "paid",
          can_manage_delivery: true,
        },
      }),
    );
    await page.route("**/api/crm/practices/713/required-documents", (route) =>
      route.fulfill({ json: [] }),
    );
    await page.route("**/api/crm/practices/713/delivery", (route) =>
      route.fulfill({ json: state() }),
    );
    await page.route(
      "**/api/crm/practices/713/delivery/slots/document_1",
      (route) => {
        expect(route.request().method()).toBe("POST");
        expect(route.request().postDataJSON().file_name).toBe(
          "renewed-itk.pdf",
        );
        uploaded = true;
        uploadCount++;
        return route.fulfill({ json: state() });
      },
    );
    await page.route("**/api/crm/practices/713/delivery/complete", (route) => {
      expect(uploaded).toBe(true);
      expect(route.request().method()).toBe("POST");
      status = "completed";
      return route.fulfill({ json: state() });
    });
    await page.goto(`${KITA_ORIGIN}/process/713`);
    const card = page.getByRole("region", { name: "Final delivery" });
    const complete = card.getByRole("button", {
      name: "Mark practice as completed",
    });
    await expect(
      card.getByRole("heading", { name: "Upload final documents" }),
    ).toBeVisible();
    await expect(
      card.getByText(
        "Upload Renewed ITK — Visit Stay Permit before completing this practice.",
      ),
    ).toBeVisible();
    await expect(card.getByText(/Each file saves automatically/)).toBeVisible();
    await expect(complete).toBeDisabled();
    await expect(card.getByRole("checkbox")).toHaveCount(0);
    const capture = async (width: number, theme: string, name: string) => {
      await page.setViewportSize({ width, height: 1000 });
      await page.evaluate((t) => {
        document.documentElement.dataset.theme = t;
      }, theme);
      const height = await card.evaluate((e) =>
        Math.ceil(e.getBoundingClientRect().height),
      );
      await page.setViewportSize({
        width,
        height: Math.max(1000, height + 160),
      });
      await card.evaluate((e) =>
        e.scrollIntoView({ block: "center", behavior: "instant" }),
      );
      await card.screenshot({ path: visualReportPath(name) });
      await expectNoHorizontalOverflow(page);
      const audit = await new AxeBuilder({ page })
        .include("#final-delivery")
        .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
        .analyze();
      expect(
        audit.violations.filter(
          (v) => v.impact === "serious" || v.impact === "critical",
        ),
      ).toEqual([]);
    };
    await capture(1280, "operative-light", "delivery-empty-desktop");
    await capture(390, "operative-light", "delivery-empty-mobile");
    await capture(390, "operative-dark", "delivery-empty-dark");
    await card.getByLabel(`Upload document — ${label}`).setInputFiles({
      name: "renewed-itk.pdf",
      mimeType: "application/pdf",
      buffer: Buffer.from("synthetic permit fixture"),
    });
    await expect(
      card.getByText("renewed-itk.pdf", { exact: true }),
    ).toBeVisible();
    await expect(
      card.getByText("Ready to complete", { exact: true }),
    ).toBeVisible();
    await expect(complete).toBeEnabled();
    expect(uploadCount).toBe(1);
    await capture(1280, "operative-light", "delivery-ready-desktop");
    await complete.click();
    await expect(
      card.getByRole("heading", { name: "Final documents delivered" }),
    ).toBeVisible();
    await expect(complete).toHaveCount(0);
    await expect(
      card.getByText("renewed-itk.pdf", { exact: true }),
    ).toBeVisible();
    expect(uploadCount).toBe(1);
  });

  test("Process keeps a horizontally scannable board and keyboard-accessible cards on mobile", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await seedSyntheticProcessBoard(page);

    await page.goto(`${KITA_ORIGIN}/process`, {
      waitUntil: "domcontentloaded",
    });

    const board = page.getByLabel("Process board");
    const card = page.getByRole("button", {
      name: "Open process 707: Visa Extension",
    });
    const cardSurface = page.getByTestId("process-card-707");

    await expect(board).toBeVisible({ timeout: 30000 });
    await expect(card).toBeVisible();
    await card.focus();
    await expect(card).toBeFocused();
    await expectNoHorizontalOverflow(page);
    await expect(cardSurface).toContainText("Waiting Payment");
    await expect(page.getByTestId("process-card-708")).toContainText(
      "Needs review · Legacy Manual Review",
    );
    await expectNoBlockingA11y(page);
    await page.getByTestId("process-card-708").scrollIntoViewIfNeeded();
    await page.screenshot({
      path: visualReportPath("process-mobile"),
    });
  });

  test("Process renders square and circular department cards with stable color through completion", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1440, height: 960 });
    await seedSyntheticProcessBoard(page);
    await page.goto(`${KITA_ORIGIN}/process`);
    await page
      .getByRole("button", { name: /^Completed\s*\d+/, expanded: false })
      .click();
    const face = (id: number) =>
      page.getByTestId(`process-card-${id}`).locator("div[data-department]");
    for (const id of [707, 708, 709]) {
      const box = await face(id).boundingBox();
      expect(box).toBeTruthy();
      expect(Math.abs(box!.width - box!.height)).toBeLessThanOrEqual(1);
    }
    await expect(face(708)).toHaveCSS("border-top-left-radius", "50%");
    await expect(face(707)).not.toHaveCSS("border-top-left-radius", "50%");
    const colorOf = async (id: number) =>
      face(id).evaluate((element) => {
        const style = getComputedStyle(element);
        return {
          hue: style.getPropertyValue("--owner-hue"),
          stage: style.getPropertyValue("--stage-strength"),
          fill: style.backgroundColor,
        };
      });
    const setup = await colorOf(707),
      tax = await colorOf(708),
      complete = await colorOf(709);
    expect(setup.hue).toBeTruthy();
    expect(complete.hue).toBe(setup.hue);
    expect(tax.hue).not.toBe(setup.hue);
    expect(complete.stage.trim()).toBe("100%");
    expect(complete.fill).not.toBe(setup.fill);
    await expectNoHorizontalOverflow(page);
    await expectNoBlockingA11y(page);
    await page.screenshot({
      path: visualReportPath("process-desktop"),
    });
    const pack = page.getByTestId("process-card-710");
    let reads = 0;
    await page.route("**/api/crm/practices/710/delivery", (route) => {
      reads++;
      return route.fulfill({
        json: {
          needs_titles: false,
          slots: [
            {
              key: "document_1",
              label: "Tax registration certificate",
              document_id: 85,
              condition: null,
              waiver_reason: null,
            },
            {
              key: "document_2",
              label: "Registration confirmation",
              document_id: null,
              condition: null,
              waiver_reason: null,
            },
          ],
        },
      });
    });
    await pack.getByText("Delivery documents", { exact: true }).click();
    await expect(pack.getByText("Tax registration certificate")).toBeVisible();
    await expect(pack.getByText("Missing", { exact: true })).toBeVisible();
    expect(reads).toBe(1);
    await expect(
      pack.getByRole("link", { name: "Open delivery" }),
    ).toHaveAttribute("href", "/process/710#final-delivery");
  });

  test("My mobile shell stays within the viewport and labels Messages canonically", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await seedSyntheticPortalSession(page);

    await page.goto(`${MY_ORIGIN}/portal/messages`, {
      waitUntil: "domcontentloaded",
    });
    const messages = page.getByRole("link", { name: /Messages/i });

    await expect(messages).toBeVisible({ timeout: 30000 });
    await expect(messages).toHaveAttribute("href", "/portal/messages");
    await expectNoHorizontalOverflow(page);
    await expectNoBlockingA11y(page);
  });
});
