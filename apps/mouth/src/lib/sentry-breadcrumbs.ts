import type { Breadcrumb, BreadcrumbHint } from "@sentry/nextjs";

/**
 * Pages that show a person's HR data wrap it in a protected region
 * (`<div className="sentry-block" data-sentry-block="">`). Replay already
 * blocks that region; click and keypress breadcrumbs do not, because Sentry
 * serialises the target's id, classes and aria-label/title/name/alt/type
 * attributes into the breadcrumb, and the next error report carries it.
 *
 * So every `ui.*` breadcrumb whose DOM target sits inside a protected region
 * is dropped, whatever the page puts in its attributes. Breadcrumbs from the
 * rest of the app are untouched.
 */
export const PROTECTED_REGION_SELECTOR = "[data-sentry-block], .sentry-block";

export function dropProtectedUiBreadcrumb(
  breadcrumb: Breadcrumb,
  hint?: BreadcrumbHint,
): Breadcrumb | null {
  if (!breadcrumb.category?.startsWith("ui.")) return breadcrumb;
  const target = (hint?.event as { target?: unknown } | undefined)?.target;
  if (
    typeof Element !== "undefined" &&
    target instanceof Element &&
    target.closest(PROTECTED_REGION_SELECTOR)
  ) {
    return null;
  }
  return breadcrumb;
}
