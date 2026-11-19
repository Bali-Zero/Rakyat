import { render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import type {
  Client,
  ClientCompanyLink,
  CompanyDriveSources,
} from "@/lib/api/crm/crm.types";
import type { FileItem } from "@/lib/api/drive/drive.types";
import { TaxDocumentsSection, folderIdFromUrl } from "./TaxDocumentsSection";
import { TaxTab } from "./TaxTab";

// Synthetic fixtures only: folder ids like FOLDER_123 / COMPANY_9, no real
// names, phones or passports.

vi.mock("@/lib/api", () => ({
  api: {
    getProfile: vi.fn().mockResolvedValue({ email: "tester@example.test" }),
    drive: { listFiles: vi.fn() },
    crm: { getCompanyDriveSources: vi.fn(), updateClient: vi.fn() },
  },
}));
vi.mock("@/lib/api/workspace/lkpm.api", () => ({
  lkpmApi: {
    getClientHistory: vi.fn().mockResolvedValue({ items: [] }),
    getClientReceipts: vi.fn().mockResolvedValue({ items: [] }),
  },
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const listFiles = vi.mocked(api.drive.listFiles);
const driveSources = vi.mocked(api.crm.getCompanyDriveSources);

const folder = (id: string, name: string) =>
  ({ id, name, mime_type: "folder", is_folder: true }) as FileItem;
const file = (id: string, name: string) =>
  ({ id, name, mime_type: "application/pdf", is_folder: false }) as FileItem;

/** Drive as a map folder_id → children; an unknown folder rejects. */
function drive(tree: Record<string, FileItem[]>) {
  listFiles.mockImplementation(async (params = {}) => {
    const files = tree[params.folder_id ?? ""];
    if (!files) throw new Error("listing failed");
    return { files, breadcrumb: [] } as never;
  });
}

const listedIds = () => listFiles.mock.calls.map(([p]) => p?.folder_id);

const link = (company_id: number, company_name: string) =>
  ({ link_id: company_id, company_id, company_name }) as ClientCompanyLink;

function sources(
  company_id: number,
  list: CompanyDriveSources["sources"],
): CompanyDriveSources {
  return {
    company_id,
    sources: list,
    lineage_status: "none",
    merged_into_company_id: null,
    historical_record_ids: [],
  };
}
const url = (id: string) => `https://drive.google.com/drive/folders/${id}`;

beforeEach(() => {
  listFiles.mockReset();
  driveSources.mockReset();
});

describe("TaxDocumentsSection — personal block", () => {
  it("opens the client's 03_Company_and_Tax subfolder", async () => {
    drive({
      FOLDER_123: [
        folder("PROFILE_1", "01_Profile"),
        folder("TAX_NEW", "03_Company_and_Tax"),
        folder("TAX_OLD", "03_Tax"),
      ],
      TAX_NEW: [folder("SPT_P", "SPT personal")],
    });
    render(<TaxDocumentsSection clientFolderId="FOLDER_123" />);

    await screen.findByText("SPT personal");
    expect(listedIds()).toEqual(["FOLDER_123", "TAX_NEW"]);
    expect(
      screen.getByRole("heading", { name: "03_Company_and_Tax" }),
    ).toBeTruthy();
    expect(screen.queryByText(/Tax folder not found/)).toBeNull();
  });

  it("falls back to the legacy 03_Tax alias", async () => {
    drive({
      FOLDER_123: [folder("TAX_OLD", "03_Tax")],
      TAX_OLD: [file("FILE_1", "spt.pdf")],
    });
    render(<TaxDocumentsSection clientFolderId="FOLDER_123" />);

    await screen.findByText("spt.pdf");
    expect(listedIds()).toContain("TAX_OLD");
    expect(screen.getByRole("heading", { name: "03_Tax" })).toBeTruthy();
  });

  it("shows the client root with a note when no tax folder exists", async () => {
    drive({ FOLDER_123: [folder("PROFILE_1", "01_Profile")] });
    render(<TaxDocumentsSection clientFolderId="FOLDER_123" />);

    await screen.findByText("Tax folder not found, showing client folder");
    await screen.findByText("01_Profile");
    expect(listedIds().every((id) => id === "FOLDER_123")).toBe(true);
  });

  it("says no folder is linked instead of listing anything", async () => {
    render(<TaxDocumentsSection clientFolderId={null} />);

    await screen.findByText("No Drive folder linked for this client");
    expect(listFiles).not.toHaveBeenCalled();
  });

  it("shows a listing error as an alert, never as an empty folder", async () => {
    listFiles.mockRejectedValue(new Error("403"));
    render(<TaxDocumentsSection clientFolderId="FOLDER_123" />);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/Could not load/);
    expect(screen.queryByText(/empty/i)).toBeNull();
  });

  it("opens files through the kita proxy, not drive.google.com", async () => {
    drive({
      FOLDER_123: [folder("TAX_NEW", "03_Company_and_Tax")],
      TAX_NEW: [file("FILE_1", "spt-2025.pdf")],
    });
    render(<TaxDocumentsSection clientFolderId="FOLDER_123" />);

    const a = await screen.findByRole("link", { name: /spt-2025\.pdf/ });
    expect(a.getAttribute("href")).toBe("/api/documents/proxy/FILE_1");
    expect(a.getAttribute("href")).not.toMatch(/drive\.google\.com/);
  });
});

describe("TaxDocumentsSection — company blocks", () => {
  it("prefers tax_dept_folder_id over the company folder", async () => {
    driveSources.mockResolvedValue(
      sources(9, [
        { url: url("COMPANY_9"), kinds: ["primary"] },
        { url: url("TAXDEPT_9"), kinds: ["restricted_tax"] },
      ]),
    );
    drive({ TAXDEPT_9: [file("FILE_2", "spt-badan.pdf")] });
    render(
      <TaxDocumentsSection
        clientFolderId={null}
        companyLinks={[link(9, "PT Example Sembilan")]}
      />,
    );

    await screen.findByText("spt-badan.pdf");
    expect(driveSources).toHaveBeenCalledWith(9);
    expect(listedIds()).toEqual(["TAXDEPT_9"]);
    expect(screen.getByText("PT Example Sembilan")).toBeTruthy();
  });

  it("then uses 02_Tax_Returns_and_Payments, else the company root", async () => {
    driveSources.mockImplementation(async (id) =>
      sources(id, [{ url: url(`COMPANY_${id}`), kinds: ["primary"] }]),
    );
    drive({
      COMPANY_9: [folder("CTAX_9", "02_Tax_Returns_and_Payments")],
      CTAX_9: [file("FILE_3", "pph.pdf")],
      COMPANY_8: [folder("LEGAL_8", "01_Legal_and_Licensing")],
    });
    render(
      <TaxDocumentsSection
        clientFolderId={null}
        companyLinks={[
          link(9, "PT Nine"),
          link(8, "PT Eight"),
          link(9, "PT Nine"),
        ]}
      />,
    );

    await screen.findByText("pph.pdf");
    await screen.findByText("Tax folder not found, showing company folder");
    await screen.findByText("01_Legal_and_Licensing");
    expect(driveSources).toHaveBeenCalledTimes(2); // duplicate link collapsed
    expect(listedIds()).toContain("CTAX_9");
  });

  it("says no folder is linked for a company without sources", async () => {
    driveSources.mockResolvedValue(sources(9, []));
    render(
      <TaxDocumentsSection
        clientFolderId={null}
        companyLinks={[link(9, "PT Nine")]}
      />,
    );

    await screen.findByText("No Drive folder linked for this company");
    expect(listFiles).not.toHaveBeenCalled();
  });

  it("shows a drive-sources failure as an alert", async () => {
    driveSources.mockRejectedValue(new Error("500"));
    render(
      <TaxDocumentsSection
        clientFolderId={null}
        companyLinks={[link(9, "PT Nine")]}
      />,
    );

    expect((await screen.findByRole("alert")).textContent).toMatch(
      /Could not load/,
    );
  });

  it("reads the folder id out of a drive-sources URL", () => {
    expect(folderIdFromUrl(url("COMPANY_9"))).toBe("COMPANY_9");
    expect(folderIdFromUrl("not a url")).toBeNull();
  });
});

describe("TaxTab renders Tax Documents", () => {
  it("shows the section with the client's tax folder", async () => {
    drive({
      FOLDER_123: [folder("TAX_NEW", "03_Company_and_Tax")],
      TAX_NEW: [file("FILE_1", "spt.pdf")],
    });
    render(
      <TaxTab
        clientId={1}
        formatDate={(d) => d}
        client={{ id: 1, google_drive_folder_id: "FOLDER_123" } as Client}
        taxConsultants={[]}
        now={new Date("2026-11-16T00:00:00Z")}
      />,
    );

    expect(screen.getByText("Tax Documents")).toBeTruthy();
    const block = (await screen.findAllByTestId("tax-folder-block"))[0];
    await waitFor(() =>
      expect(within(block).getByText("spt.pdf")).toBeTruthy(),
    );
  });
});

describe("TaxDocumentsSection — legacy company tax folder", () => {
  it("uses 03_DICHIARAZIONI_E_RICEVUTE_FISCALI when neither tax_dept nor 02_Tax_Returns_and_Payments exists", async () => {
    driveSources.mockResolvedValue(
      sources(9, [{ url: url("COMPANY_9"), kinds: ["primary"] }]),
    );
    drive({
      COMPANY_9: [
        folder("LEGAL_9", "01_Legal_and_Licensing"),
        folder("LEGACY_9", "03_DICHIARAZIONI_E_RICEVUTE_FISCALI"),
      ],
      LEGACY_9: [file("FILE_4", "legacy-spt.pdf")],
    });
    render(
      <TaxDocumentsSection
        clientFolderId={null}
        companyLinks={[link(9, "PT Nine")]}
      />,
    );

    await screen.findByText("legacy-spt.pdf");
    expect(listedIds()).toContain("LEGACY_9");
    expect(
      screen.queryByText("Tax folder not found, showing company folder"),
    ).toBeNull();
  });

  it("still prefers 02_Tax_Returns_and_Payments over the legacy alias", async () => {
    driveSources.mockResolvedValue(
      sources(9, [{ url: url("COMPANY_9"), kinds: ["primary"] }]),
    );
    drive({
      COMPANY_9: [
        folder("LEGACY_9", "03_DICHIARAZIONI_E_RICEVUTE_FISCALI"),
        folder("CTAX_9", "02_Tax_Returns_and_Payments"),
      ],
      CTAX_9: [file("FILE_3", "pph.pdf")],
    });
    render(
      <TaxDocumentsSection
        clientFolderId={null}
        companyLinks={[link(9, "PT Nine")]}
      />,
    );

    await screen.findByText("pph.pdf");
    expect(listedIds()).not.toContain("LEGACY_9");
  });
});
