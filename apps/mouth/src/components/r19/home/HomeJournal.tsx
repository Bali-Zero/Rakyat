import { getAllArticles } from "@/lib/blog/articles";
import homepageLayout from "@/content/homepage-layout.json";
import { Journal } from "./Journal";
import {
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
    insights = selectInsightArticles(
      result.articles,
      homepageLayout,
      articles.map((article) => article.slug),
    );
    status = articles.length ? "ready" : "empty";
  } catch {
    // Handle reader/adapter failure here; React rendering stays outside the catch.
    status = "unavailable";
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
        <p role="status">Loading the latest stories…</p>
      </section>
    </div>
  );
}
