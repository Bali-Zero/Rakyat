import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

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

function article(headline: string): EnrichedArticle {
  return {
    title: headline,
    headline,
    tldr: { what: "zz what", who: "zz who", when: "zz when" },
    facts: "zz facts",
    bali_zero_take: {
      hidden_insight: "zz insight",
      our_analysis: "zz analysis",
      our_advice: "zz advice",
    },
    next_steps: { expat: ["zz expat"], investor: ["zz investor"] },
    category: "business",
    priority: "medium",
    relevance_score: 80,
    ai_summary: "zz summary",
    ai_tags: ["zz tag"],
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

function opened(): PublishResponse {
  return {
    success: true,
    message: "zz",
    article_url: "https://example.invalid/zz",
    pull_request_number: 12,
    auto_merge_enabled: true,
  };
}

async function compose(value: EnrichedArticle) {
  apiMock.compose.mockResolvedValueOnce(composed(value));
  const button = await screen.findByRole("button", {
    name: /Compose Executive Brief/,
  });
  fireEvent.change(
    screen.getByPlaceholderText(STRINGS.articleComposer.titlePlaceholder),
    { target: { value: "zz title" } },
  );
  fireEvent.change(
    screen.getByPlaceholderText(STRINGS.articleComposer.contentPlaceholder),
    { target: { value: "zz content" } },
  );
  fireEvent.click(button);
  await screen.findByText(value.headline);
  return screen.getByRole("button", { name: /Publish Article/ });
}

function publishKeys(): string[] {
  return apiMock.publish.mock.calls.map(([request]) => request.publication_key);
}

describe("article composer drafts", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    apiMock.getStatus.mockResolvedValue({ configured: true });
    apiMock.getPublishStatus.mockResolvedValue({ configured: true });
  });

  it("keeps the minted key through retries and edits, then replaces it after a landed compose", async () => {
    render(<ArticleComposerPage />);
    const publish = await compose(article("zz first"));
    apiMock.publish.mockResolvedValueOnce({
      success: false,
      message: "zz failed",
    });
    fireEvent.click(publish);
    await waitFor(() => expect(apiMock.publish).toHaveBeenCalledTimes(1));
    fireEvent.click(publish);
    await waitFor(() => expect(apiMock.publish).toHaveBeenCalledTimes(2));
    expect(publishKeys()[0]).toBe(publishKeys()[1]);
    expect(publishKeys()[0]).toMatch(/^composer-[0-9a-f-]{36}$/);

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByDisplayValue("zz first"), {
      target: { value: "zz changed" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    apiMock.publish.mockResolvedValueOnce(opened());
    fireEvent.click(publish);
    await waitFor(() => expect(apiMock.publish).toHaveBeenCalledTimes(3));
    expect(publishKeys()[2]).toBe(publishKeys()[0]);
    apiMock.publish.mockResolvedValueOnce(opened());
    fireEvent.click(publish);
    await waitFor(() => expect(apiMock.publish).toHaveBeenCalledTimes(4));
    expect(publishKeys()[3]).toBe(publishKeys()[0]);

    const recomposed = await compose(article("zz changed"));
    apiMock.publish.mockResolvedValueOnce(opened());
    fireEvent.click(recomposed);
    await waitFor(() => expect(apiMock.publish).toHaveBeenCalledTimes(5));
    expect(publishKeys()[4]).not.toBe(publishKeys()[0]);
  });

  it("drops a completed edit when a replacement composition lands, but preserves it on failure", async () => {
    render(<ArticleComposerPage />);
    await compose(article("zz first"));
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByDisplayValue("zz first"), {
      target: { value: "zz draft edit" },
    });
    await compose(article("zz second"));
    expect(screen.getByText("zz second")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByDisplayValue("zz second"), {
      target: { value: "zz retained edit" },
    });
    apiMock.compose.mockResolvedValueOnce({
      success: false,
      article: null,
      error: "zz failed",
      api_cost_cents: 0,
    });
    fireEvent.click(
      screen.getByRole("button", { name: /Compose Executive Brief/ }),
    );
    await waitFor(() => expect(toasts.error).toHaveBeenCalled());
    expect(screen.getByDisplayValue("zz retained edit")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
  });

  it("saves an edit kept through a failed compose under the key it was edited from", async () => {
    render(<ArticleComposerPage />);
    const publish = await compose(article("zz kept"));
    apiMock.publish.mockResolvedValueOnce({ success: false, message: "zz" });
    fireEvent.click(publish);
    await waitFor(() => expect(apiMock.publish).toHaveBeenCalledTimes(1));
    const key = publishKeys()[0];
    expect(key).toMatch(/^composer-[0-9a-f-]{36}$/);

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByDisplayValue("zz kept"), {
      target: { value: "zz kept edited" },
    });
    apiMock.compose.mockResolvedValueOnce({
      success: false,
      article: null,
      error: "zz failed",
      api_cost_cents: 0,
    });
    fireEvent.click(
      screen.getByRole("button", { name: /Compose Executive Brief/ }),
    );
    await waitFor(() => expect(toasts.error).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    apiMock.publish.mockResolvedValueOnce(opened());
    fireEvent.click(screen.getByRole("button", { name: /Publish Article/ }));
    await waitFor(() => expect(apiMock.publish).toHaveBeenCalledTimes(2));
    expect(apiMock.publish.mock.calls[1][0].article.headline).toBe(
      "zz kept edited",
    );
    expect(publishKeys()[1]).toBe(key);
  });

  it("keeps raw tags and steps editable while publishing only parsed entries", async () => {
    render(<ArticleComposerPage />);
    const publish = await compose(article("zz first"));
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const tags = screen.getByLabelText("AI Tags");
    fireEvent.change(tags, { target: { value: "alpha, beta," } });
    expect(tags).toHaveValue("alpha, beta,");
    fireEvent.change(tags, { target: { value: "alpha, beta" } });
    expect(tags).toHaveValue("alpha, beta");
    fireEvent.change(tags, { target: { value: "alpha, , beta, " } });
    const steps = screen.getAllByPlaceholderText("One per line...")[0];
    fireEvent.change(steps, { target: { value: "first\n\nsecond" } });
    expect(steps).toHaveValue("first\n\nsecond");
    const investor = screen.getAllByPlaceholderText("One per line...")[1];
    fireEvent.change(investor, { target: { value: " third \n\n fourth " } });
    expect(investor).toHaveValue(" third \n\n fourth ");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    apiMock.publish.mockResolvedValueOnce(opened());
    fireEvent.click(publish);
    await waitFor(() => expect(apiMock.publish).toHaveBeenCalledTimes(1));
    expect(apiMock.publish.mock.calls[0][0].article.ai_tags).toEqual([
      "alpha",
      "beta",
    ]);
    expect(apiMock.publish.mock.calls[0][0].article.next_steps.expat).toEqual([
      "first",
      "second",
    ]);
    expect(
      apiMock.publish.mock.calls[0][0].article.next_steps.investor,
    ).toEqual(["third", "fourth"]);
  });

  it("starts each edit from the article, not from abandoned raw text", async () => {
    render(<ArticleComposerPage />);
    await compose(article("zz reentry"));
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("AI Tags"), {
      target: { value: "abandoned," },
    });
    fireEvent.change(screen.getAllByPlaceholderText("One per line...")[1], {
      target: { value: "abandoned\n" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByLabelText("AI Tags")).toHaveValue("zz tag");
    expect(screen.getAllByPlaceholderText("One per line...")[1]).toHaveValue(
      "zz investor",
    );
  });

  it("normalizes every editorial list, including general, before publishing", async () => {
    render(<ArticleComposerPage />);
    const value = article("zz unedited");
    value.ai_tags = [" alpha ", "", "beta"];
    value.next_steps = {
      expat: [" first ", " "],
      investor: ["", " second "],
      general: [" one ", ""],
    };
    const publish = await compose(value);
    apiMock.publish.mockResolvedValueOnce(opened());
    fireEvent.click(publish);
    await waitFor(() => expect(apiMock.publish).toHaveBeenCalledTimes(1));
    const sent = apiMock.publish.mock.calls[0][0].article;
    expect(sent.ai_tags).toEqual(["alpha", "beta"]);
    expect(sent.next_steps).toEqual({
      expat: ["first"],
      investor: ["second"],
      general: ["one"],
    });

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    apiMock.publish.mockResolvedValueOnce(opened());
    fireEvent.click(publish);
    await waitFor(() => expect(apiMock.publish).toHaveBeenCalledTimes(2));
    expect(apiMock.publish.mock.calls[1][0].article.next_steps).toEqual(
      sent.next_steps,
    );
  });

  it("keeps the key across a rejected request and publication controls", async () => {
    render(<ArticleComposerPage />);
    const publish = await compose(article("zz controls"));
    apiMock.publish.mockRejectedValueOnce(new Error("zz timeout"));
    fireEvent.click(publish);
    await waitFor(() => expect(apiMock.publish).toHaveBeenCalledTimes(1));
    const key = publishKeys()[0];
    expect(key).toMatch(/^composer-[0-9a-f-]{36}$/);

    fireEvent.change(screen.getByPlaceholderText("auto-generated-slug"), {
      target: { value: "zz-custom-slug" },
    });
    apiMock.publish.mockResolvedValueOnce(opened());
    fireEvent.click(publish);
    await waitFor(() => expect(apiMock.publish).toHaveBeenCalledTimes(2));
    expect(apiMock.publish.mock.calls[1][0].slug).toBe("zz-custom-slug");
    expect(publishKeys()[1]).toBe(key);

    const selects = screen.getAllByRole("combobox");
    fireEvent.click(selects[selects.length - 1]);
    fireEvent.click(await screen.findByText("Homepage Hero Main"));
    apiMock.publish.mockResolvedValueOnce(opened());
    fireEvent.click(publish);
    await waitFor(() => expect(apiMock.publish).toHaveBeenCalledTimes(3));
    expect(apiMock.publish.mock.calls[2][0].position).toBe("hero_main");
    expect(publishKeys()[2]).toBe(key);

    const cover = new File(["zz image"], "zz-cover.png", { type: "image/png" });
    const input =
      document.querySelector<HTMLInputElement>('input[type="file"]');
    if (!input) throw new Error("cover input missing");
    fireEvent.change(input, { target: { files: [cover] } });
    apiMock.publish.mockResolvedValueOnce(opened());
    fireEvent.click(publish);
    await waitFor(() => expect(apiMock.publish).toHaveBeenCalledTimes(4));
    expect(apiMock.publish.mock.calls[3][0].cover_image_filename).toBe(
      "zz-cover.png",
    );
    expect(publishKeys()[3]).toBe(key);

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    apiMock.publish.mockResolvedValueOnce(opened());
    fireEvent.click(publish);
    await waitFor(() => expect(apiMock.publish).toHaveBeenCalledTimes(5));
    expect(publishKeys()[4]).toBe(key);
  });

  it("does not add general when the composition does not have it", async () => {
    render(<ArticleComposerPage />);
    const publish = await compose(article("zz no general"));
    apiMock.publish.mockResolvedValueOnce(opened());
    fireEvent.click(publish);
    await waitFor(() => expect(apiMock.publish).toHaveBeenCalledTimes(1));
    expect(
      apiMock.publish.mock.calls[0][0].article.next_steps,
    ).not.toHaveProperty("general");
  });

  it("keeps an in-flight request on its original composition key", async () => {
    render(<ArticleComposerPage />);
    const firstPublish = await compose(article("zz first race"));
    let resolveFirst: (value: PublishResponse) => void;
    apiMock.publish.mockReturnValueOnce(
      new Promise<PublishResponse>((resolve) => {
        resolveFirst = resolve;
      }),
    );
    fireEvent.click(firstPublish);
    await waitFor(() => expect(apiMock.publish).toHaveBeenCalledTimes(1));
    const firstKey = publishKeys()[0];
    expect(firstKey).toMatch(/^composer-[0-9a-f-]{36}$/);

    const second = article("zz second race");
    apiMock.compose.mockResolvedValueOnce(composed(second));
    fireEvent.click(
      screen.getByRole("button", { name: /Compose Executive Brief/ }),
    );
    await screen.findByText(second.headline);
    resolveFirst!(opened());
    await waitFor(() => expect(toasts.success).toHaveBeenCalled());
    const secondPublish = screen.getByRole("button", {
      name: /Publish Article/,
    });
    apiMock.publish.mockResolvedValueOnce(opened());
    fireEvent.click(secondPublish);
    await waitFor(() => expect(apiMock.publish).toHaveBeenCalledTimes(2));
    expect(publishKeys()[1]).toMatch(/^composer-[0-9a-f-]{36}$/);
    expect(publishKeys()[1]).not.toBe(firstKey);
  });

  it("publishes a save made during a compose under the edited article's key, then the landed one under its own", async () => {
    render(<ArticleComposerPage />);
    const publish = await compose(article("zz race a"));
    apiMock.publish.mockResolvedValueOnce({ success: false, message: "zz" });
    fireEvent.click(publish);
    await waitFor(() => expect(apiMock.publish).toHaveBeenCalledTimes(1));
    const keyA = publishKeys()[0];
    expect(keyA).toMatch(/^composer-[0-9a-f-]{36}$/);

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByDisplayValue("zz race a"), {
      target: { value: "zz race a edited" },
    });
    let land!: (value: ComposeResponse) => void;
    apiMock.compose.mockReturnValueOnce(
      new Promise<ComposeResponse>((resolve) => {
        land = resolve;
      }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: /Compose Executive Brief/ }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    apiMock.publish.mockResolvedValueOnce(opened());
    fireEvent.click(screen.getByRole("button", { name: /Publish Article/ }));
    await waitFor(() => expect(apiMock.publish).toHaveBeenCalledTimes(2));
    expect(apiMock.publish.mock.calls[1][0].article.headline).toBe(
      "zz race a edited",
    );
    expect(publishKeys()[1]).toBe(keyA);

    land(composed(article("zz race b")));
    await screen.findByText("zz race b");
    apiMock.publish.mockResolvedValueOnce(opened());
    fireEvent.click(screen.getByRole("button", { name: /Publish Article/ }));
    await waitFor(() => expect(apiMock.publish).toHaveBeenCalledTimes(3));
    expect(publishKeys()[2]).toMatch(/^composer-[0-9a-f-]{36}$/);
    expect(publishKeys()[2]).not.toBe(keyA);
  });

  it.each([
    "Existing publication pull request fingerprint does not match",
    "Existing publication pull request head does not match its fingerprint",
    "Existing publication branch fingerprint does not match",
  ])("adds the verification guidance only to %s", async (error) => {
    render(<ArticleComposerPage />);
    const publish = await compose(article("zz fingerprint"));
    apiMock.publish.mockResolvedValueOnce({
      success: false,
      message: "zz",
      error,
    });

    fireEvent.click(publish);

    await waitFor(() => expect(toasts.error).toHaveBeenCalledTimes(1));
    expect(toasts.error).toHaveBeenCalledWith(
      "Publish Error",
      `${error} The existing GitHub publication for this composition could not be verified against this request (the publish date counts). Check its pull request or branch before publishing again.`,
    );
  });

  it.each([
    "Protected repository path already exists: apps/mouth/src/content/articles/business/fingerprint-guide.mdx",
    "Publication pull request was closed without merge; manual review required",
    "Failed to publish fingerprint",
  ])("does not add verification guidance to %s", async (error) => {
    render(<ArticleComposerPage />);
    const publish = await compose(article("zz other refusal"));
    apiMock.publish.mockResolvedValueOnce({
      success: false,
      message: "zz",
      error,
    });

    fireEvent.click(publish);

    await waitFor(() =>
      expect(toasts.error).toHaveBeenCalledWith("Publish Error", error),
    );
  });
});
