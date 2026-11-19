import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ClientDetailSkeleton } from "./ClientDetailSkeleton";

describe("ClientDetailSkeleton", () => {
  it("lays out one skeleton per first-screen section", () => {
    render(<ClientDetailSkeleton />);
    expect(
      screen.getByRole("status", { name: "Loading client" }),
    ).toHaveAttribute("aria-busy", "true");
    for (const id of [
      "skeleton-masthead",
      "skeleton-status",
      "skeleton-tabs",
      "skeleton-client-info",
      "skeleton-passport",
      "skeleton-documents",
    ]) {
      expect(screen.getByTestId(id)).toBeInTheDocument();
    }
  });

  it("offers no navigation before the profile says which tabs exist", () => {
    render(<ClientDetailSkeleton />);
    expect(screen.queryByRole("navigation")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
  });
});
