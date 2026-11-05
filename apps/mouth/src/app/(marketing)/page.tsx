import type { Metadata } from "next";
import { Suspense } from "react";
import { preload } from "react-dom";
import { SessionInit } from "@/components/funnel/SessionInit";
import { R19HomeProvider } from "@/components/r19/R19Presentation";
import { R19_VARS } from "@/components/r19/presentation";
import { SiteHeader, Hero } from "@/components/r19/home/Entry";
import { Services } from "@/components/r19/home/Services";
import { Reviews } from "@/components/r19/home/Reviews";
import { Evoa } from "@/components/r19/home/Evoa";
import { SecondHome } from "@/components/r19/home/SecondHome";
import { Portal } from "@/components/r19/home/Portal";
import { HomeJournal, JournalPending } from "@/components/r19/home/HomeJournal";
import { Team } from "@/components/r19/home/Team";
import { Contact } from "@/components/r19/home/Contact";
import { Footer } from "@/components/r19/home/Footer";
import { ZantaraFAB } from "../v2/_components/ZantaraFAB";
import "@/components/r19/home/home.css";

// Incremental Static Regeneration, not per-request rendering. The journal
// data already comes through unstable_cache(revalidate: 60) in
// lib/blog/articles.ts, so a 60 s page revalidation loses no editorial
// freshness, while the CDN and the browser back-forward cache can keep the
// page (web audit panel, 2026-11-01).
export const revalidate = 60;

export const metadata: Metadata = {
  title: {
    absolute: "Bali Zero | #1 Visa & PT PMA Experts in Bali, Indonesia",
  },
  description:
    "Indonesia's AI-powered visa agency. KITAS, KITAP, Golden Visa, PT PMA company setup, tax compliance. 24/7 AI assistant. Trusted by 5000+ clients since 2020.",
  alternates: {
    canonical: "https://balizero.com",
  },
  openGraph: {
    title: "Bali Zero | #1 Visa & PT PMA Experts in Bali, Indonesia",
    description:
      "Indonesia's AI-powered visa agency. KITAS, KITAP, Golden Visa, PT PMA company setup, tax compliance. 24/7 AI assistant. Trusted by 5000+ clients.",
    url: "https://balizero.com",
  },
};

// The R19 faces are declared in home.css, which the browser only parses after
// the HTML; without these hints it first downloads the root layout's Inter and
// Cormorant (unused here) and discovers Manrope/Fraunces late. Entity-checked
// against home.css by hero-and-fonts.test.ts.
const R19_HOME_FONTS = [
  "/fonts/manrope-r19-home.woff2",
  "/fonts/fraunces-r19-home.woff2",
];

export default function HomePage() {
  for (const href of R19_HOME_FONTS) {
    preload(href, { as: "font", type: "font/woff2", crossOrigin: "anonymous" });
  }
  return (
    <R19HomeProvider>
      <div
        id="top"
        data-presentation="r19"
        style={
          {
            ...R19_VARS,
            "--font-sans": '"R19 Home Manrope", Arial, sans-serif',
            "--font-serif": '"R19 Home Fraunces", Georgia, serif',
          } as React.CSSProperties
        }
      >
        <SessionInit funnel="home" />
        <div data-r19-home>
          <SiteHeader />
          <main id="main-content" tabIndex={-1}>
            <Hero />
            <Services />
            <Reviews />
            <Evoa />
            <SecondHome />
            <Portal />
            <Suspense fallback={<JournalPending />}>
              <HomeJournal />
            </Suspense>
            <Team />
            <Contact />
          </main>
          <Footer />
        </div>
        <ZantaraFAB />
      </div>
    </R19HomeProvider>
  );
}
