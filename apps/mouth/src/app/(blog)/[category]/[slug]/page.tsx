import type { Metadata } from "next";
import { cache, type ReactNode } from "react";
import { notFound, permanentRedirect } from "next/navigation";
import {
  getArticleBySlug,
  getArticleByLocale,
  getIndexableArticleLocales,
  resolveCategoryAlias,
} from "@/lib/blog/articles";
import { resolveContentLocale } from "@/i18n/content-locale";
import { LOCALES, LOCALE_NAMES } from "@/i18n/types";
import { articleLanguageUrl } from "@/lib/blog/article-language-urls";
import { ContentLangSync } from "@/i18n/ContentLangSync";
import { generateArticleMetadata } from "@/lib/blog/metadata";
import { CATEGORY_SEO } from "@/lib/blog/category-seo";
import { stripImports } from "@/lib/blog/strip-imports";
import { renderMDXBody } from "@/components/blog/MDXContentRSC";
import {
  ArticleClient,
  type SerializedArticleForClient,
} from "./ArticleClient";
import {
  ArticleWithFAQJsonLd,
  EnhancedArticleJsonLd,
  BreadcrumbJsonLd,
} from "@/components/seo";
import { logger } from "@/lib/logger";

interface PageProps {
  params: Promise<{
    category: string;
    slug: string;
  }>;
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}

// Metadata and the rendered page resolve the same article within a request.
const readArticle = cache((category: string, slug: string, lang?: string) =>
  lang
    ? getArticleByLocale(category, slug, lang)
    : getArticleBySlug(category, slug),
);
const indexableLocales = cache(getIndexableArticleLocales);

// Static file paths that should NOT be handled by this route
// 'images' included for backward compatibility (old URLs should 404, not render broken page)
const STATIC_PATHS = [
  "static",
  "assets",
  "fonts",
  "_next",
  "api",
  "images",
  "avatars",
  "blueprints",
  "videos",
  "admin",
  "dashboard",
  "login",
  "chat",
  "clients",
  "settings",
];

// The parent layout and article metadata share one raw-category boundary.
const VALID_CATEGORIES = Object.keys(CATEGORY_SEO);

/**
 * Check if this request is for a static file (should return 404 to let Next.js serve static)
 */
function isStaticFilePath(category: string, slug: string): boolean {
  // If category is a known static path, reject
  if (STATIC_PATHS.includes(category)) return true;
  // If slug has a file extension, reject
  if (slug.includes(".")) return true;
  return false;
}

/**
 * Generate dynamic metadata for SEO
 * This runs on the server and provides metadata to Google, social media, and AI crawlers
 */
export async function generateMetadata({
  params,
  searchParams,
}: PageProps): Promise<Metadata> {
  const { category, slug } = await params;
  const sp = await searchParams;
  const lang = typeof sp.lang === "string" ? sp.lang : undefined;

  // Reject static file paths - let Next.js serve them from /public
  if (isStaticFilePath(category, slug)) {
    return { title: "Not Found" };
  }

  // Aliases are not routable in the parent layout, even when article lookup
  // could resolve them. Reject the raw segment before either lookup.
  if (!VALID_CATEGORIES.includes(category)) {
    return {
      title: "Page not found",
      robots: { index: false, follow: false },
    };
  }

  try {
    const article = await readArticle(category, slug, lang);

    if (article) {
      return generateArticleMetadata(
        article,
        resolveContentLocale(lang, [article.locale]),
        article.noIndex ? [] : indexableLocales(category, slug),
      );
    }
  } catch (err) {
    logger.error(
      "Error generating metadata:",
      {},
      err instanceof Error ? err : new Error(String(err)),
    );

    // Genuine lookup error (distinct from "article missing"): keep the page
    // renderable but never indexable under invented metadata.
    return {
      title: "Bali Zero",
      robots: { index: false, follow: false },
    };
  }

  // Article does not exist: render the 404 boundary. Title-casing the slug
  // here used to fabricate a phantom 200 page for ANY url (soft-404, D12).
  notFound();
}

// Category labels for breadcrumbs
const CATEGORY_LABELS: Record<string, string> = {
  visas: "Immigration",
  business: "Business",
  taxes: "Tax & Legal",
  property: "Property",
  living: "Lifestyle",
  "digital-nomad": "Digital Nomad",
  trends: "Tech",
};

/**
 * Article page - Server component that renders the client component
 * Serializes MDX server-side and passes it as a prop for SSR (Google sees full content)
 */
export default async function ArticlePage({ params, searchParams }: PageProps) {
  const { category, slug } = await params;
  const sp = await searchParams;
  const lang = typeof sp.lang === "string" ? sp.lang : undefined;

  // Reject static file paths - return 404 so Next.js can serve from /public
  if (isStaticFilePath(category, slug)) {
    notFound();
  }

  // Fetch article (locale-aware) for JSON-LD injection AND SSR content
  const article = await readArticle(category, slug, lang);

  // No article in backend NOR local MDX: hard 404 (D12 soft-404 fix).
  // getArticleBySlug/getArticleByLocale return null both for "not found";
  // backend outages degrade to the MDX-on-disk fallback before reaching here.
  if (!article) {
    notFound();
  }

  // Category-alias canonicalization: the same article is reachable under
  // alias categories (e.g. /immigration/X and /visas/X). When the requested
  // category is a KNOWN alias of the article's canonical category, issue a
  // 308 permanent redirect to the canonical URL instead of serving duplicate
  // content that relies on the canonical <meta> alone. Unknown categories
  // keep current behavior (notFound per D12 above).
  // NOTE: permanentRedirect throws NEXT_REDIRECT — must stay outside try/catch.
  const canonical = resolveCategoryAlias(category);
  if (canonical && canonical !== category && article.category === canonical) {
    const langSuffix = lang ? `?lang=${encodeURIComponent(lang)}` : "";
    permanentRedirect(`/${canonical}/${slug}${langSuffix}`);
  }

  const contentLocale = resolveContentLocale(lang, [article.locale]);
  const articleUrl = articleLanguageUrl(category, slug, contentLocale);
  const locales = article.noIndex ? [] : indexableLocales(category, slug);

  const breadcrumbItems = [
    { name: "Home", url: "/" },
    { name: CATEGORY_LABELS[category] || category, url: `/${category}` },
    { name: article.title, url: articleUrl },
  ];

  const articleProps = article
    ? {
        title: article.title,
        description: article.seoDescription || article.excerpt,
        canonicalUrl: articleUrl,
        locale: contentLocale,
        slug: article.slug,
        category: article.category,
        publishedAt: (article.publishedAt || article.createdAt).toISOString(),
        updatedAt: article.updatedAt.toISOString(),
        author: article.author,
        image: article.coverImage,
        tags: article.tags,
        readingTime: article.readingTime,
        answerSnippet: article.aiOptimization?.answerSnippet,
        entityMentions: article.aiOptimization?.entityMentions,
      }
    : null;

  // Render the MDX body SERVER-SIDE for SSR (Google + the e2e see the full
  // article in the initial HTML). React-19 fix: we render via the RSC MDXRemote
  // (a server component) instead of serializing for the client <MDXRemote>,
  // which is hook-based and throws `Cannot read properties of null (reading
  // 'useState')` during SSR under React 19 — that uncaught throw used to unwind
  // the whole article subtree, dropping even the .rumah-putih page chrome from
  // the SSR HTML. See MDXContentRSC for the full rationale.
  const initialArticle: SerializedArticleForClient = {
    ...article,
    createdAt: article.createdAt.toISOString(),
    updatedAt: article.updatedAt.toISOString(),
    publishedAt: article.publishedAt?.toISOString() ?? null,
  };
  let mdxBody: ReactNode = null;
  try {
    mdxBody = await renderMDXBody(stripImports(article.content));
  } catch (err) {
    // The markdown fallback must receive the same localized article as the head.
    logger.error(
      "Article MDX rendering failed, using markdown",
      {},
      err instanceof Error ? err : new Error(String(err)),
    );
  }

  return (
    <>
      {/* Declares the language this page's BODY was served in, and holds
          `<html lang>` against the I18nProvider, whose locale is the UI
          chrome's and can legitimately differ. See i18n/content-locale.ts. */}
      <ContentLangSync locale={contentLocale} />
      <BreadcrumbJsonLd items={breadcrumbItems} />
      {articleProps && article?.faq?.length ? (
        <ArticleWithFAQJsonLd {...articleProps} faq={article.faq} />
      ) : articleProps ? (
        <EnhancedArticleJsonLd {...articleProps} />
      ) : null}
      <ArticleClient
        key={articleUrl}
        languageLinks={
          locales.length > 1 && (
            <nav
              aria-label="Article languages"
              className="flex flex-wrap gap-x-4 gap-y-2 mb-4 text-sm"
            >
              {LOCALES.filter((locale) => locales.includes(locale)).map(
                (locale) => (
                  <a
                    key={locale}
                    href={articleLanguageUrl(category, slug, locale)}
                    hrefLang={locale}
                    lang={locale}
                    aria-current={locale === contentLocale ? "page" : undefined}
                    className="underline underline-offset-4"
                  >
                    {LOCALE_NAMES[locale]}
                  </a>
                ),
              )}
            </nav>
          )
        }
        category={category}
        slug={slug}
        initialArticle={initialArticle}
        mdxBody={mdxBody}
      />
    </>
  );
}
