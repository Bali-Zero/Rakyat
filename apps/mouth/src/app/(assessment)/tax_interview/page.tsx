import { cookies } from "next/headers";
import { Interview } from "./_components/Interview";
import { PinGate } from "./_components/PinGate";
import { COOKIE, verifyToken } from "./_server/access";
import { CONTENT } from "./_server/content";

// Per request: the questions exist only in the response of a request that
// carries a valid signed cookie. Without it the visitor receives the PIN form.
export const dynamic = "force-dynamic";

export default async function TaxInterviewPage() {
  const jar = await cookies();
  if (!verifyToken(jar.get(COOKIE)?.value)) return <PinGate />;
  return <Interview content={CONTENT} />;
}
