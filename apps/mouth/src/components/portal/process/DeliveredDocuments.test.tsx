import { render, screen } from "@testing-library/react";
import { SWRConfig } from "swr";
import { expect, it, vi } from "vitest";
import { DeliveredDocuments } from "./DeliveredDocuments";

const get = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ api: { get } }));
it("lists documents from the authorised practice reader and links the portal download", async () => {
  get.mockResolvedValue({
    documents: [{ id: 38, file_name: "Electronic-Visa.pdf" }],
  });
  render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <DeliveredDocuments practiceId="12" />
    </SWRConfig>,
  );
  expect(
    await screen.findByRole("link", { name: "Electronic-Visa.pdf" }),
  ).toHaveAttribute("href", "/api/portal/documents/38/download");
  expect(get).toHaveBeenCalledWith("/api/portal/process/12/delivery-documents");
});
