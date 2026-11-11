/**
 * Same-tab signal that the stored session (`user_profile`) was written or
 * removed. The browser's `storage` event never fires in the tab that made the
 * change, so a same-tab sign-out or sign-in is otherwise invisible to pages
 * that hold per-person data. The single writer of `user_profile` (ApiClient and
 * the public-auth client) calls this right after every write and removal.
 */
export const SESSION_CHANGED_EVENT = "bz:session-changed";

export function notifySessionChanged(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(SESSION_CHANGED_EVENT));
}
