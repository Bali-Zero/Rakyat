// Guard: an article page has exactly one <h1> (the template's title), even when
// the article body repeats the title as a Markdown `# heading` — 170+ article
// files do (e.g. immigration/airport-procedures.mdx). Covers both body paths:
// the SSR MDX body (shared `mdxComponents`) and the client ReactMarkdown fallback.
import { renderToString } from "react-dom/server";
import remarkGfm from "remark-gfm";
import { compileMDX } from "next-mdx-remote/rsc";
import { describe, expect, it, vi } from "vitest";

// ArticleClient's only dynamic() is react-markdown (ssr:false); load it eagerly
// so the client fallback body is in the rendered HTML.
vi.mock("next/dynamic", async () => {
  const ReactMarkdown = (await import("react-markdown")).default;
  return { default: () => ReactMarkdown };
});
vi.mock("@/components/blog", () => ({
  CategoryBadge: () => null,
  TableOfContents: () => null,
  FloatingToc: () => null,
  ReadingProgress: () => null,
  ArticleCard: () => null,
  NewsletterSidebar: () => null,
  ArticleEngagement: () => null,
}));
vi.mock("@/components/blog/MDXContent", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/components/blog/MDXContent")>();
  return { ...actual, MDXContent: () => null };
});
vi.mock("@/components/lead/WhatsAppLeadButton", () => ({
  WhatsAppLeadButton: () => null,
}));
vi.mock("@/lib/api", () => ({ api: { blog: { getArticle: vi.fn() } } }));

import { mdxComponents } from "@/components/blog/MDXContent";
import {
  ArticleClient,
  type SerializedArticleForClient,
} from "./ArticleClient";

const TITLE = "Synthetic Guard Title";
const BODY = `# ${TITLE}\n\nIntro paragraph.\n\n## Section one\n\nText.`;

function article(
  overrides: Partial<SerializedArticleForClient> = {},
): SerializedArticleForClient {
  return {
    id: "a1",
    slug: "synthetic-guard",
    title: TITLE,
    excerpt: "excerpt",
    content: BODY,
    coverImage: "",
    coverImageAlt: "cover alt",
    category: "immigration",
    tags: [],
    author: { id: "au1", name: "Bali Zero Team", role: "Editorial", bio: "" },
    createdAt: "2026-01-01T00:00:00Z",
    updatedAt: "2026-01-01T00:00:00Z",
    publishedAt: "2026-01-01T00:00:00Z",
    status: "published",
    featured: false,
    trending: false,
    readingTime: 4,
    viewCount: 0,
    shareCount: 0,
    likeCount: 0,
    commentCount: 0,
    relatedArticleIds: [],
    locale: "en",
    ...overrides,
  } as unknown as SerializedArticleForClient;
}

const h1Count = (html: string) => (html.match(/<h1[\s>]/g) ?? []).length;

describe("article template renders a single <h1>", () => {
  it("labels an inherited English headline separately from the translated body", () => {
    const html = renderToString(
      <ArticleClient
        category="business"
        slug="synthetic-guard"
        initialArticle={article({ locale: "it", titleLocale: "en" })}
      />,
    );
    expect(html).toMatch(/<h1[^>]*lang="en"/);
    expect(html).toMatch(/<article[^>]*lang="it"/);
  });

  it("SSR MDX body: a body `# title` does not add a second <h1>", async () => {
    const { content } = await compileMDX({
      source: BODY,
      components: mdxComponents,
      options: { mdxOptions: { remarkPlugins: [remarkGfm] } },
    });
    const html = renderToString(
      <ArticleClient
        category="visas"
        slug="synthetic-guard"
        initialArticle={article()}
        mdxBody={content}
      />,
    );
    expect(html).toContain("Intro paragraph."); // body actually rendered
    expect(h1Count(html)).toBe(1);
    expect(html).toMatch(/<h2[^>]*>Synthetic Guard Title<\/h2>/);
  });

  it("ReactMarkdown fallback: a body `# title` does not add a second <h1>", () => {
    const html = renderToString(
      <ArticleClient
        category="business"
        slug="synthetic-guard"
        initialArticle={article()}
      />,
    );
    expect(html).toContain("Intro paragraph."); // fallback path actually rendered
    expect(h1Count(html)).toBe(1);
  });
});
