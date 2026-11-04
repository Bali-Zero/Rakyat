import type { Metadata } from "next";
import type { ReactNode } from "react";

// Tab title "LKPM | Bali Zero" (template in app/portal/layout.tsx).
export const metadata: Metadata = { title: "LKPM" };

export default function PortalLkpmLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  return children;
}
