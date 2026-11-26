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

function amountInputs(container: HTMLElement) {
  return Array.from(
    container.querySelectorAll<HTMLInputElement>('input[inputmode="numeric"]'),
  );
}

function paste(input: HTMLInputElement, value: string) {
  fireEvent.paste(input, {
    clipboardData: { getData: () => value },
  });
}

async function submitForChosenCompany(container: HTMLElement) {
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
  return postMock.mock.calls[0][1] as Record<string, number>;
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

describe("LKPM submit amount inputs", () => {
  it.each([
    "1250000.50",
    "1.250.000,50",
    "1,250,000.00",
    "1250000",
    "1.250.000",
    "1,250,000",
  ])(
    "guilt: pasting %s into investment keeps the whole amount",
    async (pasted) => {
      const { container } = render(<WorkspaceLKPMSubmitPage />);
      const investment = amountInputs(container)[0];

      paste(investment, pasted);

      expect(investment).toHaveValue("1.250.000");
      expect(await submitForChosenCompany(container)).toMatchObject({
        building_domestic: 1250000,
      });
    },
  );

  it("guilt: negative investment paste leaves its value unchanged", () => {
    const { container } = render(<WorkspaceLKPMSubmitPage />);
    const investment = amountInputs(container)[0];

    fireEvent.change(investment, { target: { value: "1250000" } });
    paste(investment, "-5000");

    expect(investment).toHaveValue("1.250.000");
  });

  it.each(["1250000.50", "1.250.000,50", "1,250,000.00", "1250000"])(
    "guilt: pasting %s into revenue keeps the whole amount and submits it",
    async (pasted) => {
      const { container } = render(<WorkspaceLKPMSubmitPage />);
      const inputs = amountInputs(container);
      const revenue = inputs[inputs.length - 2];

      paste(revenue, pasted);

      expect(revenue).toHaveValue("1.250.000");
      expect(await submitForChosenCompany(container)).toMatchObject({
        quarterly_revenue: 1250000,
      });
    },
  );

  it("guilt: negative revenue paste leaves its value unchanged", () => {
    const { container } = render(<WorkspaceLKPMSubmitPage />);
    const inputs = amountInputs(container);
    const revenue = inputs[inputs.length - 2];

    fireEvent.change(revenue, { target: { value: "1250000" } });
    paste(revenue, "-5000");

    expect(revenue).toHaveValue("1.250.000");
  });

  it("pastes a whole amount into every amount input", () => {
    const { container } = render(<WorkspaceLKPMSubmitPage />);
    const inputs = amountInputs(container);
    expect(inputs.length).toBeGreaterThan(2);
    for (const input of inputs) {
      paste(input, "1.250.000,50");
      expect(input).toHaveValue("1.250.000");
    }
  });

  it.each(["1.250.000,50\n", "\t1250000.50 ", "Rp 1.250.000,50\r\n"])(
    "keeps the whole amount of a spreadsheet cell pasted as %j",
    (pasted) => {
      const { container } = render(<WorkspaceLKPMSubmitPage />);
      const investment = amountInputs(container)[0];

      paste(investment, pasted);

      expect(investment).toHaveValue("1.250.000");
    },
  );

  it("leaves the field unchanged when the pasted text holds no amount", () => {
    const { container } = render(<WorkspaceLKPMSubmitPage />);
    const investment = amountInputs(container)[0];

    fireEvent.change(investment, { target: { value: "1250000" } });
    paste(investment, "n/a");

    expect(investment).toHaveValue("1.250.000");
  });

  it("innocence: typing digits and deleting from formatted investment preserve current parsing", async () => {
    const { container } = render(<WorkspaceLKPMSubmitPage />);
    const investment = amountInputs(container)[0];

    const user = userEvent.setup();
    await user.type(investment, "1250000");
    expect(investment).toHaveValue("1.250.000");

    fireEvent.change(investment, { target: { value: "1.250.00" } });
    expect(investment).toHaveValue("125.000");
  });
});
