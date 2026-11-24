/**
 * The LKPM submit form files data for the company the operator chose. The server takes
 * a client and, optionally, a company; with no company it picks one of the client's own
 * companies, so a client linked to several could get the figures on another company's
 * report while the page says they were filed for the chosen one. Invented values only.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import WorkspaceLKPMSubmitPage from "./page";

const toastSuccess = vi.hoisted(() => vi.fn());
const toastError = vi.hoisted(() => vi.fn());

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

vi.mock("@/lib/api", () => ({
  api: { get: vi.fn(), post: vi.fn() },
}));

vi.mock("@/components/ui/toast", () => ({
  useToast: () => ({ success: toastSuccess, error: toastError }),
}));

vi.mock("@/lib/logger", () => ({
  logger: { error: vi.fn(), warn: vi.fn(), info: vi.fn() },
}));

const getMock = vi.mocked(api.get);
const postMock = vi.mocked(api.post);

const CHOSEN = { id: 900002, company_name: "PT Contoh Dua" };

async function chooseCompanyAndSubmit(container: HTMLElement) {
  const user = userEvent.setup();
  await user.type(
    screen.getByPlaceholderText(/search company by name/i),
    "Contoh",
  );
  await user.click(await screen.findByText(CHOSEN.company_name));
  const form = container.querySelector("form");
  expect(form).not.toBeNull();
  fireEvent.submit(form as Element);
  await waitFor(() => expect(postMock).toHaveBeenCalledTimes(1));
  return postMock.mock.calls[0];
}

beforeEach(() => {
  vi.clearAllMocks();
  getMock.mockImplementation(async (url: string) => {
    if (url.startsWith("/api/crm/companies?")) return [CHOSEN];
    if (url === `/api/crm/companies/${CHOSEN.id}`) {
      return {
        id: CHOSEN.id,
        associates: [{ client_id: 900010, is_primary: true, role: "director" }],
      };
    }
    throw new Error(`unexpected GET ${url}`);
  });
  postMock.mockResolvedValue({
    success: true,
    draft_id: 900100,
    quarter: "Q1",
    year: 2026,
    realized_total: 0,
  });
});

describe("LKPM submit form", () => {
  it("sends the company the operator chose", async () => {
    const { container } = render(<WorkspaceLKPMSubmitPage />);

    const [url, body] = await chooseCompanyAndSubmit(container);

    expect(url).toBe("/api/v1/lkpm/submit-data");
    expect(body).toMatchObject({ company_id: CHOSEN.id });
  });

  it("still sends the company's linked client and the period", async () => {
    const { container } = render(<WorkspaceLKPMSubmitPage />);

    const [, body] = await chooseCompanyAndSubmit(container);

    expect(body).toMatchObject({ client_id: 900010, quarter: "Q1" });
    expect(toastSuccess).toHaveBeenCalledTimes(1);
  });
});
