/**
 * The /login contract, on concept-K "SIAP".
 *
 * Three things are pinned here, and each one is pinned with a planted
 * violation AND an innocent case, because a guard that has only ever seen
 * green cannot be shown to be looking (cicatrix #3).
 *
 *   1. sameOriginPath  — the open redirect stays closed.
 *   2. stageForError   — a 500 is never reported as a wrong PIN.
 *   3. no red, no copper FILL — the alphabet's law, read off the rendered DOM.
 */
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { hydrateRoot } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterAll, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const { mockLogin, mockLoggerError, mockLoggerWarn } = vi.hoisted(() => ({
  mockLogin: vi.fn(),
  mockLoggerError: vi.fn(),
  mockLoggerWarn: vi.fn(),
}));

vi.mock("@/lib/api", () => ({ api: { login: mockLogin } }));

vi.mock("@/lib/logger", () => ({
  logger: {
    error: mockLoggerError,
    warn: mockLoggerWarn,
    info: vi.fn(),
    debug: vi.fn(),
  },
}));

import { ApiError } from "@/lib/api/error-handler";
import LoginPage from "./page";
import {
  CREDENTIAL_FIELDS,
  firstPartyRedirect,
  stageForError,
  withoutCredentialFields,
} from "./contract";
import LoginError from "./error";
import LoginLoading from "./loading";

/**
 * Reds this surface must never show, as SHAPES and not as a list of four
 * literals. A red can arrive as a hex (long or short), as an rgb() triple, as
 * a Tailwind palette class, or as a shadcn `destructive` token, and a guard
 * that only knows the four values someone happened to type is a guard that has
 * not been shown to look (cicatrix #3).
 *
 * `var(--state-danger)` is banned too, even though the kita theme resolves it
 * to copper. The alphabet's rule is that this surface never NAMES danger: it
 * names the person who has to act, in copper, with a word. A component that
 * reaches for the danger token is writing in the wrong language even when the
 * pixels come out right — and on any other product those pixels are red.
 */
const RED_TOKENS = [
  "text-destructive",
  "bg-destructive",
  "border-destructive",
  "var(--state-danger)",
  "var(--bz-red",
];
const RED_CLASS = /\b(?:bg|text|border|ring|from|to|via)-red-\d{2,3}\b/g;
const RED_HEX =
  /#(?:[0-9a-f]{2})(?:[0-9a-f]{2})(?:[0-9a-f]{2})\b|#[0-9a-f]{3}\b/gi;
const RED_RGB = /rgba?\(\s*\d+\s*,\s*\d+\s*,\s*\d+(?:\s*,\s*[\d.]+)?\s*\)/g;

/** Is this colour a red — dominant red channel, clearly not the copper steps? */
export function isRed(value: string): boolean {
  let r: number, g: number, b: number;
  const hex = value.trim().toLowerCase();
  if (hex.startsWith("#")) {
    const h =
      hex.length === 4
        ? hex
            .slice(1)
            .split("")
            .map((c) => c + c)
            .join("")
        : hex.slice(1, 7);
    if (h.length !== 6) return false;
    [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  } else {
    const m = value.match(/\d+/g);
    if (!m || m.length < 3) return false;
    [r, g, b] = m.slice(0, 3).map(Number);
  }
  // The two copper steps and the forest/slate/ink of the alphabet are not red.
  const COPPER = [
    [164, 75, 54],
    [196, 106, 82],
  ];
  if (COPPER.some(([cr, cg, cb]) => r === cr && g === cg && b === cb)) {
    return false;
  }
  return r > 120 && r - g > 70 && r - b > 70;
}

/** Pure detector: every forbidden red this markup carries. */
export function findReds(markup: string): string[] {
  const hits = RED_TOKENS.filter((token) => markup.includes(token));
  hits.push(...(markup.match(RED_CLASS) ?? []));
  for (const raw of [
    ...(markup.match(RED_HEX) ?? []),
    ...(markup.match(RED_RGB) ?? []),
  ]) {
    if (isRed(raw)) hits.push(raw);
  }
  return [...new Set(hits)];
}

/**
 * Pure detector: a copper FILL behind a label. Copper is a person, not a
 * status, and it is never the ground a word sits on. A copper BORDER, a copper
 * RULE (the 3px masthead mark) and copper TEXT are all legal.
 *
 * It reads INLINE STYLES as well as classes, because this page fills its
 * button through `style={{ background: ... }}`: a guard that only knew the
 * Tailwind spelling would have watched the one place a copper fill could
 * actually appear and seen nothing.
 */
const COPPER_RULE =
  /h-\[3px\] w-14 rounded-sm bg-\[var\((?:--bz-copper|--bz-panel-link)\)\]/g;
const COPPER_VALUE = /var\(--bz-copper[a-z-]*\)|#a44b36|#c46a52/i;

export function findCopperFill(markup: string): string[] {
  const withoutRule = markup.replace(COPPER_RULE, "");
  const hits: string[] = [
    ...(withoutRule.match(/bg-\[var\(--bz-copper[a-z-]*\)\]/g) ?? []),
  ];
  for (const style of withoutRule.match(/style="[^"]*"/g) ?? []) {
    for (const decl of style.slice(7, -1).split(";")) {
      const [prop, value] = decl.split(":");
      if (!value) continue;
      if (
        /^\s*(background|background-color)\s*$/.test(prop) &&
        COPPER_VALUE.test(value)
      ) {
        hits.push(decl.trim());
      }
    }
  }
  return hits;
}

/**
 * Pure detector: the theatre, read off the SOURCE. The overlays, the sound and
 * the motion are gone from the file, not merely absent from one render — a DOM
 * assertion could pass simply because the plate that carried them was not on
 * screen. Guilt is the implementation this PR replaces.
 */
const THEATRE = [
  "useSystemSound",
  "framer-motion",
  "ACCESS GRANTED",
  "ACCESS DENIED",
  "REDIRECT_DELAY_MS",
  "ERROR_RESET_DELAY_MS",
];

export function findTheatre(source: string): string[] {
  // The doc comment says what was removed, so only CODE is judged: block
  // comments are dropped before the search.
  const code = source.replace(/\/\*[\s\S]*?\*\//g, "");
  return THEATRE.filter((sign) => code.includes(sign));
}

const redirectReplace = vi.fn();
const realLocation = globalThis.location;

/**
 * jsdom's `location.replace` is read-only and its Location is unforgeable, so
 * the method cannot be spied on. The `location` PROPERTY of the global is
 * configurable, though, so the whole object is swapped for the two fields this
 * page actually reads. Restored after the file so nothing else inherits it.
 */
function atUrl(search: string) {
  Object.defineProperty(globalThis, "location", {
    configurable: true,
    value: {
      href: `http://localhost/login${search}`,
      pathname: "/login",
      origin: "http://localhost",
      search,
      replace: redirectReplace,
    },
  });
}

afterAll(() => {
  Object.defineProperty(globalThis, "location", {
    configurable: true,
    value: realLocation,
  });
});

beforeEach(() => {
  vi.clearAllMocks();
  redirectReplace.mockClear();
  atUrl("");
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}"));
});

const ORIGIN = "https://kita.balizero.com";

/**
 * The vectors arrive as they are WRITTEN in the query string, and are read the
 * way page.tsx reads them — through URLSearchParams, which percent-decodes.
 * That decode is half the defect: `?redirect=/%09/evil.test` becomes the
 * characters `/`, TAB, `/`, `evil.test`, which begins with one slash, carries
 * no leading `//`, and passes every prefix test that can be written. The
 * browser then drops the TAB and resolves it to https://evil.test/.
 *
 * Reproducing the decode here is the point: a test that hands the function an
 * already-encoded `%09` is testing a string the function never sees.
 */
function fromQuery(written: string): string | null {
  return new URLSearchParams(`redirect=${written}`).get("redirect");
}

/** Accepted: [written, expected, why]. */
const ACCEPTED: ReadonlyArray<readonly [string, string, string]> = [
  ["/dashboard?tab=1#x", "/dashboard?tab=1#x", "a path, query and hash kept"],
  [
    `${ORIGIN}/clients/42`,
    "/clients/42",
    "same origin absolute — the PATH is returned, so the browser does not reload through its own hostname",
  ],
  [
    `${ORIGIN}/clients?filter=mine#top`,
    "/clients?filter=mine#top",
    "same origin absolute with query and hash",
  ],
  [
    "https://admin.balizero.com/clients/42",
    "https://admin.balizero.com/clients/42",
    "apps/admin-dashboard/middleware.ts bounces here with exactly this",
  ],
  [
    "https://mail.balizero.com/inbox",
    "https://mail.balizero.com/inbox",
    "the SSO bounce proxy.ts documents",
  ],
  ["https://balizero.com/x", "https://balizero.com/x", "the apex itself"],
  ["/", "/", "the root"],
];

/** Refused: [written, why]. */
const REFUSED: ReadonlyArray<readonly [string, string]> = [
  ["/%09/evil.test", "TAB, which the browser strips before parsing"],
  ["/%0A/evil.test", "LF, the same"],
  ["/%0D/evil.test", "CR, the same"],
  ["//evil.test", "protocol-relative"],
  ["/\\evil.test", "backslash, normalised into protocol-relative"],
  ["\\\\evil.test", "double backslash"],
  ["javascript:alert(1)", "not https, and not our origin"],
  ["data:text/html,x", "not https, and not our origin"],
  ["https://evil.test/x", "another origin"],
  ["https://balizero.com.evil.test/", "look-alike host"],
  ["https://balizero.com@evil.test/", "userinfo trick — the host is evil.test"],
  ["https://evil.test\\@balizero.com/", "the same trick with a backslash"],
  [
    "http://admin.balizero.com/x",
    "a SIBLING must be https; only our own origin may be plain http",
  ],
  [
    "http://kita.balizero.com/x",
    "our own hostname over http is not our origin",
  ],
  ["/login?expired=true", "no loop back into the gate"],
  ["", "empty"],
];

describe("firstPartyRedirect — resolve first, then judge the parsed URL", () => {
  for (const [written, expected, why] of ACCEPTED) {
    it(`accepts ${written} — ${why} (innocence)`, () => {
      expect(firstPartyRedirect(fromQuery(written), ORIGIN)).toBe(expected);
    });
  }

  for (const [written, why] of REFUSED) {
    it(`refuses ${JSON.stringify(written)} — ${why} (guilt)`, () => {
      expect(firstPartyRedirect(fromQuery(written), ORIGIN)).toBeNull();
    });
  }

  it("refuses nothing at all", () => {
    expect(firstPartyRedirect(null, ORIGIN)).toBeNull();
  });

  it("the decode the vectors depend on is real, not assumed", () => {
    // If this ever stops being true the guilt cases above stop being guilty,
    // and the guard would be proven against inputs nobody can send.
    expect(fromQuery("/%09/evil.test")).toBe("/\t/evil.test");
    expect(fromQuery("/%0A/evil.test")).toBe("/\n/evil.test");
    expect(fromQuery("/%0D/evil.test")).toBe("/\r/evil.test");
  });

  it("the prefix guard this replaces would have passed all three (guilt)", () => {
    // The exact test #6502, #6503 and #6505 shipped: starts with "/", not
    // "//", not "/\". It is true for every whitespace vector.
    const prefixGuard = (raw: string) =>
      raw.startsWith("/") && !raw.startsWith("//") && !raw.startsWith("/\\");
    for (const v of ["/%09/evil.test", "/%0A/evil.test", "/%0D/evil.test"]) {
      expect(prefixGuard(fromQuery(v)!)).toBe(true);
      expect(firstPartyRedirect(fromQuery(v), ORIGIN)).toBeNull();
    }
  });

  it("a dev server on http is its own origin and still works", () => {
    // The one place http is legitimate: when it IS the origin we are on.
    const dev = "http://localhost:3000";
    expect(firstPartyRedirect("/clients", dev)).toBe("/clients");
    expect(firstPartyRedirect(`${dev}/clients`, dev)).toBe("/clients");
    // And a sibling over http is still refused there.
    expect(firstPartyRedirect("http://admin.balizero.com/x", dev)).toBeNull();
  });

  it("returns the RESOLVED path, never the caller's string", () => {
    // The same target written two ways collapses to one answer, which is the
    // property a string test cannot have.
    expect(firstPartyRedirect("/a/../clients", ORIGIN)).toBe("/clients");
    expect(firstPartyRedirect(`${ORIGIN}/a/../clients`, ORIGIN)).toBe(
      "/clients",
    );
  });
});

describe("stageForError — a broken service is not a wrong PIN", () => {
  it("every 4xx is about what was sent (guilt for the old one-state page)", () => {
    expect(stageForError(new ApiError("no", 401))).toBe("denied");
    expect(stageForError(new ApiError("no", 403))).toBe("denied");
    // identity/router.py answers 400 when the PIN is not 4-8 digits, and
    // pydantic answers 422 on the address. Both are "look at what you typed".
    expect(stageForError(new ApiError("bad pin", 400))).toBe("denied");
    expect(stageForError(new ApiError("bad email", 422))).toBe("denied");
  });

  it("5xx, 408 and 429 are the service (innocence)", () => {
    expect(stageForError(new ApiError("boom", 500))).toBe("unreachable");
    expect(stageForError(new ApiError("slow", 504))).toBe("unreachable");
    expect(stageForError(new ApiError("timeout", 408))).toBe("unreachable");
    expect(stageForError(new ApiError("busy", 429))).toBe("unreachable");
  });

  it("a network throw is the service, not the credentials", () => {
    expect(stageForError(new TypeError("Failed to fetch"))).toBe("unreachable");
    expect(stageForError("a string")).toBe("unreachable");
  });
});

describe("the detectors themselves", () => {
  it("findReds names a token red and clears innocent markup", () => {
    expect(findReds('<p class="text-destructive">x</p>')).toEqual([
      "text-destructive",
    ]);
    expect(findReds('<p class="text-[var(--bz-copper-text)]">x</p>')).toEqual(
      [],
    );
  });

  it("findReds catches a red it was never given, in every spelling", () => {
    // None of these four is in the token list: the detector judges the COLOUR.
    expect(findReds('<p style="color:#ff0000">x</p>')).toEqual(["#ff0000"]);
    expect(findReds('<p style="color:#f00">x</p>')).toEqual(["#f00"]);
    expect(findReds('<p style="color:rgb(220, 38, 38)">x</p>')).toEqual([
      "rgb(220, 38, 38)",
    ]);
    expect(findReds('<p class="bg-red-500">x</p>')).toEqual(["bg-red-500"]);
  });

  it("findReds does not accuse the alphabet's own colours (innocence)", () => {
    // Copper light, copper dark, forest, slate, paper and ink all pass.
    const alphabet =
      '<i style="color:#a44b36"></i><i style="color:#c46a52"></i>' +
      '<i style="color:#253e33"></i><i style="color:#233d52"></i>' +
      '<i style="color:rgb(247, 244, 238)"></i><i style="color:#1d2c3b"></i>';
    expect(findReds(alphabet)).toEqual([]);
  });

  it("findCopperFill names a planted fill and clears the legal rule", () => {
    const rule =
      '<div class="mb-5 h-[3px] w-14 rounded-sm bg-[var(--bz-copper)]"></div>';
    expect(findCopperFill(rule)).toEqual([]);
    expect(
      findCopperFill(
        rule + '<span class="bg-[var(--bz-copper)]">Blocked</span>',
      ),
    ).toEqual(["bg-[var(--bz-copper)]"]);
  });

  it("findCopperFill reads inline styles, which is where a fill would hide", () => {
    // This page fills its button through style={{ background }}, so the
    // Tailwind spelling alone would have guarded an empty room.
    expect(
      findCopperFill(
        '<button style="background: var(--bz-copper)">Enter</button>',
      ),
    ).toEqual(["background: var(--bz-copper)"]);
    expect(
      findCopperFill(
        '<button style="background: var(--bz-panel)">Enter</button>',
      ),
    ).toEqual([]);
    // A copper BORDER is the legal way to mark "needs you".
    expect(
      findCopperFill('<p style="border-color: var(--bz-copper)">x</p>'),
    ).toEqual([]);
  });

  it("findTheatre names the implementation this PR replaces (guilt)", () => {
    const before = `
      const sound = useSystemSound();
      import { motion } from "framer-motion";
      const REDIRECT_DELAY_MS = 1500;
      return <div>ACCESS GRANTED</div>;
    `;
    expect(findTheatre(before).sort()).toEqual([
      "ACCESS GRANTED",
      "REDIRECT_DELAY_MS",
      "framer-motion",
      "useSystemSound",
    ]);
  });

  it("findTheatre does not fire on the comment that records the removal", () => {
    expect(
      findTheatre("/* it used to play ACCESS GRANTED with useSystemSound */"),
    ).toEqual([]);
  });
});

describe("LoginPage — the five plates", () => {
  it("renders the idle plate: forest panel, copper rule, both fields", () => {
    const { container } = render(<LoginPage />);

    expect(screen.getByRole("heading", { name: "Welcome back." })).toBeTruthy();
    expect(screen.getByLabelText("Email")).toBeTruthy();
    // The PIN is digits on the backend, so a phone must offer the number pad.
    expect(screen.getByLabelText("PIN").getAttribute("inputmode")).toBe(
      "numeric",
    );
    expect(
      container.querySelector('[style*="var(--bz-panel)"]'),
    ).not.toBeNull();
    expect(
      container.querySelector(".h-\\[3px\\].w-14.rounded-sm"),
    ).not.toBeNull();
  });

  it("none of the theatre survives, in the source (innocence)", () => {
    const source = readFileSync(join(__dirname, "page.tsx"), "utf8");

    expect(findTheatre(source)).toEqual([]);
  });

  it("carries no red and no copper fill", () => {
    const { container } = render(<LoginPage />);

    expect(findReds(container.innerHTML)).toEqual([]);
    expect(findCopperFill(container.innerHTML)).toEqual([]);
  });

  it("a 401 says the credentials; a 500 says the service", async () => {
    mockLogin.mockRejectedValueOnce(new ApiError("bad", 401));
    const first = render(<LoginPage />);

    fireEvent.submit(first.container.querySelector("form")!);
    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toContain(
        "were not accepted",
      );
    });
    // The denial is copper and a sentence, never a red.
    expect(findReds(first.container.innerHTML)).toEqual([]);
    first.unmount();

    mockLogin.mockRejectedValueOnce(new ApiError("down", 500));
    const second = render(<LoginPage />);
    fireEvent.submit(second.container.querySelector("form")!);
    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toContain(
        "could not reach the service",
      );
    });
  });

  it("honours a same-origin ?redirect= (innocence)", async () => {
    atUrl("?redirect=/clients");
    mockLogin.mockResolvedValueOnce({ user: { name: "Ada", role: "staff" } });
    const { container } = render(<LoginPage />);

    fireEvent.submit(container.querySelector("form")!);
    await waitFor(() =>
      expect(redirectReplace).toHaveBeenCalledWith("/clients"),
    );
    expect(mockLoggerWarn).not.toHaveBeenCalled();
  });

  it("sends a staff member back to the sibling that bounced them here", async () => {
    // apps/admin-dashboard/middleware.ts and the SSO subdomains bounce to
    // kita.balizero.com/login?redirect=<their own absolute URL>. Landing those
    // people on /dashboard instead of where they were is the reason the
    // first-party branch exists.
    atUrl("?redirect=https://admin.balizero.com/clients/42");
    mockLogin.mockResolvedValueOnce({ user: { name: "Ada", role: "staff" } });
    const { container } = render(<LoginPage />);

    fireEvent.submit(container.querySelector("form")!);
    await waitFor(() =>
      expect(redirectReplace).toHaveBeenCalledWith(
        "https://admin.balizero.com/clients/42",
      ),
    );
    expect(mockLoggerWarn).not.toHaveBeenCalled();
  });

  it("the whitespace vector is refused through the page, not only the unit", async () => {
    // The end of the chain the spec was written for: the query as a browser
    // would send it, decoded by the page, resolved, refused, and the staff
    // member left on this origin.
    atUrl("?redirect=/%09/evil.test");
    mockLogin.mockResolvedValueOnce({ user: { name: "Ada", role: "staff" } });
    const { container } = render(<LoginPage />);

    fireEvent.submit(container.querySelector("form")!);
    await waitFor(() =>
      expect(redirectReplace).toHaveBeenCalledWith("/dashboard"),
    );
    expect(mockLoggerWarn).toHaveBeenCalledTimes(1);
  });

  it("refuses an off-Bali-Zero ?redirect= and says so in the log (guilt)", async () => {
    // The same parameter, pointing outside Bali Zero: the role destination
    // wins and the refusal is recorded. This is the open redirect, closed.
    atUrl("?redirect=https://evil.test");
    mockLogin.mockResolvedValueOnce({ user: { name: "Ada", role: "staff" } });
    const { container } = render(<LoginPage />);

    fireEvent.submit(container.querySelector("form")!);
    await waitFor(() =>
      expect(redirectReplace).toHaveBeenCalledWith("/dashboard"),
    );
    expect(mockLoggerWarn).toHaveBeenCalledTimes(1);
  });

  it("a client role still falls back to /portal", async () => {
    mockLogin.mockResolvedValueOnce({ user: { name: "Ada", role: "client" } });
    const { container } = render(<LoginPage />);

    fireEvent.submit(container.querySelector("form")!);
    await waitFor(() =>
      expect(redirectReplace).toHaveBeenCalledWith("/portal"),
    );
  });

  it("success has no plate, and the form stays locked while the browser goes", async () => {
    // Measured in Chromium: location.replace takes the window before React
    // commits, so a success plate would never paint. What must survive is the
    // lock — a second submit during navigation must not fire a second login.
    mockLogin.mockResolvedValue({ user: { name: "Ada", role: "staff" } });
    const { container } = render(<LoginPage />);

    fireEvent.submit(container.querySelector("form")!);
    await waitFor(() =>
      expect(redirectReplace).toHaveBeenCalledWith("/dashboard"),
    );
    expect(screen.getByRole("heading", { name: "Welcome back." })).toBeTruthy();
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByLabelText("Email").hasAttribute("disabled")).toBe(true);

    fireEvent.submit(container.querySelector("form")!);
    expect(mockLogin).toHaveBeenCalledTimes(1);
  });
});

describe("the boundary and the skeleton wear the same alphabet", () => {
  it("the error boundary is copper and carries the words, never a red", () => {
    const { container } = render(
      <LoginError error={new Error("x")} reset={vi.fn()} />,
    );

    expect(findReds(container.innerHTML)).toEqual([]);
    expect(findCopperFill(container.innerHTML)).toEqual([]);
    expect(
      screen.getByRole("heading", { name: "The sign-in page did not load." }),
    ).toBeTruthy();
  });

  it("the boundary's button still calls reset", () => {
    const reset = vi.fn();
    render(<LoginError error={new Error("x")} reset={reset} />);

    fireEvent.click(screen.getByRole("button", { name: /Try again/ }));
    expect(reset).toHaveBeenCalledTimes(1);
  });

  it("the skeleton predicts the login's own silhouette", () => {
    const { container } = render(<LoginLoading />);

    // The forest band, the copper rule and two 52px fields — the same numbers
    // the page uses, so nothing jumps when the real page arrives.
    expect(
      container.querySelector('[style*="var(--bz-panel)"]'),
    ).not.toBeNull();
    expect(container.querySelectorAll(".h-\\[52px\\]").length).toBe(2);
    expect(findReds(container.innerHTML)).toEqual([]);
  });
});

/**
 * The login is a forest page in the kita face, and it is the same forest
 * whichever host serves it. Guilt: the root without the kita attributes (the
 * editorial block would win on a non-kita host, Cormorant on night). Innocence:
 * the portal files stay byte-free of the new tokens.
 */
const globalsCss = readFileSync(join(__dirname, "..", "globals.css"), "utf8");

function kitaBlock(theme: string): string {
  const m = globalsCss.match(
    new RegExp(
      `\\[data-theme="${theme}"\\]\\[data-product="kita"\\]\\s*\\{([\\s\\S]*?)\\n\\}`,
    ),
  );
  if (!m) throw new Error(`kita ${theme} block missing`);
  return m[1];
}

function decl(block: string, name: string): string {
  const m = block.match(new RegExp(`${name}:\\s*([^;]+);`));
  if (!m) throw new Error(`${name} not declared`);
  return m[1].trim();
}

function contrastOf(a: string, b: string): number {
  const lum = (hex: string) => {
    const ch = [1, 3, 5].map((i) => {
      const v = parseInt(hex.slice(i, i + 2), 16) / 255;
      return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2];
  };
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

describe("LoginPage — forest ground, kita heading face", () => {
  it("the root is forest and pins the kita product, so the face does not depend on the host", () => {
    const { container } = render(<LoginPage />);
    const root = container.firstElementChild as HTMLElement;

    expect(root.getAttribute("data-product")).toBe("kita");
    expect(root.getAttribute("data-theme")).toBe("operative-light");
    expect(root.className).toContain("bg-[var(--bz-panel)]");
    expect(root.className).not.toContain("bg-[var(--bz-base)]");
    expect(container.querySelector("main")?.className).not.toMatch(/\bbg-/);
  });

  it("--bz-panel is the rail forest, in both kita themes", () => {
    for (const theme of ["operative-light", "operative-dark"]) {
      const block = kitaBlock(theme);
      expect(decl(block, "--bz-panel")).toBe(decl(block, "--bz-kita-rail"));
    }
  });

  it("both headings resolve --font-serif, which the kita blocks point at IBM Plex Sans", () => {
    const { container } = render(<LoginPage />);
    const heads = [
      screen.getByRole("heading", { name: "Welcome back." }),
      screen.getByText("kita."),
    ];
    for (const el of heads) {
      expect(el.getAttribute("style")).toContain("var(--font-serif)");
    }
    expect(container.innerHTML).not.toMatch(/Fraunces|Cormorant|Georgia/);
    for (const theme of ["operative-light", "operative-dark"]) {
      expect(decl(kitaBlock(theme), "--font-serif")).toMatch(
        /^"IBM Plex Sans"/,
      );
    }
  });

  it("guilt: the pre-fix root (no product attribute) would not have pinned kita", () => {
    const source = readFileSync(join(__dirname, "page.tsx"), "utf8");
    const stripped = source.replace(/data-product="kita"/g, "");
    expect(stripped.includes('data-product="kita"')).toBe(false);
    expect(source.includes('data-product="kita"')).toBe(true);
  });

  it("every foreground on the page is AA on the forest, computed", () => {
    const block = kitaBlock("operative-light");
    const forest = decl(block, "--bz-panel");
    const onPanel = decl(block, "--bz-on-panel");
    const muted = decl(block, "--bz-on-panel-muted");
    const line = decl(block, "--bz-panel-line");
    const link = decl(block, "--bz-panel-link");

    expect(contrastOf(onPanel, forest)).toBeGreaterThanOrEqual(4.5);
    expect(contrastOf(muted, forest)).toBeGreaterThanOrEqual(4.5);
    expect(contrastOf(link, forest)).toBeGreaterThanOrEqual(4.5);
    expect(contrastOf(line, forest)).toBeGreaterThanOrEqual(3);
    expect(contrastOf(forest, onPanel)).toBeGreaterThanOrEqual(4.5);
    // innocence of the detector: the old copper link would have failed.
    expect(contrastOf(decl(block, "--bz-copper-text"), forest)).toBeLessThan(3);
  });

  it("the page names no raw colour and no longer reads the paper tokens", () => {
    const code = readFileSync(join(__dirname, "page.tsx"), "utf8").replace(
      /\/\*[\s\S]*?\*\//g,
      "",
    );
    expect(code).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
    expect(code).not.toMatch(/--tx-secondary|--tx-pure|--bz-copper/);
  });

  it("the portal login and its theme block are untouched", () => {
    const portalLogin = join(__dirname, "..", "portal");
    const portalBlock = globalsCss.match(
      /\[data-theme="operative-light"\]\[data-product="my"\]\s*\{[\s\S]*?\n\}/,
    )![0];
    expect(portalBlock).not.toContain("--bz-panel-link");
    expect(portalBlock).toContain('"Fraunces"');
    expect(readFileSync(join(portalLogin, "r19-fonts.css"), "utf8")).toContain(
      "Fraunces",
    );
  });
});

/**
 * The credentials never travel in the address.
 *
 * The form had no `method`, so a submit made by the browser itself, before
 * React owned the page, was a GET to `/login?email=…&pin=…`. Every value below
 * is invented.
 */
describe("credentials stay out of the address", () => {
  const serverMarkup = () => {
    const host = document.createElement("div");
    host.innerHTML = renderToString(<LoginPage />);
    return host;
  };

  it("withoutCredentialFields removes both fields and keeps the rest (guilt)", () => {
    expect(
      withoutCredentialFields(
        "https://kita.example/login?email=a%40example.com&redirect=%2Fclients&pin=135790#top",
      ),
    ).toBe("/login?redirect=%2Fclients#top");
    expect(
      withoutCredentialFields("https://kita.example/login?pin=135790"),
    ).toBe("/login");
    expect(
      withoutCredentialFields(
        "https://kita.example/login?email=a%40example.com",
      ),
    ).toBe("/login");
  });

  it("withoutCredentialFields leaves an address without them alone (innocence)", () => {
    expect(
      withoutCredentialFields("https://kita.example/login?redirect=%2Fclients"),
    ).toBeNull();
    expect(withoutCredentialFields("https://kita.example/login")).toBeNull();
    // A key that merely contains the word is not the field.
    expect(
      withoutCredentialFields("https://kita.example/login?pinned=1&emails=2"),
    ).toBeNull();
    expect(withoutCredentialFields("not an address")).toBeNull();
  });

  it("the server's markup is a POST form that cannot be submitted yet (guilt for the method-less form)", () => {
    const host = serverMarkup();
    const form = host.querySelector("form")!;
    expect(form.getAttribute("method")).toBe("post");
    expect(form.getAttribute("action")).toBeNull();
    const button = host.querySelector('button[type="submit"]');
    expect(button).not.toBeNull();
    expect(button).toBeDisabled();
    // One submit button and no other control that could submit the form.
    expect(form.querySelectorAll("button, input[type=submit]")).toHaveLength(1);
    // The fields are the two names a GET would have written into the query.
    expect(
      Array.from(form.querySelectorAll("input")).map((input) => input.name),
    ).toEqual([...CREDENTIAL_FIELDS]);
  });

  it("the button is enabled once the page is interactive (innocence)", async () => {
    render(<LoginPage />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Enter" })).toBeEnabled(),
    );
  });

  it("an address that already carries the fields is rewritten without them", async () => {
    const replaceState = vi.spyOn(globalThis.history, "replaceState");
    atUrl("?email=a%40example.com&pin=135790&redirect=%2Fclients");
    render(<LoginPage />);
    await waitFor(() => expect(replaceState).toHaveBeenCalledTimes(1));
    expect(replaceState.mock.calls[0][2]).toBe("/login?redirect=%2Fclients");
    replaceState.mockRestore();
  });

  it("an address without the fields is not rewritten (innocence)", async () => {
    const replaceState = vi.spyOn(globalThis.history, "replaceState");
    atUrl("?redirect=%2Fclients");
    render(<LoginPage />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Enter" })).toBeEnabled(),
    );
    expect(replaceState).not.toHaveBeenCalled();
    replaceState.mockRestore();
  });

  it("what was typed before React took over is what is sent, by a request and not by the address", async () => {
    const host = serverMarkup();
    document.body.appendChild(host);
    (host.querySelector("#email") as HTMLInputElement).value =
      "early@example.com";
    (host.querySelector("#pin") as HTMLInputElement).value = "246810";
    mockLogin.mockResolvedValueOnce({ user: { name: "Ada", role: "staff" } });

    let root: ReturnType<typeof hydrateRoot> | undefined;
    await act(async () => {
      root = hydrateRoot(host, <LoginPage />);
    });
    const button = host.querySelector('button[type="submit"]')!;
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.submit(host.querySelector("form")!);

    await waitFor(() =>
      expect(mockLogin).toHaveBeenCalledWith("early@example.com", "246810"),
    );
    await waitFor(() =>
      expect(redirectReplace).toHaveBeenCalledWith("/dashboard"),
    );
    await act(async () => root?.unmount());
    host.remove();
  });
});
