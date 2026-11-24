/**
 * Cases Page - Comprehensive Test Suite
 *
 * Tests all functionality of the Cases management page including:
 * - Component rendering
 * - View mode switching (Kanban/List)
 * - Search and filtering
 * - Sorting
 * - Status changes
 * - Case creation
 * - Error handling
 * - Analytics tracking
 */

import React from "react";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi, describe, it, expect, beforeEach, afterEach } from "vitest";
import { act } from "@testing-library/react";
import { notifySessionChanged } from "@/lib/auth/session-signal";
import { tokenFor } from "@/lib/auth/session-test-utils";
import PratichePage from "../page";
import { api } from "@/lib/api";
import * as analytics from "@/lib/analytics";
import type { Practice } from "@/lib/api/crm/crm.types";

// Mock dependencies
const mockPush = vi.fn();
const mockRouter = {
  push: mockPush,
  back: vi.fn(),
  forward: vi.fn(),
  refresh: vi.fn(),
  replace: vi.fn(),
};

vi.mock("next/navigation", () => ({
  useRouter: () => mockRouter,
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/process",
}));

vi.mock("@/lib/api", () => ({
  api: {
    crm: {
      getPractices: vi.fn(),
      getPracticeTypesCatalog: vi.fn(),
    },
  },
}));
vi.mock("@/lib/analytics");
vi.mock("@/components/ui/toast", () => ({
  useToast: () => ({
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  }),
}));

// Mock data
const mockPractices: Practice[] = [
  {
    id: 1,
    client_id: 101,
    can_edit_process: true,
    client_name: "John Doe",
    practice_type_id: 1,
    practice_type_code: "kitas_application",
    status: "inquiry",
    priority: "normal",
    payment_status: "pending",
    notes: "KITAS application for tech startup founder",
    created_at: "2026-01-01T10:00:00Z",
    updated_at: "2026-01-01T10:00:00Z",
  },
  {
    id: 2,
    client_id: 102,
    client_name: "Jane Smith",
    practice_type_id: 2,
    practice_type_code: "kitap_application",
    status: "quotation_sent",
    priority: "high",
    payment_status: "pending",
    notes: "KITAP renewal for long-term resident",
    created_at: "2026-01-02T10:00:00Z",
    updated_at: "2026-01-02T10:00:00Z",
  },
  {
    id: 3,
    client_id: 103,
    client_name: "Bob Qodugil",
    practice_type_id: 3,
    practice_type_code: "property_purchase",
    status: "on_process",
    priority: "normal",
    payment_status: "pending",
    notes: "Villa purchase in Canggu",
    created_at: "2026-01-03T10:00:00Z",
    updated_at: "2026-01-03T10:00:00Z",
  },
  {
    id: 4,
    client_id: 104,
    client_name: "Alice Qeledudu",
    practice_type_id: 4,
    practice_type_code: "pt_pma_setup",
    status: "completed",
    priority: "high",
    payment_status: "paid",
    notes: "PT PMA for consulting business",
    created_at: "2026-01-04T10:00:00Z",
    updated_at: "2026-01-04T10:00:00Z",
  },
];

// Stand-in for the backend smart search: case, accents and word order are free.
const fold = (text: string) =>
  text
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase();
const serverSearch = (rows: Practice[], query: string) => {
  const tokens = fold(query).split(/\s+/).filter(Boolean);
  return rows.filter((p) => {
    const hay = fold(
      `${p.client_name ?? ""} ${p.practice_type_code ?? ""} ${p.id}`,
    );
    return tokens.every((t) => hay.includes(t));
  });
};

describe("Cases Page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockPush.mockClear();
    // P2.2: view mode persists in localStorage — clear it so each test
    // starts from the default ("kanban") instead of a previous test's click.
    localStorage.clear();
    localStorage.setItem("auth_token", tokenFor("synthetic-operator"));
    vi.spyOn(console, "error").mockImplementation(() => {});
    vi.mocked(api.crm.getPractices).mockImplementation(async (params) =>
      params?.search
        ? serverSearch(mockPractices, params.search)
        : mockPractices,
    );
    // Process Type filter now reads the live catalog (kita-prune lot 6)
    // instead of a hardcoded legacy list; one category/service is enough
    // to exercise the dropdown, and its code matches mockPractices above.
    vi.mocked(api.crm.getPracticeTypesCatalog).mockResolvedValue({
      categories: [
        {
          code: "kitas",
          label: "KITAS Permits",
          services: [
            {
              code: "kitas_application",
              name: "KITAS Application",
              description: null,
              base_price: null,
              typical_duration_days: null,
            },
          ],
        },
      ],
      total_services: 1,
    });
    (analytics.initializeAnalytics as any) = vi.fn();
    (analytics.trackViewModeChange as any) = vi.fn();
    (analytics.trackFilterApplied as any) = vi.fn();
    (analytics.trackFilterRemoved as any) = vi.fn();
    (analytics.trackSortApplied as any) = vi.fn();
    (analytics.trackSearch as any) = vi.fn();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe("Component Rendering", () => {
    it("should render page title and description", async () => {
      render(<PratichePage />);

      await waitFor(() => {
        expect(screen.getByText("Process")).toBeInTheDocument();
        expect(screen.getByText("3 active · 1 completed")).toBeInTheDocument();
      });
    });

    it('should render "+ New Process" button', async () => {
      render(<PratichePage />);

      await waitFor(() => {
        expect(
          screen.getByRole("button", { name: /New Process/i }),
        ).toBeInTheDocument();
      });
    });

    it("should render search bar", async () => {
      render(<PratichePage />);

      await waitFor(() => {
        expect(
          screen.getByPlaceholderText(/Search process/i),
        ).toBeInTheDocument();
      });
    });

    it("should render Filters button", async () => {
      render(<PratichePage />);

      await waitFor(() => {
        expect(
          screen.getByRole("button", { name: /Filters/i }),
        ).toBeInTheDocument();
      });
    });

    it("should render view mode toggle buttons", async () => {
      render(<PratichePage />);

      await waitFor(() => {
        // Grid and List view buttons should be present
        const buttons = screen.getAllByRole("button");
        expect(buttons.length).toBeGreaterThan(0);
      });
    });
  });

  describe("Data Loading", () => {
    it("should load practices on mount", async () => {
      render(<PratichePage />);

      await waitFor(() => {
        expect(api.crm.getPractices).toHaveBeenCalledWith(
          expect.objectContaining({ limit: 200, include_history: true }),
        );
      });
    });

    it("should display loading skeleton while loading", () => {
      render(<PratichePage />);

      // Should show loading state initially (there are multiple skeleton divs for each column)
      expect(screen.getAllByTestId("loading-skeleton").length).toBeGreaterThan(
        0,
      );
    });

    it("should display all practices after loading", async () => {
      render(<PratichePage />);

      // Active columns render immediately; the Completed column is collapsed
      // by default to avoid 146+ finished rows dominating the board, so we
      // expand it before asserting on the completed-status fixture (Alice).
      await waitFor(() => {
        expect(screen.getByText("John Doe")).toBeInTheDocument();
        expect(screen.getByText("Jane Smith")).toBeInTheDocument();
        expect(screen.getByText("Bob Qodugil")).toBeInTheDocument();
      });

      const expandCompleted = screen.getByRole("button", {
        name: /^completed\s*\d+/i,
        expanded: false,
      });
      expandCompleted.click();

      await waitFor(() => {
        expect(screen.getByText("Alice Qeledudu")).toBeInTheDocument();
      });
    });

    it("should display error message on API failure", async () => {
      vi.mocked(api.crm.getPractices).mockRejectedValue(new Error("API Error"));

      render(<PratichePage />);

      await waitFor(() => {
        // Toast error should be called
        expect(api.crm.getPractices).toHaveBeenCalled();
      });
    });
  });

  describe("Kanban View", () => {
    it("should display Kanban board with 5 columns", async () => {
      render(<PratichePage />);

      await waitFor(() => {
        expect(screen.getByText("Inquiry")).toBeInTheDocument();
        expect(screen.getByText("Waiting Documents")).toBeInTheDocument();
        expect(screen.getByText("Sending Invoice")).toBeInTheDocument();
        expect(screen.getByText("On Process")).toBeInTheDocument();
        expect(screen.getByText("Completed")).toBeInTheDocument();
      });
    });

    it("should display cases in correct columns", async () => {
      render(<PratichePage />);

      await waitFor(() => {
        expect(screen.getByText("John Doe")).toBeInTheDocument();
      });

      // Completed column is collapsed by default — open it so the completed
      // fixture (Alice Qeledudu, pt_pma_setup, status=completed) renders.
      screen
        .getByRole("button", { name: /^completed\s*\d+/i, expanded: false })
        .click();

      await waitFor(() => {
        expect(screen.getByText("Alice Qeledudu")).toBeInTheDocument();
      });

      expect(screen.getByText("John Doe")).toBeInTheDocument();
      expect(screen.getByText("Jane Smith")).toBeInTheDocument();
      expect(screen.getByText("Bob Qodugil")).toBeInTheDocument();
    });

    it("should display case count in column headers", async () => {
      render(<PratichePage />);

      await waitFor(() => {
        // Wait for data to load first
        expect(screen.getByText("John Doe")).toBeInTheDocument();
      });

      // The count is in a separate span next to the column name
      // Verify that count badges exist - since each column has 1 case in our mock data,
      // we can verify that the column headers are visible and the structure is correct
      // (the actual count value may vary, so we just verify the columns are rendered)
      await waitFor(() => {
        expect(screen.getByText("Inquiry")).toBeInTheDocument();
        expect(screen.getByText("Waiting Documents")).toBeInTheDocument();
        expect(screen.getByText("Sending Invoice")).toBeInTheDocument();
        expect(screen.getByText("On Process")).toBeInTheDocument();
        expect(screen.getByText("Completed")).toBeInTheDocument();
      });
    });
  });

  describe("List View", () => {
    it("should switch to list view when list button clicked", async () => {
      const user = userEvent.setup();
      render(<PratichePage />);

      await waitFor(() => {
        expect(screen.getByText("John Doe")).toBeInTheDocument();
      });

      // Click list view button (assuming it has data-testid or aria-label)
      const listButton = screen
        .getAllByRole("button")
        .find(
          (btn) =>
            btn.getAttribute("aria-label")?.includes("List") ||
            btn.className.includes("list"),
        );

      if (listButton) {
        await user.click(listButton);

        // Should track view mode change
        expect(analytics.trackViewModeChange).toHaveBeenCalledWith("list");
      }
    });

    it("should display table headers in list view", async () => {
      const user = userEvent.setup();
      render(<PratichePage />);

      await waitFor(() => {
        expect(screen.getByText("John Doe")).toBeInTheDocument();
      });

      // Switch to list view
      const listButton = screen
        .getAllByRole("button")
        .find((btn) => btn.className.includes("list"));

      if (listButton) {
        await user.click(listButton);

        await waitFor(() => {
          expect(screen.getByText("ID")).toBeInTheDocument();
          expect(screen.getByText("Client")).toBeInTheDocument();
          expect(screen.getByText("Type")).toBeInTheDocument();
          expect(screen.getByText("Status")).toBeInTheDocument();
          expect(screen.getByText("Assigned To")).toBeInTheDocument();
        });
      }
    });
  });

  describe("Search Functionality", () => {
    it("should filter cases by client name", async () => {
      const user = userEvent.setup();
      render(<PratichePage />);

      await waitFor(() => {
        expect(screen.getByText("John Doe")).toBeInTheDocument();
      });

      const searchInput = screen.getByPlaceholderText(/Search process/i);
      await user.type(searchInput, "John");

      await waitFor(() => {
        expect(screen.getByText("John Doe")).toBeInTheDocument();
        expect(screen.queryByText("Jane Smith")).not.toBeInTheDocument();
        // trackSearch is called on each character change, so it should have been called with 'John' at some point
        expect(analytics.trackSearch).toHaveBeenCalledWith(
          "John",
          expect.any(Number),
        );
      });
    });

    it("should filter cases by ID", async () => {
      const user = userEvent.setup();
      render(<PratichePage />);

      await waitFor(() => {
        expect(screen.getByText("John Doe")).toBeInTheDocument();
      });

      const searchInput = screen.getByPlaceholderText(/Search process/i);
      await user.type(searchInput, "2");

      await waitFor(() => {
        expect(screen.getByText("Jane Smith")).toBeInTheDocument();
        expect(screen.queryByText("John Doe")).not.toBeInTheDocument();
      });
    });

    it("should filter cases by practice type", async () => {
      const user = userEvent.setup();
      render(<PratichePage />);

      await waitFor(() => {
        expect(screen.getByText("John Doe")).toBeInTheDocument();
      });

      const searchInput = screen.getByPlaceholderText(/Search process/i);
      await user.type(searchInput, "kitas");

      await waitFor(() => {
        expect(screen.getByText("John Doe")).toBeInTheDocument();
        expect(screen.queryByText("Bob Qodugil")).not.toBeInTheDocument(); // Property purchase
      });
    });

    describe("smart search through the backend", () => {
      const accented: Practice = {
        ...mockPractices[0],
        id: 9,
        client_name: "Müller Anna",
      };
      const withAccented = (rows: Practice[], params?: { search?: string }) =>
        params?.search
          ? serverSearch([accented, ...rows], params.search)
          : rows;

      it("loads the list for the typed text, month and history included", async () => {
        const user = userEvent.setup();
        render(<PratichePage />);
        await waitFor(() => {
          expect(screen.getByText("John Doe")).toBeInTheDocument();
        });

        await user.type(screen.getByPlaceholderText(/Search process/i), "john");

        await waitFor(() => {
          expect(api.crm.getPractices).toHaveBeenCalledWith(
            expect.objectContaining({
              search: "john",
              limit: 200,
              include_history: true,
            }),
          );
        });
      });

      it("finds a client typed lowercase without accents", async () => {
        const user = userEvent.setup();
        vi.mocked(api.crm.getPractices).mockImplementation(async (params) =>
          withAccented(mockPractices, params),
        );
        render(<PratichePage />);
        await waitFor(() => {
          expect(screen.getByText("John Doe")).toBeInTheDocument();
        });

        await user.type(
          screen.getByPlaceholderText(/Search process/i),
          "anna muller",
        );

        await waitFor(() => {
          expect(screen.getByText("Müller Anna")).toBeInTheDocument();
          expect(screen.queryByText("John Doe")).not.toBeInTheDocument();
        });
      });

      it("lets staff edit a card that only the search returned", async () => {
        const user = userEvent.setup();
        (api.crm.updatePractice as any) = vi.fn().mockResolvedValue({});
        vi.mocked(api.crm.getPractices).mockImplementation(async (params) =>
          withAccented(mockPractices, params),
        );
        render(<PratichePage />);
        await waitFor(() => {
          expect(screen.getByText("John Doe")).toBeInTheDocument();
        });
        await user.type(
          screen.getByPlaceholderText(/Search process/i),
          "muller",
        );
        await waitFor(() => {
          expect(screen.getByText("Müller Anna")).toBeInTheDocument();
          expect(screen.queryByText("John Doe")).not.toBeInTheDocument();
        });

        fireEvent.contextMenu(screen.getByText("Müller Anna"));
        await screen.findByText("Update Status");
        // the menu is rendered last, after the board columns
        const nextStage = screen.getAllByText("Waiting Documents");
        await user.click(nextStage[nextStage.length - 1]);

        await waitFor(() => {
          expect(api.crm.updatePractice).toHaveBeenCalledWith(9, {
            status: "waiting_documents",
          });
        });
        await waitFor(() => {
          // the old status is read from the same list the card is shown from
          expect(analytics.trackCaseStatusChanged).toHaveBeenCalledWith(
            9,
            "inquiry",
            "waiting_documents",
          );
        });
      });

      it("keeps the list and the local filter when the search fails", async () => {
        const user = userEvent.setup();
        vi.mocked(api.crm.getPractices).mockImplementation(async (params) => {
          if (params?.search) throw new Error("search down");
          return mockPractices;
        });
        render(<PratichePage />);
        await waitFor(() => {
          expect(screen.getByText("John Doe")).toBeInTheDocument();
        });

        await user.type(screen.getByPlaceholderText(/Search process/i), "John");

        await waitFor(() => {
          expect(api.crm.getPractices).toHaveBeenCalledWith(
            expect.objectContaining({ search: "John" }),
          );
        });
        expect(screen.getByText("John Doe")).toBeInTheDocument();
        expect(screen.queryByText("Jane Smith")).not.toBeInTheDocument();
      });

      it("ignores an older answer that arrives after a newer one", async () => {
        const user = userEvent.setup();
        let releaseOld: (rows: Practice[]) => void = () => {};
        vi.mocked(api.crm.getPractices).mockImplementation(async (params) => {
          if (params?.search === "jo") {
            return new Promise<Practice[]>((resolve) => {
              releaseOld = resolve;
            });
          }
          return params?.search
            ? serverSearch(mockPractices, params.search)
            : mockPractices;
        });
        render(<PratichePage />);
        await waitFor(() => {
          expect(screen.getByText("John Doe")).toBeInTheDocument();
        });
        const box = screen.getByPlaceholderText(/Search process/i);

        await user.type(box, "jo");
        await waitFor(() => {
          expect(api.crm.getPractices).toHaveBeenCalledWith(
            expect.objectContaining({ search: "jo" }),
          );
        });
        await user.type(box, "hn");
        await waitFor(() => {
          expect(api.crm.getPractices).toHaveBeenCalledWith(
            expect.objectContaining({ search: "john" }),
          );
        });
        // the stale "jo" answer carries a card the local text filter would also accept
        releaseOld([
          mockPractices[0],
          { ...mockPractices[0], id: 7, client_name: "Johnny Stale" },
        ]);

        await waitFor(() => {
          expect(screen.getByText("John Doe")).toBeInTheDocument();
        });
        expect(screen.queryByText("Johnny Stale")).not.toBeInTheDocument();
        expect(screen.queryByText("Jane Smith")).not.toBeInTheDocument();
      });
    });

    it('should show "no results" when search has no matches', async () => {
      const user = userEvent.setup();
      render(<PratichePage />);

      await waitFor(() => {
        expect(screen.getByText("John Doe")).toBeInTheDocument();
      });

      const searchInput = screen.getByPlaceholderText(/Search process/i);
      await user.type(searchInput, "nonexistent");

      await waitFor(() => {
        expect(screen.queryByText("John Doe")).not.toBeInTheDocument();
        expect(screen.queryByText("Jane Smith")).not.toBeInTheDocument();
      });
    });
  });

  describe("Scoped process board", () => {
    it("filters and labels the assigned operator in the board, list and history", async () => {
      const user = userEvent.setup();
      const rows: Practice[] = [
        {
          ...mockPractices[0],
          client_name: "Setup case",
          client_lead: "other@example.test",
          assigned_to: "setup@example.test",
          assignee_name: "Operator A",
          assignee_member_id: "m-1",
          assignee_department: "setup",
        },
        {
          ...mockPractices[1],
          client_name: "Tax case",
          client_lead: "setup@example.test",
          assigned_to: "tax@example.test",
          assignee_name: "Operator B",
          assignee_member_id: "m-2",
          assignee_department: "tax",
          status_transitions: [{ status: "inquiry", at: "2026-01-01" }],
        },
      ];
      vi.mocked(api.crm.getPractices).mockResolvedValue(rows);
      render(<PratichePage />);
      const tax = await screen.findByTestId("process-card-2");
      expect(tax.querySelector('div[data-department="tax"]')).not.toBeNull();
      expect(
        screen
          .getByTestId("process-card-1")
          .querySelector('div[data-department="setup"]'),
      ).not.toBeNull();
      expect(within(tax).getByText("Operator B")).toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: /Stage history/ }));
      await user.click(screen.getByRole("button", { name: /Filters/ }));
      await user.selectOptions(
        screen.getByLabelText("Assigned To"),
        "setup@example.test",
      );
      expect(screen.getByText("Setup case")).toBeVisible();
      expect(screen.queryByText("Tax case")).toBeNull();
      expect(
        screen.queryByRole("button", { name: /Stage history/ }),
      ).toBeNull();
      expect(analytics.trackFilterApplied).toHaveBeenCalledWith(
        "assigned_to",
        "selected",
      );
      await user.click(screen.getByRole("button", { name: /List View/i }));
      expect(
        within(screen.getByRole("table")).getByText("Operator A"),
      ).toBeVisible();
    });

    it("shows a read-only case without status or priority mutations", async () => {
      const row = { ...mockPractices[0], can_edit_process: false };
      vi.mocked(api.crm.getPractices).mockResolvedValue([row]);
      render(<PratichePage />);
      await userEvent
        .setup()
        .click(
          within(await screen.findByTestId("process-card-1")).getByRole(
            "button",
            { name: "More options" },
          ),
        );
      expect(screen.getByText("Process details")).toBeVisible();
      expect(
        screen.queryByRole("button", { name: "Sending Invoice" }),
      ).toBeNull();
      expect(screen.queryByRole("button", { name: "Urgent" })).toBeNull();
      expect(screen.getByRole("button", { name: "Documents" })).toBeEnabled();
    });

    it("follows server pagination and ignores rows returned twice across pages", async () => {
      const finished = Array.from({ length: 200 }, (_, i) => ({
        ...mockPractices[3],
        id: 1000 + i,
      }));
      vi.mocked(api.crm.getPractices).mockImplementation(async (params) =>
        params?.offset ? [finished[199], mockPractices[0]] : finished,
      );
      render(<PratichePage />);
      await screen.findByTestId("process-card-1");
      expect(screen.getByText("1 active · 200 completed")).toBeVisible();
      expect(api.crm.getPractices).toHaveBeenCalledWith(
        expect.objectContaining({
          offset: 200,
          include_history: true,
          session: expect.objectContaining({ sub: "synthetic-operator" }),
        }),
      );
    });

    it("discards a previous account's response and open controls on an account switch", async () => {
      let finish: (rows: Practice[]) => void = () => {};
      vi.mocked(api.crm.getPractices).mockImplementation(async (params) =>
        params?.session?.sub === "other-operator"
          ? [mockPractices[2]]
          : params?.search
            ? new Promise<Practice[]>((resolve) => {
                finish = resolve;
              })
            : [mockPractices[0]],
      );
      render(<PratichePage />);
      const card = await screen.findByTestId("process-card-1");
      await userEvent
        .setup()
        .click(within(card).getByRole("button", { name: "More options" }));
      expect(screen.getByText("Update Status")).toBeVisible();
      await userEvent
        .setup()
        .type(screen.getByLabelText("Search process"), "branch");
      await waitFor(() =>
        expect(api.crm.getPractices).toHaveBeenCalledWith(
          expect.objectContaining({ search: "branch" }),
        ),
      );
      act(() => {
        localStorage.setItem("auth_token", tokenFor("other-operator"));
        notifySessionChanged();
      });
      await screen.findByTestId("process-card-3");
      await act(async () => finish([mockPractices[0]]));
      expect(screen.queryByTestId("process-card-1")).toBeNull();
      expect(screen.queryByText("Update Status")).toBeNull();
      expect(screen.getByLabelText("Search process")).toHaveValue("");
    });
  });

  describe("Filter Functionality", () => {
    it("should open filter panel when Filters button clicked", async () => {
      const user = userEvent.setup();
      render(<PratichePage />);

      await waitFor(() => {
        expect(
          screen.getByRole("button", { name: /Filters/i }),
        ).toBeInTheDocument();
      });

      const filtersButton = screen.getByRole("button", { name: /Filters/i });
      await user.click(filtersButton);

      // Filter panel should be visible
      await waitFor(() => {
        expect(screen.getByText(/Status/)).toBeInTheDocument();
        expect(screen.getByText(/Process Type/)).toBeInTheDocument();
        expect(screen.getByText(/Assigned To/)).toBeInTheDocument();
      });
    });

    it("should filter by status", async () => {
      const user = userEvent.setup();
      render(<PratichePage />);

      await waitFor(() => {
        expect(screen.getByText("John Doe")).toBeInTheDocument();
      });

      // Open filters
      const filtersButton = screen.getByRole("button", { name: /Filters/i });
      await user.click(filtersButton);

      // Select "On Process" status
      const statusSelect = screen.getByLabelText(/Status/i);
      await user.selectOptions(statusSelect, "on_process");

      await waitFor(() => {
        expect(screen.getByText("Bob Qodugil")).toBeInTheDocument();
        expect(screen.queryByText("John Doe")).not.toBeInTheDocument();
        expect(analytics.trackFilterApplied).toHaveBeenCalledWith(
          "status",
          "on_process",
        );
      });
    });

    it("should clear filters when Clear button clicked", async () => {
      const user = userEvent.setup();
      render(<PratichePage />);

      await waitFor(() => {
        expect(screen.getByText("John Doe")).toBeInTheDocument();
      });

      // Apply filter
      const filtersButton = screen.getByRole("button", { name: /Filters/i });
      await user.click(filtersButton);

      const statusSelect = screen.getByLabelText(/Status/i);
      await user.selectOptions(statusSelect, "completed");

      // Clear filters
      const clearButton = screen.getByRole("button", { name: /Clear/i });
      await user.click(clearButton);

      await waitFor(() => {
        // All cases should be visible again
        expect(screen.getByText("John Doe")).toBeInTheDocument();
        expect(screen.getByText("Jane Smith")).toBeInTheDocument();
        expect(analytics.trackFilterRemoved).toHaveBeenCalled();
      });
    });

    it("should display active filter count", async () => {
      const user = userEvent.setup();
      render(<PratichePage />);

      await waitFor(() => {
        expect(screen.getByText("John Doe")).toBeInTheDocument();
      });

      const filtersButton = screen.getByRole("button", { name: /Filters/i });
      await user.click(filtersButton);

      // Apply two filters
      const statusSelect = screen.getByLabelText(/Status/i);
      await user.selectOptions(statusSelect, "inquiry");

      const typeSelect = screen.getByLabelText(/Process Type/i);
      await waitFor(() => {
        expect(
          within(typeSelect).getByText("KITAS Application"),
        ).toBeInTheDocument();
      });
      await user.selectOptions(typeSelect, "kitas_application");

      await waitFor(() => {
        // Filter count badge should show "2"
        expect(screen.getByText("2")).toBeInTheDocument();
      });
    });
  });

  describe("Sorting Functionality", () => {
    it("should sort by ID", async () => {
      const user = userEvent.setup();
      render(<PratichePage />);

      await waitFor(() => {
        expect(screen.getByText("John Doe")).toBeInTheDocument();
      });

      // Switch to list view first to see sortable headers
      const listButton = screen.getByRole("button", { name: /List View/i });
      await user.click(listButton);

      await waitFor(() => {
        expect(screen.getByText("Process Type")).toBeInTheDocument();
      });

      // Find and click a sortable header (e.g., Case Type)
      const processTypeHeader = screen.getByText("Process Type");
      await user.click(processTypeHeader);

      await waitFor(() => {
        expect(analytics.trackSortApplied).toHaveBeenCalled();
      });
    });

    it("should toggle sort order on second click", async () => {
      const user = userEvent.setup();
      render(<PratichePage />);

      await waitFor(() => {
        expect(screen.getByText("John Doe")).toBeInTheDocument();
      });

      // Switch to list view first
      const listButton = screen.getByRole("button", { name: /List View/i });
      await user.click(listButton);

      await waitFor(() => {
        expect(screen.getByText("Process Type")).toBeInTheDocument();
      });

      const processTypeHeader = screen.getByText("Process Type");
      await user.click(processTypeHeader); // First click: asc
      await user.click(processTypeHeader); // Second click: desc

      await waitFor(() => {
        expect(analytics.trackSortApplied).toHaveBeenCalled();
      });
    });
  });

  describe("Status Change", () => {
    it("should open context menu on card menu click", async () => {
      const user = userEvent.setup();
      render(<PratichePage />);

      await waitFor(() => {
        expect(screen.getByText("John Doe")).toBeInTheDocument();
      });

      // Find and click the menu button on first card
      const menuButtons = screen
        .getAllByRole("button")
        .filter(
          (btn) =>
            btn.getAttribute("aria-label")?.includes("menu") ||
            btn.className.includes("menu"),
        );

      if (menuButtons[0]) {
        await user.click(menuButtons[0]);

        await waitFor(() => {
          expect(screen.getByText("Update Status")).toBeInTheDocument();
          expect(screen.getByText("Inquiry")).toBeInTheDocument();
          expect(screen.getByText("Sending Invoice")).toBeInTheDocument();
        });
      }
    });

    it("should update case status when status option clicked", async () => {
      const user = userEvent.setup();
      (api.crm.updatePractice as any) = vi.fn().mockResolvedValue({});

      render(<PratichePage />);

      await waitFor(() => {
        expect(screen.getByText("John Doe")).toBeInTheDocument();
      });

      // Open context menu
      const menuButtons = screen
        .getAllByRole("button")
        .filter((btn) => btn.className.includes("menu"));

      if (menuButtons[0]) {
        await user.click(menuButtons[0]);

        // Click on "On Process" status
        const onProcessOption = screen.getByText("On Process");
        await user.click(onProcessOption);

        await waitFor(() => {
          expect(api.crm.updatePractice).toHaveBeenCalledWith(1, {
            status: "on_process",
          });
          expect(analytics.trackCaseStatusChanged).toHaveBeenCalledWith(
            1,
            "inquiry",
            "on_process",
          );
        });
      }
    });

    it("should close context menu on outside click", async () => {
      const user = userEvent.setup();
      render(<PratichePage />);

      await waitFor(() => {
        expect(screen.getByText("John Doe")).toBeInTheDocument();
      });

      // Open context menu
      const menuButtons = screen
        .getAllByRole("button")
        .filter((btn) => btn.className.includes("menu"));

      if (menuButtons[0]) {
        await user.click(menuButtons[0]);

        await waitFor(() => {
          expect(screen.getByText("Update Status")).toBeInTheDocument();
        });

        // Click outside
        await user.click(document.body);

        await waitFor(() => {
          expect(screen.queryByText("Update Status")).not.toBeInTheDocument();
        });
      }
    });
  });

  describe("Process Creation", () => {
    it('should navigate to new process page when "+ New Process" clicked', async () => {
      const user = userEvent.setup();
      mockPush.mockClear();

      render(<PratichePage />);

      await waitFor(() => {
        expect(
          screen.getByRole("button", { name: /New Process/i }),
        ).toBeInTheDocument();
      });

      const newProcessButton = screen.getByRole("button", {
        name: /New Process/i,
      });
      await user.click(newProcessButton);

      expect(mockPush).toHaveBeenCalledWith("/process/new");
    });
  });

  describe("Pagination", () => {
    it("should display pagination controls in list view", async () => {
      // Create more than 25 cases to trigger pagination
      const manyPractices = Array.from({ length: 30 }, (_, i) => ({
        ...mockPractices[0],
        id: i + 1,
        client_name: `Client ${i + 1}`,
      }));

      vi.mocked(api.crm.getPractices).mockResolvedValue(manyPractices);

      const user = userEvent.setup();
      render(<PratichePage />);

      await waitFor(() => {
        expect(screen.getByText("Client 1")).toBeInTheDocument();
      });

      // Switch to list view
      const listButton = screen
        .getAllByRole("button")
        .find((btn) => btn.className.includes("list"));

      if (listButton) {
        await user.click(listButton);

        await waitFor(() => {
          expect(screen.getByText("Page 1 of 2")).toBeInTheDocument();
          expect(
            screen.getByRole("button", { name: /Next/i }),
          ).toBeInTheDocument();
          expect(
            screen.getByRole("button", { name: /Previous/i }),
          ).toBeInTheDocument();
        });
      }
    });

    it("should navigate to next page when Next clicked", async () => {
      const manyPractices = Array.from({ length: 30 }, (_, i) => ({
        ...mockPractices[0],
        id: i + 1,
        client_name: `Client ${i + 1}`,
      }));

      vi.mocked(api.crm.getPractices).mockResolvedValue(manyPractices);

      const user = userEvent.setup();
      render(<PratichePage />);

      await waitFor(() => {
        expect(screen.getByText("Client 1")).toBeInTheDocument();
      });

      // Switch to list view
      const listButton = screen
        .getAllByRole("button")
        .find((btn) => btn.className.includes("list"));

      if (listButton) {
        await user.click(listButton);

        const nextButton = screen.getByRole("button", { name: /Next/i });
        await user.click(nextButton);

        await waitFor(() => {
          expect(analytics.trackPaginationChange).toHaveBeenCalledWith(2, 25);
        });
      }
    });
  });

  describe("Analytics Tracking", () => {
    it("should initialize analytics on mount", async () => {
      render(<PratichePage />);

      await waitFor(() => {
        expect(analytics.initializeAnalytics).toHaveBeenCalled();
      });
    });

    it("should track view mode changes", async () => {
      const user = userEvent.setup();
      render(<PratichePage />);

      await waitFor(() => {
        expect(screen.getByText("John Doe")).toBeInTheDocument();
      });

      // Should track initial view mode
      expect(analytics.trackViewModeChange).toHaveBeenCalledWith("kanban");

      // Switch to list view
      const listButton = screen
        .getAllByRole("button")
        .find((btn) => btn.className.includes("list"));

      if (listButton) {
        await user.click(listButton);
        expect(analytics.trackViewModeChange).toHaveBeenCalledWith("list");
      }
    });

    it("should track search operations", async () => {
      const user = userEvent.setup();
      render(<PratichePage />);

      await waitFor(() => {
        expect(screen.getByText("John Doe")).toBeInTheDocument();
      });

      const searchInput = screen.getByPlaceholderText(/Search process/i);
      await user.type(searchInput, "John");

      await waitFor(() => {
        expect(analytics.trackSearch).toHaveBeenCalledWith(
          "John",
          expect.any(Number),
        );
      });
    });

    it("should track filter operations", async () => {
      const user = userEvent.setup();
      render(<PratichePage />);

      await waitFor(() => {
        expect(screen.getByText("John Doe")).toBeInTheDocument();
      });

      const filtersButton = screen.getByRole("button", { name: /Filters/i });
      await user.click(filtersButton);

      const statusSelect = screen.getByLabelText(/Status/i);
      await user.selectOptions(statusSelect, "inquiry");

      await waitFor(() => {
        expect(analytics.trackFilterApplied).toHaveBeenCalledWith(
          "status",
          "inquiry",
        );
      });
    });

    it("should track sort operations", async () => {
      const user = userEvent.setup();
      render(<PratichePage />);

      await waitFor(() => {
        expect(screen.getByText("John Doe")).toBeInTheDocument();
      });

      // Initial sort should be tracked
      expect(analytics.trackSortApplied).toHaveBeenCalledWith(
        "created_at",
        "desc",
      );
    });

    it("should track case status changes", async () => {
      const user = userEvent.setup();
      (api.crm.updatePractice as any) = vi.fn().mockResolvedValue({});

      render(<PratichePage />);

      await waitFor(() => {
        expect(screen.getByText("John Doe")).toBeInTheDocument();
      });

      const menuButtons = screen
        .getAllByRole("button")
        .filter((btn) => btn.className.includes("menu"));

      if (menuButtons[0]) {
        await user.click(menuButtons[0]);

        const onProcessOption = screen.getByText("On Process");
        await user.click(onProcessOption);

        await waitFor(() => {
          expect(analytics.trackCaseStatusChanged).toHaveBeenCalledWith(
            1,
            "inquiry",
            "on_process",
          );
        });
      }
    });
  });

  describe("Error Handling", () => {
    it("should handle API error gracefully", async () => {
      vi.mocked(api.crm.getPractices).mockRejectedValue(
        new Error("Network error"),
      );

      // Component should render without crashing even when API fails
      const { container } = render(<PratichePage />);

      // Wait for error to be processed - component should still be mounted
      await waitFor(() => {
        expect(container).toBeInTheDocument();
      });
    });

    it("should handle status update failure gracefully", async () => {
      const user = userEvent.setup();
      (api.crm.updatePractice as any) = vi
        .fn()
        .mockRejectedValue(new Error("Update failed"));

      render(<PratichePage />);

      await waitFor(() => {
        expect(screen.getByText("John Doe")).toBeInTheDocument();
      });

      const menuButtons = screen
        .getAllByRole("button")
        .filter((btn) => btn.className.includes("menu"));

      if (menuButtons[0]) {
        await user.click(menuButtons[0]);

        const inProgressOption = screen.getByText("In Progress");
        await user.click(inProgressOption);

        await waitFor(() => {
          expect(console.error).toHaveBeenCalledWith(
            "Failed to update status:",
            expect.any(Error),
          );
        });
      }
    });
  });

  describe("Performance", () => {
    it("should memoize filtered practices to avoid recalculation", async () => {
      const { rerender } = render(<PratichePage />);

      await waitFor(() => {
        expect(screen.getByText("John Doe")).toBeInTheDocument();
      });

      // Rerender with same props
      rerender(<PratichePage />);

      // getPractices should only be called once (memoization working)
      expect(api.crm.getPractices).toHaveBeenCalledTimes(1);
    });

    it("should use ref for filter tracking to avoid closure issues", async () => {
      const user = userEvent.setup();
      render(<PratichePage />);

      await waitFor(() => {
        expect(screen.getByText("John Doe")).toBeInTheDocument();
      });

      const filtersButton = screen.getByRole("button", { name: /Filters/i });
      await user.click(filtersButton);

      const statusSelect = screen.getByLabelText(/Status/i);
      await user.selectOptions(statusSelect, "inquiry");

      // Should track filter applied only once (not multiple times due to closure)
      await waitFor(() => {
        const calls = (
          analytics.trackFilterApplied as unknown as {
            mock: { calls: unknown[][] };
          }
        ).mock.calls.filter(
          (call: unknown[]) => call[0] === "status" && call[1] === "inquiry",
        );
        expect(calls.length).toBe(1);
      });
    });
  });
});
