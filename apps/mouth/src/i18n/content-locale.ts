import { DEFAULT_LOCALE, LOCALES, type Locale } from "./types";

/**
 * Who owns `document.documentElement.lang`.
 *
 * The attribute describes the language of the document's MAIN CONTENT. On an
 * article route that content is the MDX body, and the SERVER picks it: it
 * honours `?lang=xx` only when a `{slug}.{locale}.mdx` exists and otherwise
 * falls back to English (`getArticleByLocale`, articles.ts). The I18nProvider
 * knows something different — the UI-chrome locale, restored from the
 * `blog-language` preference — and the two disagree routinely: a reader who
 * picked "it" from the switcher gets Italian chrome around an English article.
 *
 * Before this marker both wrote the same field with no rule, so whichever ran
 * last won and the document could claim a language its body was not in. The
 * rule: when a page KNOWS its content language, it says so by taking
 * ownership, and the provider yields.
 */
export const LANG_OWNER_ATTR = "data-lang-owner";
export const LANG_OWNER_CONTENT = "content";

/**
 * The locale the server actually SERVED, which is not the locale that was
 * asked for: an unknown code, or a known one with no translation on disk,
 * both come back as English. Article pages pass the locale from the resolved article.
 */
export function resolveContentLocale(
  requested: string | undefined | null,
  available: readonly string[],
): Locale {
  if (!requested) return DEFAULT_LOCALE;
  if (!(LOCALES as readonly string[]).includes(requested))
    return DEFAULT_LOCALE;
  if (!available.includes(requested)) return DEFAULT_LOCALE;
  return requested as Locale;
}

// The shared layout labels the site chrome in English. Article headings and bodies
// carry their own server-rendered lang, before ContentLangSync runs in the browser.
