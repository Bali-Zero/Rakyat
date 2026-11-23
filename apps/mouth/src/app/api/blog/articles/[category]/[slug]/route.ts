import { NextRequest, NextResponse } from "next/server";
import { serialize } from "next-mdx-remote/serialize";
import remarkGfm from "remark-gfm";
import { getArticleBySlug } from "@/lib/blog/articles";
import { logger } from "@/lib/logger";
import { toError } from "@/lib/types/common";

interface RouteParams {
  params: Promise<{
    category: string;
    slug: string;
  }>;
}

/**
 * Strip import statements from MDX content
 * These are handled by the component mapping, not runtime imports
 */
function stripImports(content: string): string {
  // Remove import statements (single and multi-line)
  return content
    .replace(/^import\s+[\s\S]*?from\s+['"][^'"]+['"];?\s*$/gm, "")
    .replace(/^import\s*{[\s\S]*?}\s*from\s*['"][^'"]+['"];?\s*$/gm, "")
    .trim();
}

/**
 * GET /api/blog/articles/[category]/[slug]
 * Get a single article by category and slug from local MDX files
 */
export async function GET(request: NextRequest, { params }: RouteParams) {
  try {
    const { category, slug } = await params;

    // Read article from local MDX files
    const article = await getArticleBySlug(category, slug);

    if (article) {
      // Strip import statements and serialize MDX content
      const cleanContent = stripImports(article.content);

      try {
        // Try to serialize MDX
        const mdxSource = await serialize(cleanContent, {
          mdxOptions: {
            remarkPlugins: [remarkGfm],
            development: process.env.NODE_ENV === "development",
          },
        });

        return NextResponse.json({
          ...article,
          mdxSource, // Serialized MDX for client-side rendering
        });
      } catch (mdxError) {
        logger.error(
          "MDX serialization failed, falling back to raw content",
          { component: "BlogArticle", action: "mdxSerialize" },
          toError(mdxError),
        );
        // Fallback: return article without mdxSource (will render as plain text)
        return NextResponse.json({
          ...article,
          mdxSource: null,
          content: cleanContent, // Return cleaned content (imports stripped)
        });
      }
    }

    return NextResponse.json({ error: "Article not found" }, { status: 404 });
  } catch (error) {
    logger.error(
      "Failed to fetch article",
      { component: "BlogArticle", action: "fetch" },
      toError(error),
    );

    return NextResponse.json(
      { error: "Failed to fetch article" },
      { status: 500 },
    );
  }
}

/**
 * PATCH /api/blog/articles/[category]/[slug]
 * Update an article - Not implemented for static MDX files
 */
export async function PATCH(_request: NextRequest, { params }: RouteParams) {
  const { category, slug } = await params;
  return NextResponse.json(
    {
      error: `Article updates not supported. Edit MDX file directly: src/content/articles/${category}/${slug}.mdx`,
    },
    { status: 501 },
  );
}

/**
 * DELETE /api/blog/articles/[category]/[slug]
 * Delete an article - Not implemented for static MDX files
 */
export async function DELETE(_request: NextRequest, { params }: RouteParams) {
  const { category, slug } = await params;
  return NextResponse.json(
    {
      error: `Article deletion not supported. Delete MDX file directly: src/content/articles/${category}/${slug}.mdx`,
    },
    { status: 501 },
  );
}
