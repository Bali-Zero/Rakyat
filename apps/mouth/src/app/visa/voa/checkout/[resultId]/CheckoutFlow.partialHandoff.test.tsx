import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CheckoutFlow } from "./CheckoutFlow";
import { writeCheckoutHandoff } from "../../checkoutHandoff";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

vi.mock("@balizero/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@balizero/core")>();
  return {
    ...actual,
    useFunnelApp: () => ({
      viewed: vi.fn(),
      formSubmitted: vi.fn(),
      formSubmitFailed: vi.fn(),
      ctaClicked: vi.fn(),
      whatsappHandoff: vi.fn(),
    }),
  };
});

const NAME_LABEL = /full name \(as in passport\)/i;
const PASSPORT_LABEL = /^passport number$/i;

describe("CheckoutFlow — a partial handoff still prefills what it carries", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
  });

  it("a handoff with only the passport number prefills that field and leaves the name empty", async () => {
    writeCheckoutHandoff("result-1", { passport_number: "X0000001" });
    render(<CheckoutFlow resultId="result-1" paymentsLive={true} />);

    expect(await screen.findByLabelText(PASSPORT_LABEL)).toHaveValue(
      "X0000001",
    );
    expect(screen.getByLabelText(NAME_LABEL)).toHaveValue("");
  });

  it("a handoff with only the name prefills that field and leaves the passport number empty", async () => {
    writeCheckoutHandoff("result-1", { full_name: "Person One" });
    render(<CheckoutFlow resultId="result-1" paymentsLive={true} />);

    expect(await screen.findByLabelText(NAME_LABEL)).toHaveValue("Person One");
    expect(screen.getByLabelText(PASSPORT_LABEL)).toHaveValue("");
  });

  it("a full handoff prefills both and keeps them read-only", async () => {
    writeCheckoutHandoff("result-1", {
      full_name: "Person One",
      passport_number: "X0000001",
    });
    render(<CheckoutFlow resultId="result-1" paymentsLive={true} />);

    expect(await screen.findByText("Person One")).toBeInTheDocument();
    expect(screen.getByText("X0000001")).toBeInTheDocument();
    expect(screen.queryByLabelText(NAME_LABEL)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(PASSPORT_LABEL)).not.toBeInTheDocument();
  });

  it("no handoff leaves both fields empty", () => {
    render(<CheckoutFlow resultId="result-1" paymentsLive={true} />);

    expect(screen.getByLabelText(NAME_LABEL)).toHaveValue("");
    expect(screen.getByLabelText(PASSPORT_LABEL)).toHaveValue("");
  });
});
