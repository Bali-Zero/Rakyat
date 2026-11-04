import type { Metadata } from "next";
import type { ReactNode } from "react";

// Tab title "Taxes | Bali Zero" (template in app/portal/layout.tsx).
export const metadata: Metadata = { title: "Taxes" };

export default function PortalTaxesLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  return children;
}
