import type { Metadata } from "next";
import type { ReactNode } from "react";

// Tab title "Family | Bali Zero" (template in app/portal/layout.tsx).
export const metadata: Metadata = { title: "Family" };

export default function PortalFamilyLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  return children;
}
