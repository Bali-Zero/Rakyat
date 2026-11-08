import { CATEGORY_METADATA, type ArticleListItem } from "@/lib/blog/types";
import type { JournalArticle } from "./types";

const slots = [
  "hero_main",
  "hero_2",
  "hero_3",
  "hero_4",
  "hero_5",
  "latest_1",
  "latest_2",
  "latest_3",
  "latest_4",
  "latest_5",
];

export const insightSlots = ["insight_1", "insight_2", "insight_3"] as const;

function isPublishable(
  article: ArticleListItem | undefined,
): article is ArticleListItem {
  return (
    !!article &&
    Object.prototype.hasOwnProperty.call(CATEGORY_METADATA, article.category)
  );
}

/** Keep the existing editorial preferences, then fill absent slots from the live reader. */
export function selectJournalArticles(
  articles: readonly ArticleListItem[],
  layout: Record<string, string>,
  limit = 7,
): JournalArticle[] {
  const bySlug = new Map(articles.map((article) => [article.slug, article]));
  const seen = new Set<string>();
  const ordered: { article: ArticleListItem | undefined; slot?: string }[] = [
    ...slots.map((slot) => ({ article: bySlug.get(layout[slot]), slot })),
    ...articles.map((article) => ({ article })),
  ];
  const picked: JournalArticle[] = [];
  for (const { article, slot } of ordered) {
    if (picked.length >= limit) break;
    if (!isPublishable(article) || seen.has(article.slug)) continue;
    seen.add(article.slug);
    picked.push(toJournalArticle(article, slot));
  }
  return picked;
}

/**
 * The Insight row shows only what the editors pinned in insight_1..3, in slot order.
 * An empty, unknown or already-shown slot is skipped and never filled from the feed.
 */
export function selectInsightArticles(
  articles: readonly ArticleListItem[],
  layout: Record<string, string>,
  shown: Iterable<string> = [],
): JournalArticle[] {
  const bySlug = new Map(articles.map((article) => [article.slug, article]));
  const seen = new Set(shown);
  const picked: JournalArticle[] = [];
  for (const slot of insightSlots) {
    const article = bySlug.get(layout[slot]);
    if (!isPublishable(article) || seen.has(article.slug)) continue;
    seen.add(article.slug);
    picked.push(toJournalArticle(article, slot));
  }
  return picked;
}

function toJournalArticle(
  article: ArticleListItem,
  homepagePosition?: string,
): JournalArticle {
  const date = new Date(article.publishedAt);
  const href = "/" + article.category + "/" + article.slug;
  return {
    slug: article.slug,
    title: article.title,
    image: article.coverImage
      ? { src: article.coverImage, alt: article.title }
      : null,
    category: CATEGORY_METADATA[article.category].label,
    date: Number.isNaN(date.getTime())
      ? null
      : {
          iso: date.toISOString(),
          label: new Intl.DateTimeFormat("en-GB", {
            day: "numeric",
            month: "long",
            year: "numeric",
            timeZone: "Asia/Makassar",
          }).format(date),
        },
    sourceUrl: href,
    finalSourceUrl: null,
    localHref: href,
    destinationStatus: "available",
    ...(homepagePosition ? { homepagePosition } : {}),
  };
}
