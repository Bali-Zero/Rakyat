import "@/styles/kbli-r19-wrapper.css";
import { NavShell, BZLogo } from "@balizero/core";
import { SessionInit } from "@/components/funnel/SessionInit";
import { KBLIHeaderCTA } from "./_components/KBLIHeaderCTA";
import { getFunnelNavItems } from "@/components/funnel/funnel-nav";
import { MobileNav } from "@/app/v2/_components/MobileNav";
import { Footer } from "@/app/v2/_components/Footer";

export default function KBLILayout({
  children,
  panel,
}: {
  children: React.ReactNode;
  panel: React.ReactNode;
}) {
  const navItems = getFunnelNavItems("kbli");

  return (
    // R19 Direction A: the wrapper defines every token the pages read
    // (styles/kbli-r19-wrapper.css, docs/specs/2026-10-08-kbli-r19-wrapper-token-contract.md).
    <div
      data-presentation="r19"
      className="kbli-r19 relative min-h-screen bg-[var(--background)] text-[var(--foreground)] font-sans"
    >
      {/* Above the fixed NavShell (300), below its mobile drawer (398). */}
      <a
        className="btn-primary absolute left-1 top-1 z-[301] -translate-y-[150%] focus:translate-y-0"
        href="#main-content"
      >
        Skip to main content
      </a>
      <NavShell
        logo={<BZLogo variant="full" />}
        items={navItems}
        slotAfter={<MobileNav items={navItems} funnel="kbli" paper />}
        actions={<KBLIHeaderCTA />}
      />
      <SessionInit funnel="kbli" />
      <div
        id="main-content"
        tabIndex={-1}
        className="mx-auto max-w-6xl px-4 pt-14 pb-8 sm:px-6 lg:px-8"
      >
        {children}
      </div>
      {panel}
      <Footer />
    </div>
  );
}
