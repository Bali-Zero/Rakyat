import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/error-handler";

import RelatedRegistryPage from "./page";

const apiMock = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@/lib/api", () => ({ api: apiMock }));
vi.mock("@/lib/logger", () => ({
  logger: { error: vi.fn(), warn: vi.fn(), info: vi.fn(), debug: vi.fn() },
}));

const ROW_A = {
  id: 7,
  kind: "person",
  display_name: "ZzAaa Person",
  date_of_birth: null,
  nationality: null,
  identifiers: [
    { type: "passport", value: "ZZ-1" },
    { type: "permit", value: "ZZ-2" },
    { type: "nik", value: "ZZ-3" },
    { type: "npwp", value: "ZZ-4" },
  ],
  document_count: 3,
};
const ROW_UNNAMED = {
  id: 8,
  kind: "company",
  display_name: null,
  date_of_birth: null,
  nationality: null,
  identifiers: [{ type: "ahu", value: "ZZ-AHU-1" }],
  document_count: 0,
};
const ROW_B = { ...ROW_A, id: 9, display_name: "ZzBbb Person" };

const page = (items: unknown[], total = items.length) => ({
  items,
  total,
  limit: 50,
  offset: 0,
});

const DRIVE_ID = "ZzDriveFileAaaaaaaa01";
const DETAIL_A = {
  id: 7,
  kind: "person",
  display_name: "ZzAaa Person",
  date_of_birth: "1990-01-02",
  nationality: "ZZ-land",
  sex: null,
  created_at: "2026-01-01T00:00:00Z",
  identifiers: [{ type: "passport", value: "ZZ-1" }],
  documents: [
    {
      sha256: "1".repeat(64),
      doc_type: "passport",
      mime_type: "application/pdf",
      byte_size: 2048,
      first_seen_on: "2026-01-05",
      drive_file_ids: [DRIVE_ID],
    },
  ],
};
const DETAIL_B = {
  ...DETAIL_A,
  id: 9,
  display_name: "ZzBbb Person",
  identifiers: [{ type: "passport", value: "ZZ-9" }],
  documents: [],
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

const isDetail = (url: string) => /related-registry\/\d+$/.test(url);
const params = (url: string) => new URL(url, "http://x").searchParams;
const lastUrl = () =>
  apiMock.get.mock.calls[apiMock.get.mock.calls.length - 1][0] as string;
const settle = () => act(async () => {});
const advance = (ms: number) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
const type = (value: string) =>
  fireEvent.change(screen.getByLabelText("Search"), { target: { value } });

function defaults(list: unknown = page([ROW_A, ROW_UNNAMED])) {
  apiMock.get.mockImplementation((url: string) =>
    Promise.resolve(isDetail(url) ? DETAIL_A : list),
  );
}

beforeEach(() => {
  apiMock.get.mockReset();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("related subjects registry page", () => {
  it("F1 renders rows with name or dash, kind, identifiers and document count", async () => {
    defaults();
    render(<RelatedRegistryPage />);
    await settle();
    const rows = screen.getAllByTestId("registry-row");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText("ZzAaa Person")).toBeTruthy();
    expect(within(rows[0]).getByText("person")).toBeTruthy();
    expect(within(rows[0]).getByText("PASSPORT ZZ-1")).toBeTruthy();
    expect(within(rows[0]).getByText("NIK ZZ-3")).toBeTruthy();
    expect(within(rows[0]).queryByText("NPWP ZZ-4")).toBeNull();
    expect(within(rows[0]).getByText("+1")).toBeTruthy();
    expect(within(rows[0]).getByText("3")).toBeTruthy();
    expect(within(rows[1]).getByText("—")).toBeTruthy();
    expect(within(rows[1]).getByText("company")).toBeTruthy();
    expect(within(rows[1]).queryByText(/^\+/)).toBeNull();
  });

  it("F2 a 403 shows the admin-only message and no table", async () => {
    apiMock.get.mockRejectedValue(
      new ApiError("CRM admin required", 403, { detail: "CRM admin required" }),
    );
    render(<RelatedRegistryPage />);
    await settle();
    expect(screen.getByText("Admin only.")).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByTestId("registry-row")).toBeNull();
  });

  it("F3 debounces 300 ms, drops one character, and keeps the later search on reverse answers", async () => {
    vi.useFakeTimers();
    defaults();
    render(<RelatedRegistryPage />);
    await advance(0);
    const before = apiMock.get.mock.calls.length;
    expect(before).toBe(1);

    type("ab");
    await advance(200);
    type("abc");
    await advance(299);
    expect(apiMock.get.mock.calls.length).toBe(before);
    await advance(1);
    expect(apiMock.get.mock.calls.length).toBe(before + 1);
    expect(params(lastUrl()).get("q")).toBe("abc");

    // one character cancels the pending debounce and asks nothing
    type("abcd");
    await advance(100);
    type("a");
    await advance(2000);
    expect(apiMock.get.mock.calls.length).toBe(before + 1);

    // two searches, answers in reverse order: the rows of the LATER one stay
    const first = deferred<unknown>();
    const second = deferred<unknown>();
    apiMock.get.mockReset();
    apiMock.get
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);
    type("zz");
    await advance(300);
    type("zzz");
    await advance(300);
    expect(apiMock.get.mock.calls.length).toBe(2);
    await act(async () => second.resolve(page([ROW_B])));
    await act(async () => first.resolve(page([ROW_A])));
    expect(screen.getByText("ZzBbb Person")).toBeTruthy();
    expect(screen.queryByText("ZzAaa Person")).toBeNull();
  });

  it("F4 the kind filter sends kind", async () => {
    defaults();
    render(<RelatedRegistryPage />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Companies/ }));
    await settle();
    expect(params(lastUrl()).get("kind")).toBe("company");
    fireEvent.click(
      within(screen.getByRole("group", { name: "Subject kind" })).getByRole(
        "button",
        { name: /All/ },
      ),
    );
    await settle();
    expect(params(lastUrl()).has("kind")).toBe(false);
  });

  it("F5 the next page sends offset=50", async () => {
    defaults(page([ROW_A], 120));
    render(<RelatedRegistryPage />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await settle();
    expect(params(lastUrl()).get("offset")).toBe("50");
    expect(params(lastUrl()).get("limit")).toBe("50");
  });

  it("F6 a row click asks the detail and a later click wins on reverse answers", async () => {
    const original = Element.prototype.scrollIntoView;
    const scrollMock = vi.fn();
    Element.prototype.scrollIntoView = scrollMock;
    try {
      await f6();
      expect(scrollMock).toHaveBeenCalled();
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });

  async function f6() {
    defaults(page([ROW_A, ROW_B]));
    render(<RelatedRegistryPage />);
    await settle();
    fireEvent.click(screen.getAllByTestId("registry-row")[0]);
    await settle();
    expect(lastUrl()).toBe("/api/crm/related-registry/7");
    const detail = screen.getByLabelText("Subject detail");
    expect(within(detail).getByText("ZzAaa Person")).toBeTruthy();
    expect(within(detail).getByText("PASSPORT ZZ-1")).toBeTruthy();
    expect(within(detail).getByText(/application\/pdf/)).toBeTruthy();
    expect(within(detail).getByText(/2\.0 KB/)).toBeTruthy();
    expect(within(detail).getByText(/2026-01-05/)).toBeTruthy();

    const first = deferred<unknown>();
    const second = deferred<unknown>();
    apiMock.get.mockReset();
    apiMock.get
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);
    fireEvent.click(screen.getAllByTestId("registry-row")[1]);
    fireEvent.click(screen.getAllByTestId("registry-row")[0]);
    await act(async () => second.resolve(DETAIL_A));
    await act(async () => first.resolve(DETAIL_B));
    const after = screen.getByLabelText("Subject detail");
    expect(within(after).getByText("ZzAaa Person")).toBeTruthy();
    expect(within(after).queryByText("ZzBbb Person")).toBeNull();
  }

  it("F7 a Drive id is a link to the Drive viewer in a new tab", async () => {
    defaults();
    render(<RelatedRegistryPage />);
    await settle();
    fireEvent.click(screen.getAllByTestId("registry-row")[0]);
    await settle();
    const link = screen.getByRole("link", { name: DRIVE_ID });
    expect(link.getAttribute("href")).toBe(
      `https://drive.google.com/file/d/${DRIVE_ID}/view`,
    );
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
  });

  it("F8 shows the empty state on zero rows", async () => {
    defaults(page([]));
    render(<RelatedRegistryPage />);
    await settle();
    expect(screen.getByText("No subjects found.")).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("F9 shows an error notice on a 500", async () => {
    apiMock.get.mockRejectedValue(
      new ApiError("Registry unavailable", 500, { detail: "x" }),
    );
    render(<RelatedRegistryPage />);
    await settle();
    expect(screen.getByRole("alert").textContent).toContain(
      "Registry unavailable",
    );
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("F10 changing the search or the kind on a later page asks offset=0", async () => {
    vi.useFakeTimers();
    defaults(page([ROW_A], 220));
    render(<RelatedRegistryPage />);
    await advance(0);
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await advance(0);
    expect(params(lastUrl()).get("offset")).toBe("50");

    fireEvent.click(screen.getByRole("button", { name: /People/ }));
    await advance(0);
    expect(params(lastUrl()).get("kind")).toBe("person");
    expect(params(lastUrl()).get("offset")).toBe("0");

    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await advance(0);
    expect(params(lastUrl()).get("offset")).toBe("50");
    type("ab");
    await advance(300);
    expect(params(lastUrl()).get("q")).toBe("ab");
    expect(params(lastUrl()).get("offset")).toBe("0");
  });

  it("F11 displays the returned total", async () => {
    defaults(page([ROW_A, ROW_UNNAMED], 137));
    render(<RelatedRegistryPage />);
    await settle();
    expect(screen.getByText(/Showing 1-50 of 137/)).toBeTruthy();
  });

  const OCR_TITLE =
    "This name was read from a document by OCR. It is not verified.";
  const rowWith = (id: number, source: string | null) => ({
    ...ROW_A,
    id,
    display_name: `ZzName ${id}`,
    name_source: source,
  });

  it('F12 "OCR" marks an ocr_corroborated name, with its title, in the row and in the detail', async () => {
    const detail = {
      ...DETAIL_A,
      name_source: "ocr_corroborated",
      names: [],
      names_total: 0,
    };
    apiMock.get.mockImplementation((url: string) =>
      Promise.resolve(
        isDetail(url) ? detail : page([rowWith(7, "ocr_corroborated")]),
      ),
    );
    render(<RelatedRegistryPage />);
    await settle();
    const mark = within(screen.getAllByTestId("registry-row")[0]).getByTestId(
      "name-mark",
    );
    expect(mark.textContent).toBe("OCR");
    expect(mark.getAttribute("title")).toBe(OCR_TITLE);
    fireEvent.click(screen.getAllByTestId("registry-row")[0]);
    await settle();
    const heading = screen.getByRole("heading", { level: 2 });
    const detailMark = within(heading).getByTestId("name-mark");
    expect(detailMark.textContent).toBe("OCR");
    expect(detailMark.getAttribute("title")).toBe(OCR_TITLE);
  });

  it('F13 "OCR, one file" marks an ocr_single name, with its title', async () => {
    defaults(page([rowWith(7, "ocr_single")]));
    render(<RelatedRegistryPage />);
    await settle();
    const mark = within(screen.getAllByTestId("registry-row")[0]).getByTestId(
      "name-mark",
    );
    expect(mark.textContent).toBe("OCR, one file");
    expect(mark.getAttribute("title")).toBe(OCR_TITLE);
  });

  it("F14 registration, mrz_corrected, manual and a null source carry no mark", async () => {
    defaults(
      page([
        rowWith(1, "registration"),
        rowWith(2, "mrz_corrected"),
        rowWith(3, "manual"),
        rowWith(4, null),
        ROW_A,
      ]),
    );
    render(<RelatedRegistryPage />);
    await settle();
    expect(screen.getAllByTestId("registry-row")).toHaveLength(5);
    expect(screen.queryAllByTestId("name-mark")).toHaveLength(0);
  });

  it("F15 the detail lists the names with source and date", async () => {
    const detail = {
      ...DETAIL_A,
      names: [
        {
          name: "ZZ REG NAME",
          source: "registration",
          created_at: "2026-01-01T00:00:00Z",
        },
        {
          name: "ZZ OCR NAME",
          source: "ocr_single",
          created_at: "2026-02-03T10:00:00Z",
        },
      ],
      names_total: 2,
    };
    apiMock.get.mockImplementation((url: string) =>
      Promise.resolve(isDetail(url) ? detail : page([ROW_A])),
    );
    render(<RelatedRegistryPage />);
    await settle();
    fireEvent.click(screen.getAllByTestId("registry-row")[0]);
    await settle();
    const entries = screen.getAllByTestId("registry-name");
    expect(entries).toHaveLength(2);
    expect(entries[0].textContent).toBe(
      "ZZ REG NAME · registration · 2026-01-01",
    );
    expect(entries[1].textContent).toBe(
      "ZZ OCR NAME · OCR, one file · 2026-02-03",
    );
    expect(screen.queryByTestId("registry-names-truncated")).toBeNull();
  });

  it("F16 says so when names_total is above the number shown", async () => {
    const shown = Array.from({ length: 20 }, (_, i) => ({
      name: `ZZ ROW ${i}`,
      source: "ocr_single",
      created_at: "2026-01-01T00:00:00Z",
    }));
    const detail = { ...DETAIL_A, names: shown, names_total: 26 };
    apiMock.get.mockImplementation((url: string) =>
      Promise.resolve(isDetail(url) ? detail : page([ROW_A])),
    );
    render(<RelatedRegistryPage />);
    await settle();
    fireEvent.click(screen.getAllByTestId("registry-row")[0]);
    await settle();
    expect(screen.getAllByTestId("registry-name")).toHaveLength(20);
    expect(screen.getByTestId("registry-names-truncated").textContent).toBe(
      "Showing 20 of 26 names.",
    );
  });

  it("F17 an answer without names shows the empty names line and no mark", async () => {
    defaults();
    render(<RelatedRegistryPage />);
    await settle();
    fireEvent.click(screen.getAllByTestId("registry-row")[0]);
    await settle();
    expect(screen.getByText("No names.")).toBeTruthy();
    expect(screen.queryAllByTestId("name-mark")).toHaveLength(0);
  });

  const linkOf = (over: Record<string, unknown> = {}) => ({
    client_id: 424242,
    client_name: "ZzClient One",
    client_live: true,
    basis: "drive_folder",
    relation: "unknown",
    created_at: "2026-03-04T10:00:00Z",
    ...over,
  });
  const withLinks = (links: unknown[], links_total = links.length) => ({
    ...DETAIL_A,
    links,
    links_total,
  });
  const openDetail = async (detail: unknown, list: unknown = page([ROW_A])) => {
    apiMock.get.mockImplementation((url: string) =>
      Promise.resolve(isDetail(url) ? detail : list),
    );
    render(<RelatedRegistryPage />);
    await settle();
    fireEvent.click(screen.getAllByTestId("registry-row")[0]);
    await settle();
  };

  it("F18 the Clients block lists the name as a link to the CRM client, the basis and the date", async () => {
    await openDetail(
      withLinks([
        linkOf(),
        linkOf({
          client_id: 424243,
          client_name: "ZzClient Two",
          basis: "whatsapp_phone",
          created_at: "2026-03-05T10:00:00Z",
        }),
      ]),
    );
    const block = screen.getByTestId("registry-clients");
    expect(within(block).getByRole("heading", { level: 3 }).textContent).toBe(
      "Clients",
    );
    expect(
      within(block).getByText("Proposed from documents, not confirmed."),
    ).toBeTruthy();
    const entries = within(block).getAllByTestId("registry-client");
    expect(entries).toHaveLength(2);
    expect(entries[0].textContent).toBe(
      "ZzClient One · Drive folder · 2026-03-04",
    );
    expect(entries[1].textContent).toBe(
      "ZzClient Two · WhatsApp phone · 2026-03-05",
    );
    const anchor = within(entries[0]).getByRole("link", {
      name: "ZzClient One",
    });
    expect(anchor.getAttribute("href")).toBe("/clients/424242");
    expect(within(entries[1]).getByRole("link").getAttribute("href")).toBe(
      "/clients/424243",
    );
  });

  it("F19 a link whose client is not live shows the muted line, no name and no anchor", async () => {
    await openDetail(
      withLinks([
        linkOf({ client_name: null, client_live: false, basis: "identity" }),
      ]),
    );
    const entry = screen.getByTestId("registry-client");
    expect(entry.textContent).toBe(
      "Client no longer in the CRM · Identity · 2026-03-04",
    );
    expect(within(entry).queryByRole("link")).toBeNull();
    expect(entry.textContent).not.toContain("ZzClient");
  });

  it("F20 a subject without links has no block and a total above the shown number is said", async () => {
    await openDetail(withLinks([]));
    expect(screen.queryByTestId("registry-clients")).toBeNull();
    expect(screen.queryByText("Clients")).toBeNull();
  });

  it("F20b a links_total above the shown number is said, as the names list says it", async () => {
    const shown = Array.from({ length: 20 }, (_, i) =>
      linkOf({ client_id: 424300 + i, client_name: `ZzClient ${i}` }),
    );
    await openDetail(withLinks(shown, 25));
    expect(screen.getAllByTestId("registry-client")).toHaveLength(20);
    expect(screen.getByTestId("registry-clients-truncated").textContent).toBe(
      "Showing 20 of 25 clients.",
    );
  });

  it("F21 the indicator shows on rows with links_count above zero only", async () => {
    defaults(
      page([
        { ...ROW_A, id: 1, display_name: "ZzOne", links_count: 2 },
        { ...ROW_A, id: 2, display_name: "ZzTwo", links_count: 0 },
        { ...ROW_A, id: 3, display_name: "ZzThree" },
      ]),
    );
    render(<RelatedRegistryPage />);
    await settle();
    const rows = screen.getAllByTestId("registry-row");
    expect(within(rows[0]).getByTestId("client-mark").textContent).toBe(
      "Client",
    );
    expect(within(rows[1]).queryByTestId("client-mark")).toBeNull();
    expect(within(rows[2]).queryByTestId("client-mark")).toBeNull();
    expect(screen.getAllByTestId("client-mark")).toHaveLength(1);
  });

  it("F22 the filter sends linked=true and linked=false, omits it for All, and a change on a later page asks offset=0", async () => {
    vi.useFakeTimers();
    defaults(page([ROW_A], 220));
    render(<RelatedRegistryPage />);
    await advance(0);
    expect(params(lastUrl()).has("linked")).toBe(false);
    const group = () => screen.getByRole("group", { name: "Client link" });
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await advance(0);
    expect(params(lastUrl()).get("offset")).toBe("50");

    fireEvent.click(within(group()).getByRole("button", { name: "Linked" }));
    await advance(0);
    expect(params(lastUrl()).get("linked")).toBe("true");
    expect(params(lastUrl()).get("offset")).toBe("0");

    fireEvent.click(
      within(group()).getByRole("button", { name: "Not linked" }),
    );
    await advance(0);
    expect(params(lastUrl()).get("linked")).toBe("false");

    fireEvent.click(within(group()).getByRole("button", { name: "All" }));
    await advance(0);
    expect(params(lastUrl()).has("linked")).toBe(false);
    expect(params(lastUrl()).has("kind")).toBe(false);
    // the two groups are told apart by their names
    expect(screen.getByRole("group", { name: "Subject kind" })).toBeTruthy();
  });

  it("F23 no control of the Clients block writes: no button, and every call is a read", async () => {
    await openDetail(withLinks([linkOf(), linkOf({ client_live: false })]));
    const block = screen.getByTestId("registry-clients");
    expect(within(block).queryAllByRole("button")).toHaveLength(0);
    expect(Object.keys(apiMock)).toEqual(["get"]);
    for (const call of apiMock.get.mock.calls) {
      expect(String(call[0])).toMatch(/^\/api\/crm\/related-registry/);
    }
  });

  it("F24 each of the four basis labels", async () => {
    await openDetail(
      withLinks([
        linkOf({ client_id: 1, basis: "drive_folder" }),
        linkOf({ client_id: 2, basis: "whatsapp_phone" }),
        linkOf({ client_id: 3, basis: "identity" }),
        linkOf({ client_id: 4, basis: "manual" }),
      ]),
    );
    const labels = screen
      .getAllByTestId("registry-client")
      .map((e) => e.textContent?.split(" · ")[1]);
    expect(labels).toEqual([
      "Drive folder",
      "WhatsApp phone",
      "Identity",
      "Entered by hand",
    ]);
  });

  it("F30 the legend says what each basis shown asserts, once and in a fixed order, and what no basis says", async () => {
    await openDetail(
      withLinks([
        linkOf({ client_id: 1, basis: "whatsapp_phone" }),
        linkOf({ client_id: 2, basis: "drive_folder" }),
        linkOf({ client_id: 3, basis: "whatsapp_phone" }),
        linkOf({ client_id: 4, basis: "zz_unknown_basis" }),
      ]),
    );
    const legend = within(screen.getByTestId("registry-clients")).getByTestId(
      "registry-basis-legend",
    );
    expect(
      within(legend)
        .getAllByTestId("registry-basis-meaning")
        .map((e) => e.textContent),
    ).toEqual([
      "Drive folder: files of this subject sit in the client's Drive folder.",
      "WhatsApp phone: files of this subject arrived in the WhatsApp thread of the client's phone.",
    ]);
    expect(
      within(legend).getByTestId("registry-basis-caveat").textContent,
    ).toBe(
      "A basis says where a link comes from, not how the subject and the client are related.",
    );
    // the entries themselves are unchanged: the legend is beside them, not inside
    expect(
      screen.getAllByTestId("registry-client").map((e) => e.textContent),
    ).toEqual([
      "ZzClient One · WhatsApp phone · 2026-03-04",
      "ZzClient One · Drive folder · 2026-03-04",
      "ZzClient One · WhatsApp phone · 2026-03-04",
      "ZzClient One · — · 2026-03-04",
    ]);
  });

  it("F31 the other two bases have their sentence", async () => {
    await openDetail(
      withLinks([
        linkOf({ client_id: 1, basis: "manual" }),
        linkOf({ client_id: 2, basis: "identity" }),
      ]),
    );
    expect(
      screen.getAllByTestId("registry-basis-meaning").map((e) => e.textContent),
    ).toEqual([
      "Identity: an identifier or identity datum of this subject equals one held by the client.",
      "Entered by hand: a staff member entered this link.",
    ]);
  });

  it("F32 links of an unknown basis alone show the block and no legend", async () => {
    await openDetail(withLinks([linkOf({ basis: "zz_unknown_basis" })]));
    expect(screen.getByTestId("registry-clients")).toBeTruthy();
    expect(screen.queryByTestId("registry-basis-legend")).toBeNull();
    expect(screen.queryByTestId("registry-basis-caveat")).toBeNull();
  });

  const MERGED_AT = "2026-02-03T08:00:00Z";
  const mergedRecord = (over: Record<string, unknown> = {}) => ({
    subject_id: 11,
    basis: "permit_stamp",
    merged_at: MERGED_AT,
    ...over,
  });
  const survivorDetail = (over: Record<string, unknown> = {}) => ({
    ...DETAIL_A,
    merged_into: null,
    merged_from: [mergedRecord()],
    ...over,
  });

  it("F25 a survivor says how many records were merged into it, singular for one, a line per record", async () => {
    await openDetail(survivorDetail());
    const block = screen.getByTestId("registry-merged-from");
    expect(
      within(block).getByText("1 record merged into this one"),
    ).toBeTruthy();
    const lines = within(block).getAllByTestId("registry-merged-record");
    expect(lines.map((l) => l.textContent)).toEqual([
      "#11 · Permit stamp · 2026-02-03",
    ]);
    expect(screen.queryByTestId("open-survivor")).toBeNull();
    expect(screen.queryByText(/This record was merged into/)).toBeNull();

    document.body.innerHTML = "";
    apiMock.get.mockReset();
    await openDetail(
      survivorDetail({
        merged_from: [
          mergedRecord(),
          mergedRecord({ subject_id: 12, basis: "manual" }),
        ],
      }),
    );
    const plural = screen.getByTestId("registry-merged-from");
    expect(
      within(plural).getByText("2 records merged into this one"),
    ).toBeTruthy();
    expect(
      within(plural).getAllByTestId("registry-merged-record"),
    ).toHaveLength(2);
  });

  it("F26 items from a merged record carry the mark in identifiers, documents, names and clients; own items none", async () => {
    const detail = survivorDetail({
      identifiers: [
        { type: "passport", value: "ZZ-1", from_subject_id: null },
        { type: "permit", value: "ZZ-P1", from_subject_id: 11 },
      ],
      documents: [
        { ...DETAIL_A.documents[0], from_subject_id: null },
        {
          ...DETAIL_A.documents[0],
          sha256: "2".repeat(64),
          drive_file_ids: [],
          from_subject_id: 11,
        },
      ],
      names: [
        {
          name: "ZZ OWN",
          source: "registration",
          created_at: "2026-01-01T00:00:00Z",
          from_subject_id: null,
        },
        {
          name: "ZZ THEIRS",
          source: "ocr_single",
          created_at: "2026-02-03T00:00:00Z",
          from_subject_id: 11,
        },
      ],
      names_total: 2,
      links: [
        { ...linkOf(), from_subject_id: null },
        { ...linkOf({ client_id: 424243 }), from_subject_id: 11 },
      ],
      links_total: 2,
    });
    await openDetail(detail);
    const marks = screen.getAllByTestId("from-mark");
    expect(marks).toHaveLength(4);
    for (const mark of marks) {
      expect(mark.textContent).toBe("from merged record #11");
    }
    const names = screen.getAllByTestId("registry-name");
    expect(within(names[0]).queryByTestId("from-mark")).toBeNull();
    expect(within(names[1]).getByTestId("from-mark")).toBeTruthy();
    const clients = screen.getAllByTestId("registry-client");
    expect(within(clients[0]).queryByTestId("from-mark")).toBeNull();
    expect(within(clients[1]).getByTestId("from-mark")).toBeTruthy();
    const docs = screen.getAllByTestId("registry-document");
    expect(within(docs[0]).queryByTestId("from-mark")).toBeNull();
    expect(within(docs[1]).getByTestId("from-mark")).toBeTruthy();
    const region = within(
      screen.getByRole("region", { name: "Subject detail" }),
    );
    const theirs = region
      .getByText(/PERMIT ZZ-P1/)
      .closest("li") as HTMLElement;
    expect(within(theirs).getByTestId("from-mark")).toBeTruthy();
    const own = region.getByText(/PASSPORT ZZ-1/).closest("li") as HTMLElement;
    expect(within(own).queryByTestId("from-mark")).toBeNull();
  });

  it("F27 a merged-away record shows the banner with the survivor's id; the control asks the survivor's detail and nothing else", async () => {
    const away = {
      ...DETAIL_A,
      id: 11,
      merged_into: 7,
      merged_from: [],
    };
    apiMock.get.mockImplementation((url: string) =>
      Promise.resolve(
        isDetail(url)
          ? url.endsWith("/7")
            ? survivorDetail()
            : away
          : page([{ ...ROW_A, id: 11 }]),
      ),
    );
    render(<RelatedRegistryPage />);
    await settle();
    fireEvent.click(screen.getAllByTestId("registry-row")[0]);
    await settle();
    expect(
      screen.getByText(/This record was merged into subject #7/),
    ).toBeTruthy();
    expect(screen.queryByTestId("registry-merged-from")).toBeNull();
    const region = screen.getByRole("region", { name: "Subject detail" });
    expect(
      within(region).getByRole("heading", { name: /ZzAaa Person/ }),
    ).toBeTruthy();
    expect(within(region).queryAllByRole("button")).toHaveLength(1);
    const before = apiMock.get.mock.calls.length;
    fireEvent.click(screen.getByTestId("open-survivor"));
    await settle();
    const asked = apiMock.get.mock.calls.slice(before).map((c) => c[0]);
    expect(asked).toEqual(["/api/crm/related-registry/7"]);
    expect(screen.getByTestId("registry-merged-from")).toBeTruthy();
    expect(screen.queryByText(/This record was merged into/)).toBeNull();
    expect(Object.keys(apiMock)).toEqual(["get"]);
  });

  it("F28 a subject in no merge, and an answer without the new fields, show no line, banner or mark; every call is a GET", async () => {
    await openDetail(DETAIL_A);
    for (const id of ["registry-merged-from", "from-mark", "open-survivor"]) {
      expect(screen.queryByTestId(id)).toBeNull();
    }
    expect(screen.queryByText(/merged/)).toBeNull();

    document.body.innerHTML = "";
    apiMock.get.mockReset();
    await openDetail({
      ...DETAIL_A,
      identifiers: [{ type: "passport", value: "ZZ-1", from_subject_id: null }],
      merged_from: [],
      merged_into: null,
    });
    for (const id of ["registry-merged-from", "from-mark", "open-survivor"]) {
      expect(screen.queryByTestId(id)).toBeNull();
    }
    expect(screen.queryByText(/merged/)).toBeNull();
    expect(Object.keys(apiMock)).toEqual(["get"]);
    for (const call of apiMock.get.mock.calls) {
      expect(String(call[0])).toMatch(/^\/api\/crm\/related-registry/);
    }
  });

  it("F29 the three basis labels, a null basis, a null date, and both null", async () => {
    await openDetail(
      survivorDetail({
        merged_from: [
          mergedRecord({ subject_id: 11, basis: "permit_stamp" }),
          mergedRecord({ subject_id: 12, basis: "same_identity" }),
          mergedRecord({ subject_id: 13, basis: "manual" }),
          mergedRecord({ subject_id: 14, basis: null }),
          mergedRecord({ subject_id: 15, merged_at: null }),
          mergedRecord({ subject_id: 16, basis: null, merged_at: null }),
        ],
      }),
    );
    expect(
      screen.getAllByTestId("registry-merged-record").map((l) => l.textContent),
    ).toEqual([
      "#11 · Permit stamp · 2026-02-03",
      "#12 · Same identity · 2026-02-03",
      "#13 · Entered by hand · 2026-02-03",
      "#14 · 2026-02-03",
      "#15 · Permit stamp",
      "#16",
    ]);
    expect(screen.getByText("6 records merged into this one")).toBeTruthy();
  });
});
