import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Bali Visa Agency 2026 | KITAS, PT PMA, Golden Visa Services",
  description:
    "Complete visa and business services in Bali. KITAS, PT PMA, Golden Visa, tax compliance.",
  alternates: {
    canonical: "https://balizero.com/services",
  },
  openGraph: {
    type: "website",
    siteName: "Bali Zero",
    title: "Bali Zero Services | Visa, PT PMA, Tax & Property",
    description:
      "Complete visa and business services in Bali. KITAS, PT PMA, Golden Visa, tax compliance.",
    url: "https://balizero.com/services",
    images: [
      {
        url: "/static/og-image.jpg",
        width: 1024,
        height: 1024,
        alt: "Bali Zero Services | Visa, PT PMA, Tax & Property",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    site: "@balizero",
    creator: "@balizero",
    title: "Bali Zero Services | Visa, PT PMA, Tax & Property",
    description:
      "Complete visa and business services in Bali. KITAS, PT PMA, Golden Visa, tax compliance.",
    images: ["/static/og-image.jpg"],
  },
};

export default function ServicesLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return children;
}
