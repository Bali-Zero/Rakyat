import { parseOracleLocale } from "../_lib/oracle-locale";
import { VisaOraclePrivacyClient } from "./VisaOraclePrivacyClient";

export default async function VisaOraclePrivacyPage({
  searchParams,
}: {
  searchParams: Promise<{ lang?: string }>;
}) {
  const { lang } = await searchParams;
  const initialLanguage = parseOracleLocale(lang);

  return <VisaOraclePrivacyClient initialLanguage={initialLanguage} />;
}
