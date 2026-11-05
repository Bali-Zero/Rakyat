import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { LogContact } from "./LogContact";

vi.mock("@/lib/api", () => ({
  api: { crm: { createInteraction: vi.fn() } },
}));

const create = () => vi.mocked(api.crm.createInteraction);

const open = () => {
  fireEvent.click(screen.getByTestId("log-contact-trigger"));
  return screen.getByRole("dialog", { name: "Log contact" });
};

const setup = (onLogged = vi.fn().mockResolvedValue(undefined)) => {
  render(
    <LogContact
      clientId={42}
      teamMember="staff@example.test"
      onLogged={onLogged}
    />,
  );
  return onLogged;
};

describe("LogContact", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    create().mockResolvedValue({} as never);
  });

  it("offers the four presets and focuses the first when opened", () => {
    setup();
    const panel = open();
    for (const name of ["Call", "Email", "Meeting", "Note"])
      expect(screen.getByRole("radio", { name })).toBeInTheDocument();
    expect(panel).toBeInTheDocument();
    expect(document.activeElement).toBe(
      screen.getByRole("radio", { name: "Call" }),
    );
  });

  it("GUILT: each preset posts its own type and channel, the label as summary when there is no note", async () => {
    const cases = [
      ["Call", "call", "phone"],
      ["Email", "email", "gmail"],
      ["Meeting", "meeting", "in_person"],
      ["Note", "note", null],
    ] as const;
    for (const [label, type, channel] of cases) {
      create().mockClear();
      const { unmount } = render(
        <LogContact
          clientId={42}
          teamMember="staff@example.test"
          onLogged={vi.fn()}
        />,
      );
      fireEvent.click(screen.getByTestId("log-contact-trigger"));
      fireEvent.click(screen.getByRole("radio", { name: label }));
      fireEvent.click(screen.getByTestId("log-contact-submit"));
      await waitFor(() => expect(create()).toHaveBeenCalledTimes(1));
      expect(create()).toHaveBeenCalledWith({
        client_id: 42,
        interaction_type: type,
        channel,
        summary: label,
        team_member: "staff@example.test",
      });
      unmount();
    }
  });

  it("the note is optional, and when given it becomes the summary (Enter submits)", async () => {
    setup();
    open();
    expect(screen.getByTestId("log-contact-submit")).toBeDisabled();
    fireEvent.click(screen.getByRole("radio", { name: "Email" }));
    fireEvent.change(screen.getByLabelText("Note (optional)"), {
      target: { value: "  Sent the revised quote  " },
    });
    fireEvent.keyDown(screen.getByLabelText("Note (optional)"), {
      key: "Enter",
    });
    await waitFor(() => expect(create()).toHaveBeenCalledTimes(1));
    expect(create().mock.calls[0][0]).toMatchObject({
      interaction_type: "email",
      summary: "Sent the revised quote",
    });
  });

  it("on success it refreshes the page data once, closes, and resets for next time", async () => {
    const onLogged = setup();
    open();
    fireEvent.click(screen.getByRole("radio", { name: "Call" }));
    fireEvent.click(screen.getByTestId("log-contact-submit"));
    await waitFor(() => expect(onLogged).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("dialog")).toBeNull();
    open();
    expect(screen.getByRole("radio", { name: "Call" })).not.toBeChecked();
  });

  it("GUILT: on error it says what failed, stays open, keeps the choice and does not refresh", async () => {
    create().mockRejectedValue(new Error("Forbidden"));
    const onLogged = setup();
    open();
    fireEvent.click(screen.getByRole("radio", { name: "Meeting" }));
    fireEvent.click(screen.getByTestId("log-contact-submit"));
    const alert = await screen.findByTestId("log-contact-error");
    expect(alert.textContent).toContain("Could not log the contact: Forbidden");
    expect(alert.textContent).toContain("Nothing was saved");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Meeting" })).toBeChecked();
    expect(onLogged).not.toHaveBeenCalled();
  });

  it("Escape closes the popover and returns focus to the trigger", () => {
    setup();
    const panel = open();
    fireEvent.keyDown(panel, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(
      screen.getByTestId("log-contact-trigger"),
    );
  });

  it("INNOCENCE: with no known viewer the control is disabled and nothing can be posted", () => {
    render(<LogContact clientId={42} teamMember={null} onLogged={vi.fn()} />);
    expect(screen.getByTestId("log-contact-trigger")).toBeDisabled();
    expect(create()).not.toHaveBeenCalled();
  });

  it("the trigger is an outlined 32px button named Log contact with an icon, not indented text", () => {
    setup();
    const b = screen.getByRole("button", { name: "Log contact" });
    expect(b.className).toContain("min-h-8");
    expect(b.className).toContain("border");
    expect(b.className).not.toContain("ml-");
    expect(b.querySelector("svg")).not.toBeNull();
  });

  it("GUILT: the panel is in the card's flow, full width, presets in a 2x2 grid and the note input w-full, with no absolute positioning", () => {
    setup();
    const panel = open();
    expect(panel.className).toContain("w-full");
    expect(panel.className).not.toContain("absolute");
    const grid = screen.getByRole("radio", { name: "Call" }).closest(".grid")!;
    expect(grid.className).toContain("grid-cols-2");
    const note = screen.getByLabelText("Note (optional)");
    expect(note.className).toContain("w-full");
    expect(note.className).toContain("min-w-0");
  });
});
