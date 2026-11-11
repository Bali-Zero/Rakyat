"use client";

import { usePathname } from "next/navigation";
import { Footer } from "@/app/v2/_components/Footer";

// The visa segment also hosts self-contained product screens that bring their own
// theme and must not offer marketing exits mid-flow: the GARUDA VOA checkout,
// upload and order screens (light theme, lead capture per order) and the second
// home studio app. Everything else under /visa gets the site footer.
const NO_FOOTER_PREFIXES = ["/visa/voa", "/visa/second-home/studio"];

export function isVisaFooterHidden(pathname: string): boolean {
  return NO_FOOTER_PREFIXES.some(
    (p) => pathname === p || pathname.startsWith(`${p}/`),
  );
}

export function VisaFooter() {
  const pathname = usePathname();
  if (isVisaFooterHidden(pathname)) return null;
  return <Footer funnel="visa" />;
}
