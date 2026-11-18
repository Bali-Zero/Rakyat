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
    fireEvent.click(screen.getByRole("button", { name: /All/ }));
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
});
