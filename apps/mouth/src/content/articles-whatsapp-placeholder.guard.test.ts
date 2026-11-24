import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { compileMDX } from "next-mdx-remote/rsc";
import { renderToString } from "react-dom/server";
import remarkGfm from "remark-gfm";
import { describe, expect, it } from "vitest";

import { mdxComponents } from "@/components/blog/MDXContent";

/**
 * Three published articles showed readers the unfilled template text
 * "[Contact number]" where the WhatsApp contact belongs. The contact line now
 * uses the shared article CTA, so the number lives in code and not in the MDX.
 *
 * Declared limits: reads the MDX as text and matches this one English literal.
 * Translated placeholders are a separate problem and are not checked here.
 */
const ROOT = join(__dirname, "articles");
const PLACEHOLDER = "[Contact number]";
const CTA_LINE = '- **WhatsApp:** <HeaderWhatsAppCTA funnel="visa" />';
const FIXED = [
  "immigration/e25b-director-kitas-guide.mdx",
  "immigration/e31e-child-dependent-kitas-guide.mdx",
  "immigration/e31e-child-dependent-kitas-guide.ru.mdx",
];

const mdxFiles = (readdirSync(ROOT, { recursive: true }) as string[]).filter(
  (file) => file.endsWith(".mdx"),
);

describe("article WhatsApp contact placeholder", () => {
  it("positive control: the scan reads the article corpus", () => {
    expect(mdxFiles.length).toBeGreaterThan(1000);
    expect(mdxFiles).toEqual(expect.arrayContaining(FIXED));
  });

  it("no article shows the [Contact number] placeholder", () => {
    const offenders = mdxFiles.filter((file) =>
      readFileSync(join(ROOT, file), "utf8").includes(PLACEHOLDER),
    );
    expect(offenders).toEqual([]);
  });

  it.each(FIXED)("%s uses the shared WhatsApp CTA", (file) => {
    expect(readFileSync(join(ROOT, file), "utf8")).toContain(CTA_LINE);
  });

  it.each(FIXED)("%s renders a WhatsApp link", async (file) => {
    const { content } = await compileMDX({
      source: readFileSync(join(ROOT, file), "utf8"),
      components: mdxComponents,
      options: {
        parseFrontmatter: true,
        blockJS: false,
        blockDangerousJS: true,
        mdxOptions: { remarkPlugins: [remarkGfm], development: false },
      },
    });
    const html = renderToString(content);
    expect(html).toContain('href="https://wa.me/');
    expect(html).not.toContain(PLACEHOLDER);
  });
});
