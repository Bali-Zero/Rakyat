import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { CommandPalette, type CommandAction } from "./CommandPalette";

const STATIC: CommandAction[] = [
  { id: "go-clients", label: "Clients", group: "Navigation", run: () => {} },
  { id: "go-process", label: "Process", group: "Navigation", run: () => {} },
];

describe("CommandPalette queryActions", () => {
  it("shows a query action for a query no static action matches, and runs it with the typed text", () => {
    const run = vi.fn();
    const queryActions = vi.fn((q: string): CommandAction[] => [
      { id: "search", label: `Search clients for "${q}"`, run: () => run(q) },
    ]);
    const { getByRole, getByText } = render(
      <CommandPalette
        open
        actions={STATIC}
        queryActions={queryActions}
        onClose={() => {}}
      />,
    );
    const input = getByRole("combobox");
    fireEvent.change(input, { target: { value: "  ayu  " } });
    expect(getByText('Search clients for "ayu"')).toBeTruthy();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(run).toHaveBeenCalledWith("ayu");
  });

  it("keeps matching static actions first and the query action after them", () => {
    const { getByRole, container } = render(
      <CommandPalette
        open
        actions={STATIC}
        queryActions={(q) => [
          { id: "search", label: `find ${q}`, run: () => {} },
        ]}
        onClose={() => {}}
      />,
    );
    fireEvent.change(getByRole("combobox"), { target: { value: "cli" } });
    const items = Array.from(container.querySelectorAll("li")).map(
      (li) => li.textContent,
    );
    expect(items).toEqual(["NavigationClients", "find cli"]);
  });

  it("does not call queryActions for an empty or blank query", () => {
    const queryActions = vi.fn(() => []);
    const { getByRole, container } = render(
      <CommandPalette
        open
        actions={STATIC}
        queryActions={queryActions}
        onClose={() => {}}
      />,
    );
    fireEvent.change(getByRole("combobox"), { target: { value: "   " } });
    expect(queryActions).not.toHaveBeenCalled();
    expect(container.querySelectorAll("li")).toHaveLength(2);
  });

  it("without queryActions an unmatched query lists nothing (old behaviour)", () => {
    const { getByRole, container } = render(
      <CommandPalette open actions={STATIC} onClose={() => {}} />,
    );
    fireEvent.change(getByRole("combobox"), { target: { value: "ayu" } });
    expect(container.querySelectorAll("li")).toHaveLength(0);
  });
});
