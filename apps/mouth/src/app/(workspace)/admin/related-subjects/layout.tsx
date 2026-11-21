import type { ReactNode } from "react";

// Keep the authenticated registry out of public prerendered route payloads.
export const dynamic = "force-dynamic";

export default function RelatedRegistryLayout({
  children,
}: {
  children: ReactNode;
}) {
  return children;
}
