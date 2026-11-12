import { render, screen, within } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { TeamReelMonthlyHub } from "../TeamReelMonthlyHub";
import { PortalChampionsLastMonthPodium } from "../PortalChampionsLastMonthPodium";

vi.mock("next/link", () => ({
  default: ({
    children,
    href,
    ...rest
  }: {
    children: React.ReactNode;
    href: string;
    [key: string]: unknown;
  }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

vi.mock("../_lib/usePortalChallenge", () => ({
  usePortalChallenge: vi.fn(() => ({
    data: null,
    isLoading: false,
  })),
}));

describe("TeamReelMonthlyHub & PortalChampionsLastMonthPodium", () => {
  it("renders the team reel with human non-technical copy and the 3 monthly highlight cards", () => {
    render(<TeamReelMonthlyHub />);

    const video = screen.getByTestId(
      "continuous-team-reel-video",
    ) as HTMLVideoElement;
    expect(video).toBeInTheDocument();
    expect(video.getAttribute("src")).toBe(
      "/static/team/bali-zero-team-reel-33s.mp4",
    );
    expect(video.loop).toBe(true);
    expect(video.autoplay).toBe(true);

    // Ensure no technical jargon (H5, Continuous Loop, (/hr/me)) is rendered
    const heroText = screen.getByTestId("kita-team-reel-hero").textContent ?? "";
    expect(heroText).not.toMatch(/\bH5\b/i);
    expect(heroText).not.toMatch(/continuous loop/i);
    expect(heroText).not.toContain("(/hr/me)");

    // 1) New Photos & WhatsApp / Google / Zoho card
    expect(screen.getByTestId("monthly-box-brand-kit")).toBeInTheDocument();
    expect(
      screen.getByText(/Put Your New Photo on WhatsApp, Google & Zoho/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /Department → Team → Your Name → Material Marketing/i,
      ),
    ).toBeInTheDocument();

    // 2) New Invoices card
    const invoicesBox = screen.getByTestId("monthly-box-invoices");
    expect(invoicesBox).toBeInTheDocument();
    expect(invoicesBox.getAttribute("href")).toBe("/clients");

    // 3) Personal HR Space card -> clicks straight into /hr/me
    const hrBox = screen.getByTestId("monthly-box-hr-portal");
    expect(hrBox).toBeInTheDocument();
    expect(hrBox.getAttribute("href")).toBe("/hr/me");
  });

  it("renders the original blue ChampionArena podium with official October Round 2 winners (Damar #1, Krishna #2, Adit #3) as last month's news", () => {
    render(<PortalChampionsLastMonthPodium identity="preview" />);

    const podiumSection = screen.getByTestId(
      "portal-champions-last-month-podium",
    );
    expect(podiumSection).toBeInTheDocument();

    const arena = within(podiumSection).getByTestId("champion-arena");
    expect(arena).toBeInTheDocument();
    expect(within(arena).getByText("Portal Champion")).toBeInTheDocument();
    expect(within(arena).getByText("353")).toBeInTheDocument();
    expect(
      within(arena).getByRole("button", { name: "Lihat peluang Damar" }),
    ).toBeInTheDocument();
    expect(
      within(arena).getByRole("button", { name: "Lihat peluang Krishna" }),
    ).toBeInTheDocument();
    expect(
      within(arena).getByRole("button", { name: "Lihat peluang Adit" }),
    ).toBeInTheDocument();
  });
});
