import type { Metadata } from "next";
import type { ReactNode } from "react";

// Tab title "Settings | Bali Zero" (template in app/portal/layout.tsx).
export const metadata: Metadata = { title: "Settings" };

export default function PortalSettingsLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  return children;
}
