import { Loader2 } from "lucide-react";

export default function Loading() {
  return (
    // No visible text. This root boundary wraps EVERY route, so its fallback
    // is streamed into the server HTML of public pages (/, /cookies,
    // /services/<slug>…) and crawlers read it as page copy: "Loading
    // Zantara..." was being indexed on pages that are not Zantara. The
    // spinner plus an accessible name is enough while a route streams.
    <div
      role="status"
      aria-label="Loading"
      className="flex h-screen w-full items-center justify-center bg-[var(--background)]"
    >
      <Loader2
        aria-hidden="true"
        className="h-10 w-10 animate-spin text-[var(--accent)]"
      />
    </div>
  );
}
