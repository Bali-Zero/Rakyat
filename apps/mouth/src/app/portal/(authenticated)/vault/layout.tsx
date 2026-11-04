import type { Metadata } from "next";
import type { ReactNode } from "react";

// Tab title "Vault | Bali Zero" (template in app/portal/layout.tsx).
export const metadata: Metadata = { title: "Vault" };

export default function PortalVaultLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  return children;
}
