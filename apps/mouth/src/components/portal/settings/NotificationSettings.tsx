"use client";

import { useEffect, useState } from "react";
import { useNotificationPrefs } from "@/hooks/useNotificationPrefs";
import { usePortalProfileWaPhone } from "@/hooks/usePortalProfileWaPhone";
import { Button } from "@/components/ui/button";
import { normalizeWaPhone } from "@/lib/wa-phone";

const INVALID_WA_PHONE =
  "Enter a valid WhatsApp number with country code, e.g. +62 890 0000 0000.";

/**
 * Notification channel preferences (email + WhatsApp).
 *
 * Wires directly to `GET/PUT /api/portal/notifications/prefs`. The hook
 * catches the backend's client-safe 503 and exposes it through the legacy
 * `migrationMissing` field. Under no circumstances do we fake the toggles:
 * when prefs cannot be read, controls are hidden.
 */
export function NotificationSettings() {
  const {
    data,
    migrationMissing,
    isLoading,
    error,
    mutate,
    updatePrefs,
    isUpdating,
  } = useNotificationPrefs();
  const [waPhone, setWaPhone] = useState("");
  const [saveError, setSaveError] = useState<string | null>(null);
  const profileWaPhone = usePortalProfileWaPhone();
  const prefsLoaded = data != null;

  useEffect(() => {
    setWaPhone(data?.wa_phone ?? "");
  }, [data?.wa_phone]);

  // No saved WhatsApp number yet: offer the one from the client's profile.
  // Never overwrite a saved number or something the client already typed.
  useEffect(() => {
    if (!prefsLoaded || data?.wa_phone || !profileWaPhone) return;
    setWaPhone((current) => current || profileWaPhone);
  }, [prefsLoaded, data?.wa_phone, profileWaPhone]);

  const persist = async (payload: NonNullable<typeof data>): Promise<void> => {
    setSaveError(null);
    try {
      await updatePrefs(payload);
    } catch {
      setSaveError("Unable to save notification preferences.");
    }
  };

  if (isLoading) {
    return <p className="text-sm text-[var(--bz-text-2)]">Loading…</p>;
  }

  if (migrationMissing) {
    return (
      <section className="space-y-4">
        <p role="alert" className="text-sm text-[var(--state-warning)]">
          Notification preferences are temporarily unavailable. Your saved
          choices have not been changed.
        </p>
        <Button type="button" variant="outline" onClick={() => void mutate()}>
          Retry
        </Button>
      </section>
    );
  }

  if (error || !data) {
    return (
      <section className="space-y-4">
        <p role="alert" className="text-sm text-[var(--state-danger)]">
          Unable to load preferences. Your saved choices have not been changed.
        </p>
        <Button type="button" variant="outline" onClick={() => void mutate()}>
          Retry
        </Button>
      </section>
    );
  }

  return (
    <section className="space-y-4 max-w-md">
      <Toggle
        checked={data.email_enabled}
        disabled={isUpdating}
        onChange={(v) => void persist({ ...data, email_enabled: v })}
        label="Email notifications"
      />
      <Toggle
        checked={data.wa_enabled}
        disabled={isUpdating}
        onChange={(enabled) => {
          const phone = normalizeWaPhone(waPhone);
          if (enabled && !waPhone.trim()) {
            setSaveError(
              "Enter a valid WhatsApp number before enabling notifications.",
            );
            return;
          }
          if (waPhone.trim() && !phone) {
            setSaveError(INVALID_WA_PHONE);
            return;
          }
          void persist({
            ...data,
            wa_enabled: enabled,
            wa_phone: phone,
          });
        }}
        label="WhatsApp notifications"
      />
      <div>
        <label
          htmlFor="wa-phone-input"
          className="text-xs uppercase tracking-[2px] text-[var(--bz-text-3)] block mb-1"
        >
          WhatsApp number
        </label>
        <input
          id="wa-phone-input"
          type="tel"
          inputMode="tel"
          value={waPhone}
          disabled={isUpdating}
          aria-describedby="wa-phone-help"
          onChange={(event) => setWaPhone(event.target.value)}
          onBlur={() => {
            const phone = normalizeWaPhone(waPhone);
            if (waPhone.trim() && !phone) {
              setSaveError(INVALID_WA_PHONE);
              return;
            }
            if (phone) setWaPhone(phone);
            void persist({ ...data, wa_phone: phone });
          }}
          placeholder="+62 890 0000 0000"
          className="w-full px-3 py-2 bg-[var(--bz-card)] rounded border border-[var(--bz-border)] text-sm text-[var(--bz-text-1)] disabled:opacity-60"
        />
        <p
          id="wa-phone-help"
          className="text-[10px] text-[var(--bz-text-3)] mt-1"
        >
          Add the number before enabling WhatsApp, with the country code.
          Formats like +62 890 0000 0000 are fine.
        </p>
      </div>
      {saveError && (
        <p role="alert" className="text-sm text-[var(--state-danger)]">
          {saveError}
        </p>
      )}
    </section>
  );
}

function Toggle({
  checked,
  disabled,
  onChange,
  label,
}: {
  checked: boolean;
  disabled: boolean;
  onChange: (v: boolean) => void;
  label: string;
}) {
  return (
    <label className="flex items-center gap-3 cursor-pointer">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className="w-4 h-4 accent-[var(--bz-copper-text)] disabled:opacity-60"
      />
      <span className="text-sm text-[var(--bz-text-1)]">{label}</span>
    </label>
  );
}
