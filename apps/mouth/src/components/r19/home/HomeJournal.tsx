import { getAllArticles, getPinnedMdxArticles } from "@/lib/blog/articles";
import homepageLayout from "@/content/homepage-layout.json";
import { Journal } from "./Journal";
import {
  insightSlots,
  selectInsightArticles,
  selectJournalArticles,
} from "./journal/adapter";

export async function HomeJournal() {
  let articles: ReturnType<typeof selectJournalArticles> = [];
  let insights: ReturnType<typeof selectInsightArticles> = [];
  let status: "ready" | "empty" | "unavailable" = "empty";
  try {
    const result = await getAllArticles({});
    articles = selectJournalArticles(result.articles, homepageLayout);
    status = articles.length ? "ready" : "empty";
  } catch {
    // Handle reader/adapter failure here; React rendering stays outside the catch.
    status = "unavailable";
  }
  if (status === "ready") {
    try {
      // Pins come from their own MDX files, never from the feed or the backend.
      const layout: Record<string, string> = homepageLayout;
      const pins = await getPinnedMdxArticles(
        insightSlots.map((slot) => layout[slot]),
      );
      insights = selectInsightArticles(
        pins,
        layout,
        articles.map((article) => article.slug),
      );
    } catch {
      insights = [];
    }
  }
  return (
    <Journal
      articles={articles}
      insights={insights}
      status={status}
      indexHref="/news"
    />
  );
}

export function JournalPending() {
  return (
    <div className="wrap">
      <section
        className="journal"
        aria-busy="true"
        aria-label="The Bali Zero Journal"
      >
        <h2>The Bali Zero Journal</h2>
        {/* Streamed into the server HTML before the stories resolve, so it
            must read as content, not as a placeholder: a crawler kept
            "Loading the latest stories…" as homepage copy. */}
        <p>
          <a className="textlink" href="/news">
            Read the latest stories in the Journal{" "}
            <span aria-hidden="true">↗</span>
          </a>
        </p>
      </section>
    </div>
  );
}
