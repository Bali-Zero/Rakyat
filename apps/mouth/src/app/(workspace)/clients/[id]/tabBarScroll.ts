/**
 * Keeps the active tab inside the tab bar's own horizontal scroll, without
 * ever scrolling the page (scrollIntoView would also move it vertically).
 * Pure DOM arithmetic so it is unit-testable.
 */
export function scrollActiveTabIntoView(bar: HTMLElement | null): void {
  const tab = bar?.querySelector<HTMLElement>('[aria-current="page"]');
  if (!bar || !tab) return;
  const left = tab.offsetLeft;
  const right = left + tab.offsetWidth;
  if (left < bar.scrollLeft) bar.scrollLeft = left;
  else if (right > bar.scrollLeft + bar.clientWidth)
    bar.scrollLeft = right - bar.clientWidth;
}
