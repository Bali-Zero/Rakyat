/**
 * Serialize a value for the body of a <script type="application/ld+json"> element.
 *
 * JSON.stringify keeps "</script>" and "<!--" literally, so a string that contains them
 * (an FAQ answer, a frontmatter title) closes or confuses the script container. Escaping
 * the HTML-significant characters as unicode escapes keeps the output valid JSON that
 * parses back to the identical value, and leaves nothing for the HTML tokenizer to act on.
 * The line and paragraph separators are escaped too, for consumers that read the body as
 * a JavaScript string.
 */
const LINE_SEPARATOR = String.fromCharCode(0x2028);
const PARAGRAPH_SEPARATOR = String.fromCharCode(0x2029);

export function serializeJsonLd(value: unknown): string {
  return JSON.stringify(value)
    .replace(/</g, "\\u003c")
    .replace(/>/g, "\\u003e")
    .replace(/&/g, "\\u0026")
    .split(LINE_SEPARATOR)
    .join("\\u2028")
    .split(PARAGRAPH_SEPARATOR)
    .join("\\u2029");
}
