import type { Metadata } from "next";
import type { ReactNode } from "react";

// Tab title "Profile | Bali Zero" (template in app/portal/layout.tsx).
export const metadata: Metadata = { title: "Profile" };

export default function PortalProfileLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  return children;
}
