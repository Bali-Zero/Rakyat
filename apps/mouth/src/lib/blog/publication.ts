/**
 * The one publication rule for News Room MDX, shared by the site's reader and
 * the build's llms exports: a file counts only when its frontmatter parsed and
 * its status is absent or exactly "published". Parse with an options object:
 * without one, gray-matter caches by content, and a parse that threw leaves an
 * empty frontmatter in that cache for the next read of the same file.
 */
export function isPublishedFrontmatter(data: Record<string, unknown>): boolean {
  return (
    !Object.prototype.hasOwnProperty.call(data, "status") ||
    data.status === "published"
  );
}
