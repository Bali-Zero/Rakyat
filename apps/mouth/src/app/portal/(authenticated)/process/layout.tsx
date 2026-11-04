import type { Metadata } from "next";
import type { ReactNode } from "react";

// Tab title "Process | Bali Zero" (template in app/portal/layout.tsx).
export const metadata: Metadata = { title: "Process" };

export default function PortalProcessLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  return children;
}
