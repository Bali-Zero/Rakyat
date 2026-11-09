import { describe, expect, it } from "vitest";
import { scrollActiveTabIntoView } from "./tabBarScroll";

function bar(active: { left: number; width: number }, scrollLeft: number) {
  const el = document.createElement("nav");
  const tab = document.createElement("button");
  tab.setAttribute("aria-current", "page");
  el.appendChild(tab);
  Object.defineProperty(tab, "offsetLeft", { value: active.left });
  Object.defineProperty(tab, "offsetWidth", { value: active.width });
  Object.defineProperty(el, "clientWidth", { value: 390 });
  el.scrollLeft = scrollLeft;
  return el;
}

describe("scrollActiveTabIntoView", () => {
  it("scrolls right just enough when the active tab is past the right edge", () => {
    const el = bar({ left: 500, width: 100 }, 0);
    scrollActiveTabIntoView(el);
    expect(el.scrollLeft).toBe(210);
  });
  it("scrolls left when the active tab is before the left edge", () => {
    const el = bar({ left: 40, width: 100 }, 300);
    scrollActiveTabIntoView(el);
    expect(el.scrollLeft).toBe(40);
  });
  it("INNOCENCE: a tab already in view leaves the scroll alone, and no active tab or bar is a no-op", () => {
    const el = bar({ left: 100, width: 100 }, 0);
    scrollActiveTabIntoView(el);
    expect(el.scrollLeft).toBe(0);
    expect(() => scrollActiveTabIntoView(null)).not.toThrow();
    expect(() =>
      scrollActiveTabIntoView(document.createElement("nav")),
    ).not.toThrow();
  });
});
