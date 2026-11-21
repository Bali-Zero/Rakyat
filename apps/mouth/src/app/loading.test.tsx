import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import Loading from "./loading";

describe("Loading (root route boundary)", () => {
  it("renders no visible text, so streamed public HTML carries no placeholder copy", () => {
    const { container } = render(<Loading />);
    expect(container.textContent).toBe("");
    expect(container.innerHTML).not.toMatch(/Loading Zantara/);
  });

  it("still announces loading to assistive tech", () => {
    render(<Loading />);
    expect(screen.getByRole("status")).toHaveAccessibleName("Loading");
  });

  it("should render loader icon", () => {
    const { container } = render(<Loading />);
    expect(container.querySelector(".animate-spin")).toBeInTheDocument();
  });

  it("should have correct structure", () => {
    const { container } = render(<Loading />);
    expect(container.firstChild as HTMLElement).toHaveClass(
      "flex",
      "h-screen",
      "w-full",
      "items-center",
      "justify-center",
    );
  });
});
