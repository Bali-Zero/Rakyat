import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import {
  DashboardNewsCarousel,
  cleanArticleSummary,
  deriveArticleEditorialCopy,
  type DashboardNewsArticle,
} from "../DashboardNewsCarousel";

const SIX_ARTICLES: DashboardNewsArticle[] = [
  {
    slug: "remote-work-bali-budget-workday-readiness",
    title:
      "Working Remotely from Bali: Budget for the Workday, Not Just the Stay",
    category: "living",
    publishedAt: "2026-11-12T02:36:47.148Z",
    coverImage: "/static/news/remote-work-bali-budget-workday-readiness.jpg",
    cardImage:
      "/static/news/remote-work-bali-budget-workday-readiness_card.jpg",
    readingTime: 2,
    excerpt:
      "An IDN Times career article published on 8 November discusses the preparation behind a sustainable digital-nomad lifestyle.",
  },
  {
    slug: "bali-creators-platform-withholding-annual-tax-records",
    title:
      "Platform Tax Deductions: Why Bali Creators Still Need an Annual Record",
    category: "taxes",
    publishedAt: "2026-11-12T02:35:00.494Z",
    coverImage:
      "/static/news/bali-creators-platform-withholding-annual-tax-records.jpg",
    readingTime: 2,
    excerpt:
      "NusaBali reported on 2 November a reminder to content creators that platform deductions do not remove the need to calculate their annual income tax.",
  },
  {
    slug: "indonesia-industrial-estate-licensing-environment-tenant-checks",
    title:
      "Faster Industrial-Estate Licensing: Which Businesses Are Actually Covered?",
    category: "business",
    publishedAt: "2026-11-12T02:33:18.807Z",
    coverImage:
      "/static/news/indonesia-industrial-estate-licensing-environment-tenant-checks.jpg",
    readingTime: 3,
    excerpt:
      "A coordination meeting at the Industry Ministry on 3 November 2026 discussed speeding up licensing within industrial estates.",
  },
  {
    slug: "bali-south-ring-road-property-project-evidence",
    title: "Bali's South Ring Road: Check the Project Before Pricing a Property",
    category: "business",
    publishedAt: "2026-11-12T02:30:11.393Z",
    coverImage:
      "/static/news/bali-south-ring-road-property-project-evidence.jpg",
    readingTime: 2,
    excerpt:
      "Badung's public works department recorded a groundbreaking for Jalan Lingkar Selatan. Its [official announcement](https://dpupr.badungkab.go.id/berita/73439-truncated",
  },
  {
    slug: "oss-tax-compliance-kswp-bali-pma-checklist",
    title: "OSS and Tax Compliance: A Practical Check for Bali PT PMAs",
    category: "business",
    publishedAt: "2026-11-12T02:20:00.000Z",
    coverImage: "/static/news/oss-tax-compliance-kswp-bali-pma-checklist.jpg",
    readingTime: 3,
    excerpt:
      "Reports about linking business licensing with tax compliance should be read with their original dates.",
  },
  {
    slug: "indonesia-london-investment-commitments-versus-realisation",
    title:
      "London Investment Commitments: What Indonesia's US$385.1 Million Headline Means",
    category: "business",
    publishedAt: "2026-11-12T02:10:00.000Z",
    coverImage:
      "/static/news/indonesia-london-investment-commitments-versus-realisation.jpg",
    readingTime: 3,
    excerpt:
      "Indonesia reported investment commitments worth US$385.1 million associated with the Indonesia Investment Forum held in London.",
  },
];

describe("DashboardNewsCarousel", () => {
  it("renders the active article with large cover image, title, subtitle, and 2-line summary, plus all 6 article thumbnails", () => {
    render(<DashboardNewsCarousel articles={SIX_ARTICLES} />);

    expect(screen.getByTestId("dashboard-news-carousel")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Working Remotely from Bali" }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("carousel-article-subtitle")).toHaveTextContent(
      "Budget for the Workday, Not Just the Stay",
    );
    expect(screen.getByTestId("carousel-article-summary")).toHaveTextContent(
      "An IDN Times career article published on 8 November discusses the preparation behind a sustainable digital-nomad lifestyle.",
    );
    expect(screen.getByText("01 / 06")).toBeInTheDocument();
  });

  it("navigates between the 6 articles via Next/Previous controls and thumbnail buttons", () => {
    render(<DashboardNewsCarousel articles={SIX_ARTICLES} />);

    fireEvent.click(screen.getByRole("button", { name: "Next article" }));
    expect(
      screen.getByRole("heading", { name: "Platform Tax Deductions" }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("carousel-article-subtitle")).toHaveTextContent(
      "Why Bali Creators Still Need an Annual Record",
    );
    expect(screen.getByText("02 / 06")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Previous article" }));
    expect(
      screen.getByRole("heading", { name: "Working Remotely from Bali" }),
    ).toBeInTheDocument();
  });

  it("strips truncated markdown links and raw URLs from article summaries", () => {
    const cleaned = cleanArticleSummary(SIX_ARTICLES[3].excerpt);
    expect(cleaned).toBe(
      "Badung's public works department recorded a groundbreaking for Jalan Lingkar Selatan. Its official announcement",
    );
    expect(cleaned).not.toContain("https://");

    const copy = deriveArticleEditorialCopy(SIX_ARTICLES[3]);
    expect(copy.headline).toBe("Bali's South Ring Road");
    expect(copy.subtitle).toBe("Check the Project Before Pricing a Property");
  });
});
