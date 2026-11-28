import type { ReactNode } from "react";

// Keep the reviewers' page out of public prerendered route payloads.
export const dynamic = "force-dynamic";

export default function DocumentRequestReviewLayout({
  children,
}: {
  children: ReactNode;
}) {
  return children;
}
