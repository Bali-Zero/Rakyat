"use client";

import React, { useEffect, useMemo, useState } from "react";
import {
  ArrowUpRight,
  ChevronLeft,
  ChevronRight,
  Clock,
  Newspaper,
} from "lucide-react";
import { SERIF } from "./r19";

export interface DashboardNewsArticle {
  id?: string;
  slug: string;
  title: string;
  subtitle?: string;
  excerpt?: string;
  coverImage?: string;
  cardImage?: string;
  cover_image?: string;
  category: string;
  publishedAt: string;
  readingTime?: number;
}

interface DashboardNewsCarouselProps {
  articles: DashboardNewsArticle[];
  isLoading?: boolean;
}

const CATEGORY_COVER_FALLBACKS: Record<string, string> = {
  visas: "/static/blog/kitas-guide.jpg",
  immigration: "/static/blog/kitas-guide.jpg",
  business: "/static/blog/oss-guide.jpg",
  taxes: "/static/blog/tax-calendar.jpg",
  "tax-legal": "/static/blog/tax-calendar.jpg",
  property: "/static/blog/golden-visa.jpg",
  living: "/static/blog/north-bali.jpg",
  lifestyle: "/static/blog/north-bali.jpg",
  trends: "/static/blog/nomad-comparison.jpg",
};

function resolveArticleImage(article: DashboardNewsArticle): string {
  const candidate =
    article.coverImage || article.cardImage || article.cover_image || "";
  if (
    candidate.startsWith("/") ||
    candidate.startsWith("https://") ||
    candidate.startsWith("http://")
  ) {
    return candidate;
  }
  const key = (article.category || "business").toLowerCase();
  return CATEGORY_COVER_FALLBACKS[key] || "/static/blog/golden-visa.jpg";
}

/**
 * Clean markdown links (including truncated links at the end of a backend summary)
 * and normalize whitespace so the 2-line summary is always clean editorial prose.
 */
export function cleanArticleSummary(raw?: string): string {
  if (!raw) return "";
  return raw
    .replace(/^#{1,6}\s+.*$/gm, "")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*$/g, "$1")
    .replace(/https?:\/\/\S+/g, "")
    .replace(/\*\*(.*?)\*\*/g, "$1")
    .replace(/\*(.*?)\*/g, "$1")
    .replace(/`(.*?)`/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * Derive a clean Title, Subtitle, and 2-line Summary for every article.
 * - If the article has `Title: Subtitle` in `title` and no explicit `subtitle`,
 *   split at the first colon so both Title and Subtitle are crisp and distinct.
 * - If no colon is present and `subtitle` is absent, use an editorial category
 *   deck as the subtitle and keep the full cleaned excerpt for the 2-line summary.
 */
export function deriveArticleEditorialCopy(article: DashboardNewsArticle): {
  headline: string;
  subtitle: string;
  summary: string;
} {
  const rawTitle = (article.title || "Untitled Article").trim();
  const explicitSubtitle = article.subtitle?.trim() || "";
  const cleanedExcerpt = cleanArticleSummary(article.excerpt);
  const categoryLabel = (article.category || "news")
    .replace(/[-_]/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());

  let headline = rawTitle;
  let subtitle = explicitSubtitle;

  if (!subtitle) {
    const colonIdx = rawTitle.indexOf(": ");
    if (colonIdx > 6 && colonIdx < rawTitle.length - 6) {
      headline = rawTitle.slice(0, colonIdx).trim();
      subtitle = rawTitle.slice(colonIdx + 2).trim();
    } else {
      subtitle = `${categoryLabel} Briefing · Bali Zero Newsroom`;
    }
  }

  const summary =
    cleanedExcerpt ||
    `${subtitle}. Read the full analysis and regulatory breakdown on Bali Zero.`;

  return { headline, subtitle, summary };
}

function formatPublishedDate(dateStr: string): string {
  const parsed = new Date(dateStr);
  if (Number.isNaN(parsed.getTime())) return "Recent";
  return parsed.toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
}

export function DashboardNewsCarousel({
  articles,
  isLoading = false,
}: DashboardNewsCarouselProps) {
  const items = useMemo(() => (articles ?? []).slice(0, 6), [articles]);
  const [activeIndex, setActiveIndex] = useState(0);
  const [isPaused, setIsPaused] = useState(false);
  const [failedImages, setFailedImages] = useState<Record<string, boolean>>({});

  useEffect(() => {
    if (activeIndex >= items.length && items.length > 0) {
      setActiveIndex(0);
    }
  }, [activeIndex, items.length]);

  useEffect(() => {
    if (items.length <= 1 || isPaused) return;
    const timer = window.setInterval(() => {
      setActiveIndex((prev) => (prev + 1) % items.length);
    }, 6500);
    return () => window.clearInterval(timer);
  }, [items.length, isPaused]);

  if (isLoading) {
    return (
      <div
        data-testid="dashboard-news-carousel-loading"
        className="mt-4 flex w-full flex-col gap-4"
      >
        <div className="h-[420px] w-full animate-pulse rounded-2xl bg-[var(--bz-card)]" />
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          {[1, 2, 3, 4, 5, 6].map((n) => (
            <div
              key={n}
              className="h-32 animate-pulse rounded-xl bg-[var(--bz-card)]"
            />
          ))}
        </div>
      </div>
    );
  }

  if (items.length === 0) {
    return (
      <div
        data-testid="dashboard-news-carousel-empty"
        className="mt-4 w-full rounded-2xl border border-[var(--bz-border)] bg-[var(--bz-card)] px-6 py-14 text-center text-[13px] text-[var(--bz-text-3)]"
      >
        No recent articles published yet.
      </div>
    );
  }

  const current = items[activeIndex] ?? items[0];
  const { headline, subtitle, summary } = deriveArticleEditorialCopy(current);
  const categoryUpper = (current.category || "news")
    .replace(/[-_]/g, " ")
    .toUpperCase();
  const dateLabel = formatPublishedDate(current.publishedAt);
  const readMinutes =
    current.readingTime && current.readingTime > 0
      ? `${current.readingTime} min read`
      : "3 min read";
  const articleHref = `https://balizero.com/${current.category}/${current.slug}`;
  const fallbackSrc =
    CATEGORY_COVER_FALLBACKS[(current.category || "business").toLowerCase()] ||
    "/static/blog/golden-visa.jpg";
  const currentImgSrc = failedImages[current.slug]
    ? fallbackSrc
    : resolveArticleImage(current);

  const goPrev = () => {
    setActiveIndex((prev) => (prev - 1 + items.length) % items.length);
  };

  const goNext = () => {
    setActiveIndex((prev) => (prev + 1) % items.length);
  };

  return (
    <div
      data-testid="dashboard-news-carousel"
      className="mt-4 flex w-full flex-col gap-4"
      onMouseEnter={() => setIsPaused(true)}
      onMouseLeave={() => setIsPaused(false)}
      onFocusCapture={() => setIsPaused(true)}
      onBlurCapture={() => setIsPaused(false)}
    >
      {/* ── Full-Width Tall Featured Active Article Card ── */}
      <article
        className="group relative w-full overflow-hidden rounded-2xl border border-[var(--bz-border)] bg-[var(--bz-card)] transition-shadow duration-300"
        style={{
          boxShadow: "0 20px 48px -22px rgba(18, 16, 13, 0.28)",
        }}
      >
        <div className="grid grid-cols-1 items-stretch lg:grid-cols-12">
          {/* Large Tall Cover Image (Left 7 cols on desktop) */}
          <a
            href={articleHref}
            target="_blank"
            rel="noopener noreferrer"
            className="relative block aspect-[16/10] w-full overflow-hidden bg-[var(--surface-sunken)] lg:col-span-7 lg:aspect-auto lg:min-h-[460px]"
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={currentImgSrc}
              alt={current.title}
              onError={() =>
                setFailedImages((prev) => ({ ...prev, [current.slug]: true }))
              }
              className="h-full w-full object-cover transition-transform duration-700 ease-out group-hover:scale-[1.03]"
            />
            {/* Subtle bottom vignette for legibility */}
            <div
              aria-hidden="true"
              className="pointer-events-none absolute inset-0"
              style={{
                background:
                  "linear-gradient(180deg, rgba(9, 13, 22, 0.34) 0%, rgba(9, 13, 22, 0.04) 45%, rgba(9, 13, 22, 0.64) 100%)",
              }}
            />

            {/* Top-left Category & Slide Counter Pill */}
            <div className="absolute left-4 top-4 flex flex-wrap items-center gap-2.5">
              <span
                className="inline-flex items-center gap-1.5 rounded-full px-3.5 py-1.5 text-[11px] font-extrabold uppercase tracking-[0.12em]"
                style={{
                  background: "rgba(9, 13, 22, 0.88)",
                  border: "1px solid rgba(247, 244, 238, 0.24)",
                  color: "rgb(247, 244, 238)",
                }}
              >
                <Newspaper className="h-3.5 w-3.5" aria-hidden="true" />
                {categoryUpper}
              </span>
              <span
                className="inline-flex items-center rounded-full px-3 py-1.5 font-mono text-[11px] font-bold tracking-[0.08em]"
                style={{
                  background: "rgba(9, 13, 22, 0.84)",
                  border: "1px solid rgba(247, 244, 238, 0.2)",
                  color: "rgb(247, 244, 238)",
                }}
              >
                {String(activeIndex + 1).padStart(2, "0")} /{" "}
                {String(items.length).padStart(2, "0")}
              </span>
            </div>
          </a>

          {/* Editorial Copy: Title, Subtitle, 2-Line Summary & Controls (Right 5 cols) */}
          <div className="flex min-w-0 flex-col justify-between p-6 sm:p-8 lg:col-span-5 lg:p-10">
            <div className="flex flex-col gap-4">
              {/* Date & Reading Time */}
              <div className="flex flex-wrap items-center gap-2.5 font-mono text-[12px] uppercase tracking-[0.12em] text-[var(--bz-text-3)]">
                <span>{dateLabel}</span>
                <span aria-hidden="true">·</span>
                <span className="inline-flex items-center gap-1.5">
                  <Clock className="h-3.5 w-3.5" aria-hidden="true" />
                  {readMinutes}
                </span>
              </div>

              {/* Title */}
              <h3
                className="text-[clamp(26px,2.8vw,38px)] font-normal leading-[1.08] tracking-[-0.02em] text-[var(--bz-text-1)]"
                style={SERIF}
              >
                <a
                  href={articleHref}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="hover:underline focus-visible:outline-none focus-visible:underline"
                >
                  {headline}
                </a>
              </h3>

              {/* Subtitle */}
              <p
                data-testid="carousel-article-subtitle"
                className="border-l-2 border-[var(--bz-text-1)] pl-3.5 text-[15px] font-semibold leading-snug text-[var(--bz-text-1)] sm:text-[17px]"
              >
                {subtitle}
              </p>

              {/* 2-Line Summary */}
              <p
                data-testid="carousel-article-summary"
                className="mt-1 line-clamp-2 text-[14px] leading-relaxed text-[var(--bz-text-2)] sm:text-[15px]"
              >
                {summary}
              </p>
            </div>

            {/* Footer Action & Prev/Next Controls */}
            <div className="mt-6 flex items-center justify-between gap-4 border-t border-[var(--bz-border)] pt-5">
              <a
                href={articleHref}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-2 rounded-lg border border-[var(--bz-text-1)] bg-[var(--bz-text-1)] px-4 py-2.5 text-[12px] font-bold uppercase tracking-[0.08em] text-[var(--bz-card)] transition-transform hover:scale-[1.02]"
              >
                <span>Read article</span>
                <ArrowUpRight className="h-4 w-4" aria-hidden="true" />
              </a>

              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={goPrev}
                  aria-label="Previous article"
                  className="inline-flex h-10 w-10 items-center justify-center rounded-full border border-[var(--bz-border)] bg-[var(--surface-raised)] text-[var(--bz-text-1)] transition-colors hover:bg-[var(--bz-border)]"
                >
                  <ChevronLeft className="h-4 w-4" aria-hidden="true" />
                </button>
                <button
                  type="button"
                  onClick={goNext}
                  aria-label="Next article"
                  className="inline-flex h-10 w-10 items-center justify-center rounded-full border border-[var(--bz-border)] bg-[var(--surface-raised)] text-[var(--bz-text-1)] transition-colors hover:bg-[var(--bz-border)]"
                >
                  <ChevronRight className="h-4 w-4" aria-hidden="true" />
                </button>
              </div>
            </div>
          </div>
        </div>
      </article>

      {/* ── Full-Width 6-Article Thumbnail Selector Rail ── */}
      <div
        role="group"
        aria-label="Latest 6 published articles"
        className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6"
      >
        {items.map((item, idx) => {
          const isSelected = idx === activeIndex;
          const itemCopy = deriveArticleEditorialCopy(item);
          const itemFallback =
            CATEGORY_COVER_FALLBACKS[
              (item.category || "business").toLowerCase()
            ] || "/static/blog/golden-visa.jpg";
          const thumbSrc = failedImages[item.slug]
            ? itemFallback
            : resolveArticleImage(item);
          const catShort = (item.category || "news")
            .replace(/[-_]/g, " ")
            .toUpperCase();

          return (
            <button
              key={item.slug}
              type="button"
              onClick={() => setActiveIndex(idx)}
              aria-current={isSelected ? "true" : undefined}
              className="group relative flex min-w-0 flex-col overflow-hidden rounded-xl border text-left transition-all"
              style={{
                background: isSelected
                  ? "var(--surface-raised)"
                  : "var(--bz-card)",
                borderColor: isSelected
                  ? "var(--bz-text-1)"
                  : "var(--bz-border)",
              }}
            >
              {/* Active top bar */}
              <div
                aria-hidden="true"
                className="h-1 w-full transition-opacity"
                style={{
                  background: "var(--bz-text-1)",
                  opacity: isSelected ? 1 : 0.14,
                }}
              />

              {/* Thumbnail image */}
              <div className="relative aspect-[16/10] w-full overflow-hidden bg-[var(--surface-sunken)]">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={thumbSrc}
                  alt=""
                  aria-hidden="true"
                  onError={() =>
                    setFailedImages((prev) => ({
                      ...prev,
                      [item.slug]: true,
                    }))
                  }
                  className="h-full w-full object-cover transition-transform duration-500 group-hover:scale-105"
                />
              </div>

              {/* Compact meta & title */}
              <div className="flex flex-1 flex-col justify-between gap-1.5 p-3">
                <div className="flex items-center justify-between gap-1 font-mono text-[10px] font-bold uppercase tracking-[0.08em] text-[var(--bz-text-3)]">
                  <span>{String(idx + 1).padStart(2, "0")}</span>
                  <span className="truncate">{catShort}</span>
                </div>
                <p className="line-clamp-2 text-[12px] font-bold leading-snug text-[var(--bz-text-1)]">
                  {itemCopy.headline}
                </p>
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
