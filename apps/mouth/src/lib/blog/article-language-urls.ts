import type { Locale } from "@/i18n/types";

export const articleOgLocales: Record<Locale, string> = {
  en: "en_US",
  id: "id_ID",
  it: "it_IT",
  ru: "ru_RU",
  fr: "fr_FR",
};

export function articleLanguageUrl(
  category: string,
  slug: string,
  locale = "en",
) {
  const path = `https://balizero.com/${encodeURIComponent(category)}/${encodeURIComponent(slug)}`;
  return locale === "en" ? path : `${path}?lang=${encodeURIComponent(locale)}`;
}

export function articleLanguageAlternates(
  category: string,
  slug: string,
  locales: readonly string[],
): Record<string, string> {
  const languages = Object.fromEntries(
    locales.map((locale) => [
      locale,
      articleLanguageUrl(category, slug, locale),
    ]),
  );
  if (languages.en) languages["x-default"] = languages.en;
  return languages;
}
