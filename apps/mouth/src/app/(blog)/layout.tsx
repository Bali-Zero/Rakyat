import type { ReactNode } from "react";
import { R19Presentation } from "@/components/r19/R19Presentation";
import { BlogNav } from "@/app/(blog)/_components/BlogNav";
import { ZantaraFAB } from "@/components/site/ZantaraFAB";
import { Footer } from "@/components/site/Footer";
import { I18nProvider } from "@/i18n";

export default function BlogLayout({ children }: { children: ReactNode }) {
  return (
    <I18nProvider>
      <R19Presentation>
        <div
          className="min-h-screen flex flex-col"
          style={{
            background: "var(--surface-base)",
            color: "var(--text-primary)",
          }}
        >
          <a className="bz-skip-link" href="#main-content">
            Skip to main content
          </a>
          <BlogNav />
          <main
            id="main-content"
            tabIndex={-1}
            className="flex-1"
            style={{ paddingTop: "var(--public-header-height, 56px)" }}
          >
            {children}
          </main>
          <Footer />
          <ZantaraFAB />
        </div>
      </R19Presentation>
    </I18nProvider>
  );
}
