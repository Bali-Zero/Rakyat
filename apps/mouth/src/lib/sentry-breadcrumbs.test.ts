import { afterEach, describe, expect, it, vi } from "vitest";

import { dropProtectedUiBreadcrumb } from "./sentry-breadcrumbs";

const { initMock } = vi.hoisted(() => ({ initMock: vi.fn() }));
vi.mock("@sentry/nextjs", () => ({
  init: initMock,
  replayIntegration: () => ({}),
}));

function mount(html: string): HTMLElement {
  const host = document.createElement("div");
  host.innerHTML = html;
  document.body.appendChild(host);
  return host;
}

const click = (target: EventTarget | null) => ({
  event: { target } as unknown as Event,
});

afterEach(() => {
  document.body.innerHTML = "";
});

describe("dropProtectedUiBreadcrumb", () => {
  it("drops a click inside a data-sentry-block region", () => {
    const host = mount(
      '<div data-sentry-block=""><button aria-label="Made Contoh">x</button></div>',
    );
    const button = host.querySelector("button");
    expect(
      dropProtectedUiBreadcrumb(
        { category: "ui.click", message: 'button[aria-label="Made Contoh"]' },
        click(button),
      ),
    ).toBeNull();
  });

  it("drops a click on an svg nested in a .sentry-block region, and a keypress there", () => {
    const host = mount(
      '<div class="sentry-block"><span><svg><path></path></svg></span><input /></div>',
    );
    expect(
      dropProtectedUiBreadcrumb(
        { category: "ui.click" },
        click(host.querySelector("path")),
      ),
    ).toBeNull();
    expect(
      dropProtectedUiBreadcrumb(
        { category: "ui.input" },
        click(host.querySelector("input")),
      ),
    ).toBeNull();
  });

  it("keeps clicks outside any protected region", () => {
    const host = mount('<nav><a href="/hr">People</a></nav>');
    const crumb = { category: "ui.click", message: "nav > a" };
    expect(
      dropProtectedUiBreadcrumb(crumb, click(host.querySelector("a"))),
    ).toBe(crumb);
  });

  it("keeps non-ui breadcrumbs and ui breadcrumbs without a DOM target", () => {
    const nav = { category: "navigation", data: { to: "/hr" } };
    expect(dropProtectedUiBreadcrumb(nav)).toBe(nav);
    const bare = { category: "ui.click" };
    expect(dropProtectedUiBreadcrumb(bare)).toBe(bare);
    expect(dropProtectedUiBreadcrumb(bare, click(null))).toBe(bare);
  });
});

describe("sentry.client.config", () => {
  it("installs the protected-region filter as beforeBreadcrumb", async () => {
    await import("../../sentry.client.config");
    expect(initMock).toHaveBeenCalledTimes(1);
    expect(initMock.mock.calls[0][0].beforeBreadcrumb).toBe(
      dropProtectedUiBreadcrumb,
    );
  });
});
