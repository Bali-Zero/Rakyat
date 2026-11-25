import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

// Publishing from the composer opens a pull request; the article is on the
// site only after the checks pass and the site is deployed. The page said
// "Published! Article is live." and kept a bare link under Publish that named
// no article, so after a second compose it sat under another article.
//
// These tests pin what the page SAYS. They also pin what this change leaves
// alone: the request carries no key, and no control changes its state.
const apiMock = vi.hoisted(() => ({
  getStatus: vi.fn(),
  getPublishStatus: vi.fn(),
  compose: vi.fn(),
  publish: vi.fn(),
}));
const toasts = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));

vi.mock("@/lib/api/articles.api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api/articles.api")>()),
  articlesApi: apiMock,
}));
vi.mock("@/lib/logger", () => ({
  logger: {
    error: vi.fn(),
    warn: vi.fn(),
    info: vi.fn(),
    debug: vi.fn(),
    componentMount: vi.fn(),
  },
}));
vi.mock("@/components/ui/toast", () => ({ useToast: () => toasts }));

import type {
  ComposeResponse,
  EnrichedArticle,
  PublishResponse,
} from "@/lib/api/articles.api";
import { STRINGS } from "@/lib/strings";
import ArticleComposerPage from "./page";

function article(headline: string, facts = "zz facts"): EnrichedArticle {
  return {
    title: headline,
    headline,
    tldr: { what: "zz what", who: "zz who", when: "zz when" },
    facts,
    bali_zero_take: {
      hidden_insight: "zz insight",
      our_analysis: "zz analysis",
      our_advice: "zz advice",
    },
    next_steps: { expat: ["zz expat step"], investor: ["zz investor step"] },
    category: "business",
    priority: "medium",
    relevance_score: 80,
    ai_summary: "zz summary",
    ai_tags: ["zz-tag"],
    suggested_components: [],
    cover_image: null,
    source: "zz source",
    source_url: null,
    enriched_at: "2026-11-20T00:00:00Z",
  };
}

function composed(value: EnrichedArticle): ComposeResponse {
  return { success: true, article: value, error: null, api_cost_cents: 12 };
}

function opened(pullRequest: number | null, url: string): PublishResponse {
  return {
    success: true,
    message: "zz server message",
    article_url: url,
    pull_request_number: pullRequest,
    auto_merge_enabled: pullRequest !== null,
  };
}

/** A request that stays out until the test settles it. */
function held<T>(): { promise: Promise<T>; settle: (value: T) => void } {
  let settle!: (value: T) => void;
  const promise = new Promise<T>((resolve) => {
    settle = resolve;
  });
  return { promise, settle };
}

const FIRST = "zz first article";
const SECOND = "zz second article";
const FIRST_URL = "https://balizero.com/business/zz-first-article";
const SECOND_URL = "https://balizero.com/business/zz-second-article";
const NOT_LIVE_WITH_CHECKS =
  "Not live yet: the article appears on the site after the checks pass and the site is deployed.";

/** Fill the form and press Compose. The form's title is not the headline. */
async function pressCompose(): Promise<void> {
  const button = await screen.findByRole("button", {
    name: /Compose Executive Brief/,
  });
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.change(
    screen.getByPlaceholderText(STRINGS.articleComposer.titlePlaceholder),
    { target: { value: "zz raw title" } },
  );
  fireEvent.change(
    screen.getByPlaceholderText(STRINGS.articleComposer.contentPlaceholder),
    { target: { value: "zz raw content" } },
  );
  fireEvent.click(button);
}

/** Compose an article and hand back the Publish button of the result. */
async function compose(value: EnrichedArticle): Promise<HTMLElement> {
  apiMock.compose.mockResolvedValueOnce(composed(value));
  await pressCompose();
  const publish = await screen.findByRole("button", {
    name: /Publish Article/,
  });
  await screen.findByText(value.headline);
  return publish;
}

function sentLine(): string {
  const label = screen.getByText(/Last sent for publication/);
  return label.parentElement?.textContent ?? "";
}

function positionSelect(): HTMLElement {
  const selects = screen.getAllByRole("combobox");
  return selects[selects.length - 1];
}

function coverInput(): HTMLInputElement {
  const input = document.querySelector<HTMLInputElement>('input[type="file"]');
  if (!input) throw new Error("the cover input is not on the page");
  return input;
}

function everySuccessToast(): string {
  return JSON.stringify(toasts.success.mock.calls);
}

describe("what the article composer says about a publication", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    apiMock.getStatus.mockResolvedValue({ configured: true });
    apiMock.getPublishStatus.mockResolvedValue({ configured: true });
  });

  it("says sent for publication, names the article and the pull request, and never says live", async () => {
    render(<ArticleComposerPage />);
    const publish = await compose(article(FIRST));
    toasts.success.mockClear();
    apiMock.publish.mockResolvedValueOnce(opened(4321, FIRST_URL));

    fireEvent.click(publish);

    await waitFor(() =>
      expect(toasts.success).toHaveBeenCalledWith(
        "Sent for publication",
        `"${FIRST}": pull request #4321 opened. ${NOT_LIVE_WITH_CHECKS}`,
      ),
    );
    expect(toasts.success).toHaveBeenCalledTimes(1);
    expect(everySuccessToast()).not.toMatch(/is live|Published!/i);
    expect(document.body.textContent).not.toMatch(/is live|Published:/i);
  });

  it("says not live yet when the answer names no pull request", async () => {
    render(<ArticleComposerPage />);
    const publish = await compose(article(FIRST));
    toasts.success.mockClear();
    apiMock.publish.mockResolvedValueOnce(opened(null, FIRST_URL));

    fireEvent.click(publish);

    await waitFor(() =>
      expect(toasts.success).toHaveBeenCalledWith(
        "Sent for publication",
        `"${FIRST}". Not live yet: the article appears on the site after the next deploy.`,
      ),
    );
    expect(everySuccessToast()).not.toMatch(/is live|Published!/i);
  });

  it("still sends the article, the position and no slug, with its publication key", async () => {
    render(<ArticleComposerPage />);
    const publish = await compose(article(FIRST));
    apiMock.publish.mockResolvedValueOnce(opened(4321, FIRST_URL));

    fireEvent.click(publish);

    await waitFor(() => expect(apiMock.publish).toHaveBeenCalledTimes(1));
    const request = apiMock.publish.mock.calls[0][0];
    expect(request).toEqual({
      article: article(FIRST),
      position: "normal",
      slug: undefined,
      publication_key: expect.stringMatching(/^composer-/),
    });
    expect(Object.keys(request).sort()).toEqual([
      "article",
      "position",
      "publication_key",
      "slug",
    ]);
  });

  it("sends the cover with the request when one was chosen", async () => {
    render(<ArticleComposerPage />);
    const publish = await compose(article(FIRST));
    const cover = new File(["zz image bytes"], "zz-cover.png", {
      type: "image/png",
    });
    fireEvent.change(coverInput(), { target: { files: [cover] } });
    apiMock.publish.mockResolvedValueOnce(opened(4321, FIRST_URL));

    fireEvent.click(publish);

    await waitFor(() => expect(apiMock.publish).toHaveBeenCalledTimes(1));
    const request = apiMock.publish.mock.calls[0][0];
    expect(request.cover_image_filename).toBe("zz-cover.png");
    expect(typeof request.cover_image_base64).toBe("string");
    expect(request.cover_image_base64.length).toBeGreaterThan(0);
    expect(request.publication_key).toMatch(/^composer-/);
  });

  it("names the article and the address under Publish, and leaves every control usable", async () => {
    render(<ArticleComposerPage />);
    const publish = await compose(article(FIRST));
    apiMock.publish.mockResolvedValueOnce(opened(4321, FIRST_URL));

    fireEvent.click(publish);

    const link = await screen.findByRole("link", { name: FIRST_URL });
    expect(link).toHaveAttribute("href", FIRST_URL);
    expect(sentLine()).toBe(
      `Last sent for publication: "${FIRST}". Live once deployed: ${FIRST_URL}`,
    );
    await waitFor(() => expect(publish).toBeEnabled());
    expect(screen.getByRole("button", { name: "Edit" })).toBeEnabled();
    expect(screen.getByPlaceholderText("auto-generated-slug")).toBeEnabled();
    expect(positionSelect()).toBeEnabled();
    expect(coverInput()).toBeEnabled();
    expect(
      screen.getByRole("button", { name: /Compose Executive Brief/ }),
    ).toBeEnabled();
  });

  it("changes no control while a publication is on its way, beyond Publish itself as before", async () => {
    render(<ArticleComposerPage />);
    const publish = await compose(article(FIRST));
    const out = held<PublishResponse>();
    apiMock.publish.mockReturnValueOnce(out.promise);

    fireEvent.click(publish);

    await waitFor(() => expect(publish).toBeDisabled());
    expect(publish).toHaveTextContent("Publishing...");
    expect(screen.getByRole("button", { name: "Edit" })).toBeEnabled();
    expect(screen.getByPlaceholderText("auto-generated-slug")).toBeEnabled();
    expect(positionSelect()).toBeEnabled();
    expect(coverInput()).toBeEnabled();
    expect(
      screen.getByRole("button", { name: /Compose Executive Brief/ }),
    ).toBeEnabled();
    expect(screen.queryByText(/Last sent for publication/)).toBeNull();

    out.settle(opened(4321, FIRST_URL));
    await waitFor(() => expect(publish).toBeEnabled());
  });

  it("names the article that was sent when another one is on screen by the time the answer comes", async () => {
    render(<ArticleComposerPage />);
    const publish = await compose(article(FIRST));
    toasts.success.mockClear();
    const out = held<PublishResponse>();
    apiMock.publish.mockReturnValueOnce(out.promise);
    fireEvent.click(publish);
    await waitFor(() => expect(apiMock.publish).toHaveBeenCalledTimes(1));

    apiMock.compose.mockResolvedValueOnce(composed(article(SECOND)));
    await pressCompose();
    await screen.findByText(SECOND);
    toasts.success.mockClear();
    out.settle(opened(4321, FIRST_URL));

    await screen.findByRole("link", { name: FIRST_URL });
    expect(sentLine()).toBe(
      `Last sent for publication: "${FIRST}". Live once deployed: ${FIRST_URL}`,
    );
    expect(toasts.success).toHaveBeenCalledWith(
      "Sent for publication",
      `"${FIRST}": pull request #4321 opened. ${NOT_LIVE_WITH_CHECKS}`,
    );
    expect(screen.getByText(SECOND)).toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /Publish Article/ }),
      ).toBeEnabled(),
    );
  });

  it("names the edited article that was sent when a compose started during the edit lands first", async () => {
    render(<ArticleComposerPage />);
    await compose(article(FIRST));
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByDisplayValue(FIRST), {
      target: { value: `${FIRST} edited` },
    });

    const composing = held<ComposeResponse>();
    apiMock.compose.mockReturnValueOnce(composing.promise);
    await pressCompose();
    await waitFor(() => expect(apiMock.compose).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    toasts.success.mockClear();
    const out = held<PublishResponse>();
    apiMock.publish.mockReturnValueOnce(out.promise);
    fireEvent.click(
      await screen.findByRole("button", { name: /Publish Article/ }),
    );
    await waitFor(() => expect(apiMock.publish).toHaveBeenCalledTimes(1));
    expect(apiMock.publish.mock.calls[0][0].article.headline).toBe(
      `${FIRST} edited`,
    );

    composing.settle(composed(article(SECOND)));
    await screen.findByText(SECOND);
    toasts.success.mockClear();
    out.settle(opened(4321, FIRST_URL));

    await screen.findByRole("link", { name: FIRST_URL });
    expect(sentLine()).toBe(
      `Last sent for publication: "${FIRST} edited". Live once deployed: ${FIRST_URL}`,
    );
    expect(toasts.success).toHaveBeenCalledWith(
      "Sent for publication",
      `"${FIRST} edited": pull request #4321 opened. ${NOT_LIVE_WITH_CHECKS}`,
    );
    expect(screen.getByText(SECOND)).toBeInTheDocument();
  });

  it("shows the last one that was sent, and a refusal changes nothing of it", async () => {
    render(<ArticleComposerPage />);
    apiMock.publish.mockResolvedValueOnce(opened(4321, FIRST_URL));
    fireEvent.click(await compose(article(FIRST)));
    await screen.findByRole("link", { name: FIRST_URL });

    apiMock.publish.mockResolvedValueOnce(opened(null, SECOND_URL));
    fireEvent.click(await compose(article(SECOND)));
    await screen.findByRole("link", { name: SECOND_URL });
    expect(sentLine()).toBe(
      `Last sent for publication: "${SECOND}". Live once deployed: ${SECOND_URL}`,
    );
    expect(screen.queryByRole("link", { name: FIRST_URL })).toBeNull();

    const reason = "Protected repository path already exists: zz/path.mdx";
    apiMock.publish.mockResolvedValueOnce({
      success: false,
      message: "zz refused",
      error: reason,
    });
    const publish = screen.getByRole("button", { name: /Publish Article/ });
    await waitFor(() => expect(publish).toBeEnabled());
    fireEvent.click(publish);

    await waitFor(() =>
      expect(toasts.error).toHaveBeenCalledWith("Publish Error", reason),
    );
    apiMock.publish.mockRejectedValueOnce(new Error("Request timeout"));
    await waitFor(() => expect(publish).toBeEnabled());
    fireEvent.click(publish);
    await waitFor(() =>
      expect(toasts.error).toHaveBeenCalledWith(
        "Publish Error",
        "Request timeout",
      ),
    );
    expect(sentLine()).toBe(
      `Last sent for publication: "${SECOND}". Live once deployed: ${SECOND_URL}`,
    );
    await waitFor(() => expect(publish).toBeEnabled());
  });

  it("is a record of the last request, not a mark on the article on screen", async () => {
    // Two different articles can share a headline, and with it the default
    // address. The line then reads the same under the second one, which was
    // not sent: Publish stays usable and sends the second article.
    render(<ArticleComposerPage />);
    apiMock.publish.mockResolvedValueOnce(opened(4321, FIRST_URL));
    fireEvent.click(await compose(article(FIRST, "zz facts of the first")));
    await screen.findByRole("link", { name: FIRST_URL });

    const publish = await compose(article(FIRST, "zz facts of the second"));
    expect(sentLine()).toBe(
      `Last sent for publication: "${FIRST}". Live once deployed: ${FIRST_URL}`,
    );
    await waitFor(() => expect(publish).toBeEnabled());
    apiMock.publish.mockResolvedValueOnce(opened(4322, FIRST_URL));
    fireEvent.click(publish);

    await waitFor(() => expect(apiMock.publish).toHaveBeenCalledTimes(2));
    expect(apiMock.publish.mock.calls[1][0].article.facts).toBe(
      "zz facts of the second",
    );
  });
});
