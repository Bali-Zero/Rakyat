import { render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { VaultCard } from "./VaultCard";
import { api } from "@/lib/api";

vi.mock("@/lib/api", () => ({
  api: {
    crm: {
      getPortalStatus: vi.fn(),
      sendPortalInvite: vi.fn(),
      getPortalMessages: vi.fn(),
      markPortalMessageRead: vi.fn().mockResolvedValue(undefined),
      sendPortalMessage: vi.fn(),
    },
  },
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const renderVault = (email: string | null = "sample.client@example.test") =>
  render(
    <QueryClientProvider client={new QueryClient()}>
      <VaultCard clientId={42} clientName="Sample Client" clientEmail={email} />
    </QueryClientProvider>,
  );

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.crm.getPortalMessages).mockResolvedValue({
    messages: [],
  } as never);
});

describe("VaultCard — my.balizero.com", () => {
  it("brands the card and carries a decorative, lazy illustration", async () => {
    vi.mocked(api.crm.getPortalStatus).mockResolvedValue({
      has_portal_access: false,
      pending_invite: false,
    } as never);
    renderVault();
    expect(screen.getByTestId("vault-wordmark").textContent).toBe(
      "my.balizero.com",
    );
    const art = screen.getByTestId("card-art-vault-rings");
    expect(art.getAttribute("src")).toBe(
      "/images/kita/client-card/vault-rings.webp",
    );
    // the one image that carries the brand name
    expect(art.getAttribute("alt")).toBe("Bali Zero");
    expect(art.getAttribute("loading")).toBe("lazy");
    await screen.findByText("No portal access yet");
  });

  it("is a dark branded card: rail tokens, copper OUTLINE, the Bali Zero cube", async () => {
    vi.mocked(api.crm.getPortalStatus).mockResolvedValue({
      has_portal_access: false,
      pending_invite: false,
    } as never);
    renderVault();
    const card = screen.getByTestId("vault-card");
    expect(card.className).toContain(
      "bg-[var(--bz-kita-surface-navy,var(--nav-rail-bg))]",
    );
    expect(card.className).toContain("text-[var(--nav-fg)]");
    expect(card.className).toContain("border-[var(--nav-rule)]");
    // copper is an outline here, never a fill
    expect(card.className).not.toMatch(/bg-\[var\(--(bz-copper|nav-rule)/);
    expect(card.className).not.toMatch(/(red|rose|orange|amber)-/);
    expect(screen.getByTestId("card-art-vault-rings").getAttribute("src")).toBe(
      "/images/kita/client-card/vault-rings.webp",
    );
    expect(
      screen.getByText(/private vault: documents, permits and messages/),
    ).toBeInTheDocument();
    await screen.findByText("No portal access yet");
  });

  it("mobile: 2-column header, fluid title, promise full width below; desktop unchanged", async () => {
    vi.mocked(api.crm.getPortalStatus).mockResolvedValue({
      has_portal_access: false,
      pending_invite: false,
    } as never);
    renderVault();
    const header = screen.getByTestId("vault-card-header");
    expect(header.className).toContain("grid-cols-[minmax(0,1fr)_120px]");
    expect(header.className).toContain("md:grid-cols-[minmax(0,1fr)_200px]");
    expect(header.className).toContain("md:h-[176px]");
    const title = screen.getByTestId("vault-wordmark");
    expect(title.className).toContain("text-[clamp(22px,6.5vw,32px)]");
    expect(title.className).toContain("md:text-[34px]");
    const art = screen.getByTestId("card-art-vault-rings");
    expect(art.className).toContain("w-[120px]");
    expect(art.className).toContain("md:w-[200px]");
    // bleeds past the card edge (the card clips it), keeps the brand alt
    expect(art.className).toContain("md:-mr-6");
    expect(art.getAttribute("alt")).toBe("Bali Zero");
    expect(screen.getByTestId("vault-card").className).toContain(
      "overflow-hidden",
    );
    // the title and the cube are separate grid cells, never overlapping
    expect(title.parentElement!.contains(art)).toBe(false);
    expect(screen.getByTestId("vault-promise").className).toContain(
      "col-span-2",
    );
    await screen.findByText("No portal access yet");
  });

  it("keeps the illustration in the header zone, above the columns", async () => {
    vi.mocked(api.crm.getPortalStatus).mockResolvedValue({
      has_portal_access: false,
      pending_invite: false,
    } as never);
    renderVault();
    const header = screen.getByTestId("vault-card-header");
    const art = screen.getByTestId("card-art-vault-rings");
    const access = screen.getByTestId("portal-access");
    expect(header.contains(art)).toBe(true);
    expect(art.className).not.toContain("absolute");
    expect(header.contains(access)).toBe(false);
    expect(
      header.compareDocumentPosition(access) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    await screen.findByText("No portal access yet");
  });

  it("shows the invite state: not invited -> an Invite button that sends", async () => {
    vi.mocked(api.crm.getPortalStatus).mockResolvedValue({
      has_portal_access: false,
      pending_invite: false,
    } as never);
    vi.mocked(api.crm.sendPortalInvite).mockResolvedValue({
      emailSent: true,
    } as never);
    renderVault();
    const button = await screen.findByRole("button", {
      name: /Invite to portal/,
    });
    button.click();
    await waitFor(() =>
      expect(api.crm.sendPortalInvite).toHaveBeenCalledWith(
        42,
        "sample.client@example.test",
      ),
    );
  });

  it("shows a pending invite with Send again", async () => {
    vi.mocked(api.crm.getPortalStatus).mockResolvedValue({
      has_portal_access: false,
      pending_invite: true,
      invite_expires_at: "2026-12-01T00:00:00Z",
    } as never);
    renderVault();
    expect(await screen.findByText("Invitation pending")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Send again/ })).toBeEnabled();
  });

  it("shows an active portal with the last sign-in, and never-signed-in in words", async () => {
    vi.mocked(api.crm.getPortalStatus).mockResolvedValue({
      has_portal_access: true,
      portal_email: "sample.client@example.test",
      last_login: null,
    } as never);
    renderVault();
    expect(await screen.findByText(/Portal active/)).toBeInTheDocument();
    expect(
      screen.getByText("Registered, but has never signed in yet"),
    ).toBeInTheDocument();
  });

  it("shows the unread count and the Open messages action", async () => {
    vi.mocked(api.crm.getPortalStatus).mockResolvedValue({
      has_portal_access: true,
      portal_email: "sample.client@example.test",
      last_login: "2026-11-01T02:00:00Z",
    } as never);
    vi.mocked(api.crm.getPortalMessages).mockResolvedValue({
      messages: [
        {
          id: 1,
          direction: "client_to_team",
          content: "Hello",
          created_at: "2026-11-02T00:00:00Z",
          read_at: null,
        },
      ],
    } as never);
    // The thread marks messages read once visible; keep the badge by hiding.
    Object.defineProperty(document, "visibilityState", {
      value: "hidden",
      configurable: true,
    });
    try {
      renderVault();
      const badge = await screen.findByTestId("portal-unread");
      expect(badge.textContent).toBe("1");
      expect(badge.getAttribute("aria-label")).toBe("1 unread");
      expect(
        within(screen.getByTestId("vault-card")).getByRole("button", {
          name: "Open messages",
        }),
      ).toBeEnabled();
    } finally {
      Object.defineProperty(document, "visibilityState", {
        value: "visible",
        configurable: true,
      });
    }
  });
});
