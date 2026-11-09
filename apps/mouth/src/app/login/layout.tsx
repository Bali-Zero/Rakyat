import type { Metadata } from "next";

/**
 * /login is only served on kita.balizero.com (the public host redirects it
 * there) and it is the one kita page an anonymous visitor reaches: every
 * other kita route redirects to it without a session.
 *
 * Without this, it inherits the root layout's `canonical: balizero.com` and
 * hreflang, which tell a crawler the kita login page IS the public homepage.
 * The root layout cannot read the host without making every page dynamic
 * (the KBLI and article pages are statically generated), so the fix sits on
 * the kita route itself. The proxy's X-Robots-Tag header stays as it is.
 */
export const metadata: Metadata = {
  robots: { index: false, follow: false },
  alternates: { canonical: null, languages: {} },
};

export default function LoginLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return children;
}
