"use client";

import useSWR from "swr";
import { api } from "@/lib/api";
import { normalizeWaPhone } from "@/lib/wa-phone";

/**
 * The client's WhatsApp number from their portal profile, normalised to the
 * notification-prefs format (E.164 digits, no "+"), or `null`.
 *
 * Reads `api.portal.getProfile()` — the same call the Profile page uses — and
 * prefers `whatsapp`, falling back to `phone`. Used only to pre-fill the
 * WhatsApp field in Settings → Notifications when no `wa_phone` is saved yet,
 * so the client does not have to type the same number twice. A failed read
 * simply means no pre-fill.
 *
 * SWR (not React Query) so it works in both notification forms without
 * requiring a QueryClientProvider.
 */
export function usePortalProfileWaPhone(): string | null {
  const { data } = useSWR(
    ["portal-profile-wa-phone"],
    async () => {
      try {
        const profile = await api.portal.getProfile();
        return (
          normalizeWaPhone(profile.whatsapp) ?? normalizeWaPhone(profile.phone)
        );
      } catch {
        return null;
      }
    },
    { revalidateOnFocus: false, shouldRetryOnError: false },
  );
  return data ?? null;
}
