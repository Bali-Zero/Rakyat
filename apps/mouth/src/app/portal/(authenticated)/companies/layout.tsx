import type { Metadata } from "next";
import type { ReactNode } from "react";

// Tab title "Companies | Bali Zero" (template in app/portal/layout.tsx).
export const metadata: Metadata = { title: "Companies" };

export default function PortalCompaniesLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  return children;
}
