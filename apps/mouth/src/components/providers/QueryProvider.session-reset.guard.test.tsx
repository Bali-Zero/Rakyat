/**
 * Guard: the browser keeps one query client for the whole page life and portal
 * query keys carry no account identity, so every session boundary (logout, 401
 * teardown, next login) must drop the whole cache. Synthetic data only.
 */
import { describe, it, expect } from "vitest";
import { render, waitFor } from "@testing-library/react";
import React from "react";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { QueryProvider } from "./QueryProvider";

function Seed({ onClient }: { onClient: (c: QueryClient) => void }) {
  const qc = useQueryClient();
  React.useEffect(() => {
    qc.setQueryData(["portal", "dashboard"], { marker: "account-a" });
    onClient(qc);
  }, [qc, onClient]);
  return null;
}

describe("QueryProvider session reset", () => {
  it.each(["clearToken", "setToken"] as const)(
    "%s on the real api client empties the provider's cache",
    async (boundary) => {
      let captured: QueryClient | undefined;
      const { unmount } = render(
        <QueryProvider>
          <Seed onClient={(c) => (captured = c)} />
        </QueryProvider>,
      );
      await waitFor(() => expect(captured).toBeDefined());
      expect(captured!.getQueryData(["portal", "dashboard"])).toEqual({
        marker: "account-a",
      });

      if (boundary === "clearToken") api.clearToken();
      else api.setToken("t.t.t");

      expect(captured!.getQueryData(["portal", "dashboard"])).toBeUndefined();
      unmount();
      api.clearToken();
    },
  );

  it("stops listening once unmounted", async () => {
    let captured: QueryClient | undefined;
    const { unmount } = render(
      <QueryProvider>
        <Seed onClient={(c) => (captured = c)} />
      </QueryProvider>,
    );
    await waitFor(() => expect(captured).toBeDefined());
    unmount();
    captured!.setQueryData(["portal", "dashboard"], { marker: "later" });
    api.clearToken();
    expect(captured!.getQueryData(["portal", "dashboard"])).toEqual({
      marker: "later",
    });
  });
});
