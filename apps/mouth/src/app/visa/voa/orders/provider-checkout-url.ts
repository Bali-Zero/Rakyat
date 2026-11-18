/**
 * The tracker's "Pay now" leaves the site for ONE place only: an `https:` page on the
 * payment provider's own checkout host. `resumeOrderCheckout` is served only for Xendit
 * invoices today (the backend refuses Midtrans with CHECKOUT_NOT_RESUMABLE), so the list
 * is Xendit's live and test-mode checkout hosts — the same list the backend's adapter
 * enforces (`XenditPaymentProvider.checkout_hosts`). Anything else is never followed.
 */
export const PROVIDER_CHECKOUT_HOSTS: ReadonlySet<string> = new Set([
  "checkout.xendit.co",
  "checkout-staging.xendit.co",
]);

export function isProviderCheckoutUrl(url: unknown): url is string {
  if (typeof url !== "string" || !url) return false;
  try {
    const parsed = new URL(url);
    return (
      parsed.protocol === "https:" &&
      parsed.port === "" &&
      parsed.username === "" &&
      parsed.password === "" &&
      PROVIDER_CHECKOUT_HOSTS.has(parsed.hostname)
    );
  } catch {
    return false;
  }
}

/** Indirection so tests can observe the hand-off without navigating jsdom. */
export const checkoutNavigation = {
  assign(url: string): void {
    window.location.assign(url);
  },
};
