import { NextRequest, NextResponse } from "next/server";
import type { ArticleCategory, ArticleSearchParams } from "@/lib/blog/types";
import {
  getAllArticles,
  searchArticles,
  getCategoryCounts,
} from "@/lib/blog/articles";
import { logger } from "@/lib/logger";
import { toError } from "@/lib/types/common";

/**
 * GET /api/blog/articles
 * List articles with optional filtering from local MDX files
 */
export async function GET(request: NextRequest) {
  try {
    const { searchParams } = new URL(request.url);

    // Build query params
    const query = searchParams.get("q") || undefined;
    const category = searchParams.get("category") || undefined;
    const featured = searchParams.get("featured") === "true" ? true : undefined;
    const limit = parseInt(searchParams.get("limit") || "12");
    const offset = parseInt(searchParams.get("offset") || "0");

    // If search query, use search function
    if (query) {
      const articles = await searchArticles(query, limit);
      return NextResponse.json({
        articles,
        total: articles.length,
        hasMore: false,
      });
    }

    // Otherwise, get all articles with filters
    const { articles, total } = await getAllArticles({
      category,
      featured,
      limit,
      offset,
    });

    return NextResponse.json({
      articles,
      total,
      hasMore: offset + articles.length < total,
    });
  } catch (error) {
    logger.error(
      "Failed to fetch articles",
      { component: "BlogArticles", action: "list" },
      toError(error),
    );

    return NextResponse.json(
      { error: "Failed to fetch articles" },
      { status: 500 },
    );
  }
}

/**
 * POST /api/blog/articles
 * Create a new article - Not implemented for static MDX files
 */
export async function POST(_request: NextRequest) {
  return NextResponse.json(
    {
      error:
        "Article creation not supported via API. Create MDX file directly in src/content/articles/[category]/",
    },
    { status: 501 },
  );
}
