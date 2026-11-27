import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";
import { LanguageToggle } from "./LanguageToggle";

describe("LanguageToggle", () => {
  it("updates document language immediately, keeps compact labels, and restores it on unmount", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    document.documentElement.lang = "fr";
    const view = render(<LanguageToggle language="en" onChange={onChange} />);

    expect(document.documentElement.lang).toBe("en");
    expect(screen.getByText("EN")).toBeInTheDocument();
    expect(screen.getByText("ID")).toBeInTheDocument();

    const replaceState = vi
      .spyOn(window.history, "replaceState")
      .mockImplementation(() => {});
    const store: Record<string, string> = {};
    vi.stubGlobal("sessionStorage", {
      getItem: (key: string) => store[key] ?? null,
      setItem: (key: string, value: string) => {
        store[key] = value;
      },
    });
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        pathname: "/visa-oracle",
        search: "",
        hash: "",
      },
    });

    await user.click(screen.getByRole("button", { name: /bahasa indonesia/i }));
    expect(document.documentElement.lang).toBe("id");
    expect(onChange).toHaveBeenCalledWith("id");
    expect(replaceState).toHaveBeenCalledWith(null, "", "/visa-oracle?lang=id");
    replaceState.mockRestore();
    vi.unstubAllGlobals();

    view.rerender(<LanguageToggle language="id" onChange={onChange} />);
    expect(document.documentElement.lang).toBe("id");
    view.unmount();
    expect(document.documentElement.lang).toBe("fr");
  });
});
