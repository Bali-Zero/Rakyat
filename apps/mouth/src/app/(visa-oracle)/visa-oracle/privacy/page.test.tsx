import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import VisaOraclePrivacyPage from "./page";
import { VisaOraclePrivacyClient } from "./VisaOraclePrivacyClient";

describe("Visa Oracle Privacy Policy V1", () => {
  it("publishes the approved durations, separate consent and ENFORCE gate", () => {
    render(<VisaOraclePrivacyClient initialLanguage="en" />);

    expect(screen.getByText(/retained for 30 days/)).toBeInTheDocument();
    expect(screen.getByText(/retained for 24 hours/)).toBeInTheDocument();
    expect(screen.getByText(/retained for 90 days/)).toBeInTheDocument();
    expect(
      screen.getByRole("heading", {
        name: "WhatsApp and CRM are separate choices",
      }),
    ).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent(
      "cannot enter ENFORCE mode",
    );
    expect(
      screen.getByRole("link", {
        name: "Send a request to privacy@balizero.com",
      }),
    ).toHaveAttribute("href", "mailto:privacy@balizero.com");
  });

  it("server entry renders English when ?lang is omitted", async () => {
    const ui = await VisaOraclePrivacyPage({
      searchParams: Promise.resolve({}),
    });
    render(ui);
    expect(
      screen.getByRole("heading", { name: "Your Visa Oracle data" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Back to Visa Oracle" }),
    ).toHaveAttribute("href", "/visa-oracle");
  });

  it("server entry renders Indonesian when ?lang=id", async () => {
    const ui = await VisaOraclePrivacyPage({
      searchParams: Promise.resolve({ lang: "id" }),
    });
    render(ui);
    expect(
      screen.getByRole("heading", { name: "Data Visa Oracle Anda" }),
    ).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("id");
    expect(
      screen.getByRole("link", { name: "Kembali ke Visa Oracle" }),
    ).toHaveAttribute("href", "/visa-oracle?lang=id");
  });

  it("switches the complete notice to Bahasa Indonesia", async () => {
    const user = userEvent.setup();
    render(<VisaOraclePrivacyClient initialLanguage="en" />);

    await user.click(screen.getByRole("button", { name: /bahasa indonesia/i }));

    expect(
      screen.getByRole("heading", { name: "Data Visa Oracle Anda" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", {
        name: "WhatsApp dan CRM adalah pilihan terpisah",
      }),
    ).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("id");
    expect(
      screen.getByRole("link", { name: "Kembali ke Visa Oracle" }),
    ).toHaveAttribute("href", "/visa-oracle?lang=id");
  });

  it("uses natural Bahasa Indonesia for the minors transfer wording", async () => {
    const user = userEvent.setup();
    render(<VisaOraclePrivacyClient initialLanguage="en" />);

    await user.click(screen.getByRole("button", { name: /bahasa indonesia/i }));

    expect(
      screen.getByText(
        /tidak mengotomatiskan rekomendasi yang didukung maupun pengalihan/i,
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("main")).not.toHaveTextContent(/handoff/i);
  });
});
