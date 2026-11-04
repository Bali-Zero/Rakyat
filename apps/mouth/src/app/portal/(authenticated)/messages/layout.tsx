import type { Metadata } from "next";
import type { ReactNode } from "react";

// Tab title "Messages | Bali Zero" (template in app/portal/layout.tsx).
export const metadata: Metadata = { title: "Messages" };

export default function PortalMessagesLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  return children;
}
