import type { ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { useCrmSearch, useGlobalSearch } from "../useCrmSearch";

vi.mock("@/lib/api", () => ({
  api: { crm: { getClients: vi.fn() } },
}));

const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider
    client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
  >
    {children}
  </QueryClientProvider>
);

const row = (id: number, name: string) => ({
  id,
  full_name: name,
  email: `c${id}@example.test`,
  status: "active",
});

describe("useCrmSearch suggestions", () => {
  beforeEach(() => vi.mocked(api.crm.getClients).mockReset());

  it("lists the names the backend matched, even when they are not a substring of the query", async () => {
    vi.mocked(api.crm.getClients).mockResolvedValue([
      row(1, "Müller Anna"),
      row(2, "Mario Rossi"),
    ] as never);
    const { result } = renderHook(() => useCrmSearch(), { wrapper });
    act(() => result.current.updateQuery("muller rossi"));
    await waitFor(() =>
      expect(result.current.suggestions).toEqual([
        "Müller Anna",
        "Mario Rossi",
      ]),
    );
    expect(api.crm.getClients).toHaveBeenCalledWith({
      search: "muller rossi",
      limit: 20,
    });
  });

  it("shows at most five suggestions", async () => {
    vi.mocked(api.crm.getClients).mockResolvedValue(
      Array.from({ length: 8 }, (_, i) =>
        row(i + 1, `Synthetic ${i + 1}`),
      ) as never,
    );
    const { result } = renderHook(() => useCrmSearch(), { wrapper });
    act(() => result.current.updateQuery("synthetic"));
    await waitFor(() => expect(result.current.suggestions).toHaveLength(5));
  });
});

describe("useGlobalSearch", () => {
  beforeEach(() => vi.mocked(api.crm.getClients).mockReset());

  it("sends the query as typed and maps the clients it gets back", async () => {
    vi.mocked(api.crm.getClients).mockResolvedValue([
      row(1, "Müller Anna"),
    ] as never);
    const { result } = renderHook(() => useGlobalSearch({ limit: 7 }), {
      wrapper,
    });
    act(() => result.current.setQuery("muller"));
    await waitFor(() =>
      expect(api.crm.getClients).toHaveBeenCalledWith({
        search: "muller",
        limit: 7,
      }),
    );
    await waitFor(() =>
      expect(result.current.results.map((r) => r.title)).toEqual([
        "Müller Anna",
      ]),
    );
  });
});
