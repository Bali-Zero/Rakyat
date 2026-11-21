import { NextRequest, NextResponse } from "next/server";
import { serialize } from "next-mdx-remote/serialize";
import remarkGfm from "remark-gfm";
import type { Article } from "@/lib/blog/types";
import { getArticleBySlug, isArticleHidden } from "@/lib/blog/articles";
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

    // A hidden article is not found: no demo stand-in replaces it.
    if (isArticleHidden(category, slug)) {
      return NextResponse.json({ error: "Article not found" }, { status: 404 });
    }

    // Fallback to mock article for demo
    const mockArticle = getMockArticle(category, slug);
    if (mockArticle) {
      return NextResponse.json(mockArticle);
    }

    return NextResponse.json({ error: "Article not found" }, { status: 404 });
  } catch (error) {
    logger.error(
      "Failed to fetch article",
      { component: "BlogArticle", action: "fetch" },
      toError(error),
    );

    // Try mock data as fallback, never for a hidden article
    const { category, slug } = await params;
    const mockArticle = isArticleHidden(category, slug)
      ? null
      : getMockArticle(category, slug);
    if (mockArticle) {
      return NextResponse.json(mockArticle);
    }

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

// Mock article for demo/development — Partial because not all fields are populated
function getMockArticle(category: string, slug: string): Article | null {
  const mockArticles: Record<string, Partial<Article>> = {
    "immigration/golden-visa-revolution": {
      id: "1",
      slug: "golden-visa-revolution",
      title:
        "The Golden Visa Revolution: Indonesia's $350K Bet on Global Talent",
      excerpt:
        "Indonesia launches its Golden Visa program, offering 5-10 year stays for investors and high-net-worth individuals.",
      content: `
# The Golden Visa Revolution

Indonesia has officially launched its highly anticipated **Golden Visa** program, marking a significant shift in the nation's approach to attracting global talent and investment.

## What is the Golden Visa?

The Golden Visa is a special residency permit that allows high-net-worth individuals and investors to stay in Indonesia for **5 to 10 years** without the need for regular visa renewals.

### Investment Requirements

| Tier | Investment | Duration | Benefits |
|------|------------|----------|----------|
| E28C (Capital Market) | USD 350,000+ (bonds, public stocks, mutual funds) | 5 years | Multi-entry residency, family sponsorship (no salaried employment) |
| E28C (Capital Market / Property) | USD 700,000+ (financial instruments) or USD 1,000,000+ (apartment) | 10 years | Multi-entry residency, family sponsorship (no salaried employment) |
| E28B (Company Establishment) | USD 2,500,000+ (5y) / USD 5,000,000+ (10y) | 5–10 years | Establish/direct PT PMA (no fast-track citizenship) |

## Key Benefits

1. **Extended Stay** - No more short-term visa runs
2. **Investment & Director Authorization** - Right to invest and oversee your PT PMA (E28B) or hold passive capital-market/property assets (E28C); salaried local employment requires a work KITAS (E23)
3. **Family Inclusion** - Spouse and children included
4. **Property Rights** - Enhanced property ownership options
5. **Tax Benefits** - Potential tax advantages for investors

## Application Process

The application process is streamlined through the Immigration Directorate General:

\`\`\`
1. Submit initial application online
2. Provide proof of investment/net worth
3. Complete background check
4. Attend interview (if required)
5. Receive Golden Visa
\`\`\`

## Impact on Bali's Economy

This program is expected to bring:

- **Increased foreign investment** in local businesses
- **Growth in property sector** as more foreigners can invest
- **Job creation** through new business ventures
- **Knowledge transfer** from global talent

## Expert Opinion

> "The Golden Visa represents Indonesia's commitment to becoming a global hub for innovation and investment. We expect to see significant interest from entrepreneurs, retirees, and digital nomads alike."
>
> — Immigration Expert

## Conclusion

The Golden Visa program positions Indonesia, and particularly Bali, as a premier destination for those seeking long-term residency in Southeast Asia. With its combination of lifestyle benefits and investment opportunities, it's likely to attract significant interest from global citizens.

---

*Have questions about the Golden Visa? [Contact our team](/contact) for a free consultation.*
      `,
      coverImage: "/static/blog/golden-visa.jpg",
      category: "visas",
      tags: ["golden-visa", "investment", "residency", "indonesia"],
      author: {
        id: "zantara-ai",
        name: "Zantara AI",
        avatar: "/static/zantara-avatar.png",
        role: "AI Research Assistant",
        isAI: true,
      },
      publishedAt: new Date("2024-12-20"),
      updatedAt: new Date("2024-12-20"),
      readingTime: 12,
      viewCount: 15420,
      featured: true,
      trending: true,
      aiGenerated: true,
      status: "published",
      seoTitle: "Golden Visa Indonesia 2026: Complete Guide | Bali Zero",
      seoDescription:
        "Everything you need to know about Indonesia's Golden Visa program. Investment requirements, benefits, and application process explained.",
      relatedArticleIds: ["oss-2-complete-guide", "kitas-application-2026"],
      coverImageAlt: "Golden Visa Indonesia program",
      createdAt: new Date("2024-12-20"),
      shareCount: 234,
      likeCount: 89,
      commentCount: 12,
      locale: "en",
    },
    "business/oss-2-complete-guide": {
      id: "2",
      slug: "oss-2-complete-guide",
      title: "OSS 2.0: The Complete Guide to Indonesia Business Licensing",
      excerpt:
        "Everything you need to know about the Online Single Submission system for company registration.",
      content: `
# OSS 2.0: Complete Guide

The **Online Single Submission (OSS)** system is Indonesia's integrated business licensing platform. Version 2.0, also known as OSS-RBA (Risk-Based Approach), has streamlined the process significantly.

## What is OSS?

OSS is a single-window system that allows businesses to:

- Register their company
- Obtain business licenses
- Get required permits
- All in one integrated platform

## The Risk-Based Approach

Under OSS-RBA, businesses are classified by risk level:

### Low Risk
- Simple registration
- Immediate NIB (Business Identification Number)
- No additional permits required

### Medium-Low Risk
- Standard registration
- Self-declaration compliance
- Minimal documentation

### Medium-High Risk
- Verification required
- Technical standards compliance
- Inspections may be needed

### High Risk
- Full verification
- Pre-operational inspections
- Ongoing compliance monitoring

## Step-by-Step Registration

1. **Create OSS Account**
2. **Complete Company Profile**
3. **Select KBLI Codes**
4. **Submit Required Documents**
5. **Receive NIB**
6. **Obtain Sector-Specific Permits**

## Common KBLI Codes for Foreigners

| Code | Description | Risk Level |
|------|-------------|------------|
| 62199 | Software & Application Development | Low |
| 70201 | Management & Tourism Consulting | Low (check Bali OSS restrictions) |
| 73100 | Advertising | Medium-Low |
| 68210 | Real Estate Intermediation / Brokerage | Medium-High |

## Timeline

- Low Risk: 1-3 days
- Medium Risk: 1-2 weeks
- High Risk: 1-3 months

---

*Need help with OSS registration? [Contact us](/contact) for professional assistance.*
      `,
      coverImage: "/static/blog/oss-guide.jpg",
      category: "business",
      tags: ["oss", "business-license", "pt-pma", "indonesia"],
      author: {
        id: "zantara-ai",
        name: "Zantara AI",
        avatar: "/static/zantara-avatar.png",
        role: "AI Research Assistant",
        isAI: true,
      },
      publishedAt: new Date("2024-12-18"),
      updatedAt: new Date("2024-12-18"),
      readingTime: 15,
      viewCount: 8932,
      featured: false,
      trending: true,
      aiGenerated: false,
      status: "published",
      seoTitle: "OSS 2.0 Guide: Indonesia Business License System | Bali Zero",
      seoDescription:
        "Complete guide to Indonesia's OSS 2.0 system. Learn how to register your business, obtain licenses, and navigate KBLI codes.",
      relatedArticleIds: ["golden-visa-revolution", "tax-deadlines-2026"],
      coverImageAlt: "OSS 2.0 Business Licensing Indonesia",
      createdAt: new Date("2024-12-18"),
      shareCount: 156,
      likeCount: 67,
      commentCount: 8,
      locale: "en",
    },
    "property/capital-evolution-bkpm-5-2025": {
      id: "capital-evolution",
      slug: "capital-evolution-bkpm-5-2025",
      title: "Capital Evolution: How BKPM 5/2025 Saved Property Investors",
      excerpt:
        "BKPM Reg 5/2025 sets PT PMA paid-up capital at IDR 2.5B per company, while the >IDR 10B investment threshold still excludes land and buildings—and KBLI 55203 Villas remains 0% PMA.",
      content: `
# Capital Evolution: BKPM 5/2025

For years, the **10 Billion IDR** barrier was the "monster under the bed" for small and medium investors in Bali. But with the enforcement of **BKPM Regulation No. 5 of 2025**, the paradigm has shifted.

## The Heart of the Reform

The BKPM distinguishes between **Paid-up Capital (Modal Disetor)** and **Minimum Total Investment Value (Nilai Investasi)** under Perka BKPM 5/2025 Art. 26:

- **Paid-up Capital (Modal Disetor):** Minimum **IDR 2.5 Billion per company** (subject to the 12-month lock-up rule unless used for legitimate business operations).
- **Minimum Total Investment:** Must still exceed **IDR 10 Billion per 5-digit KBLI per project location, EXCLUDING the value of land and buildings** (tanah dan bangunan). Real estate / land acquisition value does **not** count toward the IDR 10B BKPM threshold.

## The 12-Month Lock-up Rule

Attention: paid-up capital is subject to a **12-month lock-up period**. It can only be used for documented business purposes (construction, equipment/asset purchase, working capital).

## Why Compliance Structuring Matters

Property/land value (e.g., IDR 8 Billion in land or villa shell) CANNOT be added to IDR 2.5 Billion paid-up capital to clear the **>IDR 10 Billion** BKPM minimum investment rule—the IDR 10B threshold explicitly excludes land and buildings. Furthermore, **KBLI 55203 (Aktivitas Vila)** is **0% PMA** (reserved for Koperasi/UMKM under Perpres 49/2021). Foreign hospitality projects must use open PMA KBLIs such as **55101 (Star Hotel, >= 6,000 m² in Bali under the May 2026 moratorium)** or **55204 (Serviced Apartments)** in compliant zoning with PKKPR and PBG & SLF via SIMBG.
      `,
      coverImage: "/static/insights/property/property-via-pt-pma-indonesia.jpg",
      category: "property",
      author: {
        id: "zantara-ai",
        name: "Zantara AI",
        avatar: "/static/zantara-lotus.png",
        role: "AI Business Advisor",
        isAI: true,
      },
      publishedAt: new Date("2026-02-23"),
      readingTime: 8,
      viewCount: 12450,
      featured: true,
      trending: true,
      aiGenerated: true,
      status: "published",
    },
    "property/ota-delisting-bali-2026": {
      id: "ota-delisting",
      slug: "ota-delisting-bali-2026",
      title: "Digital Darwinism: The March 31 OTA Cut-off",
      excerpt:
        "Airbnb and Booking.com are syncing with the NIB database. Wrong code? Your listing goes dark with no warning.",
      content: `
# Digital Darwinism: March 31

March 31, 2026, is not an administrative date—it's a digital **kill switch**. The Indonesian Ministry of Tourism has completed API integration with major OTAs (Airbnb, Booking.com, Expedia).

## Automatic Execution

The **OSS-RBA** system now communicates in real-time with booking platforms. If your listing is categorized as a "Villa" but your NIB shows code **68111 (Real Estate)**, the algorithm detects the mismatch.

**Result:** Immediate delisting. No warning email, no human appeal.

## The Tax Mismatch

Our research shows integration goes beyond OSS. It crosses data with **SITP (Tourist Tax)**. If you operate under 68111, you are technically paying the wrong taxes. Airbnb delisting is just step one; step two is a retroactive tax audit.

## Corrective Action

For foreign-owned PT PMA operators, migrating to **KBLI 55203 (Aktivitas Vila)** is NOT permitted: under Perpres 49/2021 Lampiran II, 55203 is **0% PMA** (reserved for Indonesian Koperasi & UMKM) and lacks a Large-scale entry in PP 28/2025. Eligible foreign hospitality projects must evaluate open PMA codes such as **55204 (Serviced Apartments)** or **55101 (5-Star Hotel — >= 6,000 m² in Bali)** in a **Pink Zone (Tourism)** with PKKPR and PBG/SLF via SIMBG — or long-term residential leasing (68112, closed to new Bali PMAs).
      `,
      coverImage: "/static/insights/property/airbnb-bali-ban-truth.jpg",
      category: "property",
      author: {
        id: "zantara-ai",
        name: "Zantara AI",
        avatar: "/static/zantara-lotus.png",
        role: "AI Research",
        isAI: true,
      },
      publishedAt: new Date("2026-02-24"),
      readingTime: 10,
      viewCount: 18920,
      featured: true,
      trending: true,
      aiGenerated: true,
      status: "published",
    },
    "property/glamping-trap-kbli-55209": {
      id: "glamping-trap",
      slug: "glamping-trap-kbli-55209",
      title: "The Glamping Trap: Why 55209 is a Compliance Minefield",
      excerpt:
        "KBLI 55209 strictly prohibits daily housekeeping. Using it for luxury resorts is an invitation for NIB revocation.",
      content: `
# The Glamping Trap: KBLI 55209

KBLI **55209** ("Other accommodation") has become the favorite refuge for those wanting to avoid strict hotel requirements. Glamping, bungalows, treehouses: they seem agile. In reality, they are a legal minefield.

## The Service Paradox

The official 2025 text for 55209 contains a lethal clause: **"Daily housekeeping service is not permitted."** If you run a luxury eco-resort under this code but remake the beds every morning, you are operating outside your license.

## Review Intelligence

The government doesn't need to enter your property to fine you. Authorities have started using **Review Scraping** software. A TripAdvisor review praising "excellent daily cleaning" is now sufficient documentary evidence to revoke a 55209 NIB.

## Strategic Choice

If your PT PMA brand requires daily hotel-level services, do NOT migrate to **55106 (Non-star Hotel)** or **55203 (Villa)** — both are **0% PMA** (reserved exclusively for Indonesian Koperasi & UMKM under Perpres 49/2021 Lampiran II). The compliant foreign-investment route is upgrading to open PMA codes such as **55204 (Serviced Apartments / Aparthotel)** or **55101 (5-Star Hotel — >= 6,000 m² in Bali)**, meeting standard PT PMA investment rules (>IDR 10B excluding land/buildings).
      `,
      coverImage: "/static/insights/property/property-green-zone-alert.jpg",
      category: "property",
      author: {
        id: "zantara-ai",
        name: "Zantara AI",
        avatar: "/static/zantara-lotus.png",
        role: "AI Business Advisor",
        isAI: true,
      },
      publishedAt: new Date("2026-02-25"),
      readingTime: 7,
      viewCount: 9420,
      featured: true,
      trending: true,
      aiGenerated: true,
      status: "published",
    },
  };

  const key = `${category}/${slug}`;
  return (mockArticles[key] as Article) || null;
}
