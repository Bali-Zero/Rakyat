import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { ArticleEditor } from "./ArticleEditor";
import { intelligenceApi, type StagingItem } from "@/lib/api/intelligence.api";

const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));

vi.mock("@/lib/api/intelligence.api");
vi.mock("@/lib/logger");
vi.mock("@/components/ui/toast", () => ({ useToast: () => toast }));
vi.mock("@/components/ui/select", () => ({
  Select: ({
    value,
    onValueChange,
    children,
  }: {
    value?: string;
    onValueChange?: (value: string) => void;
    children?: ReactNode;
  }) => (
    <select
      value={value ?? ""}
      onChange={(event) => onValueChange?.(event.target.value)}
    >
      {children}
    </select>
  ),
  SelectTrigger: ({ children }: { children?: ReactNode }) => <>{children}</>,
  SelectValue: () => null,
  SelectContent: ({ children }: { children?: ReactNode }) => <>{children}</>,
  SelectItem: ({
    value,
    children,
  }: {
    value: string;
    children?: ReactNode;
  }) => <option value={value}>{children}</option>,
}));

const item: StagingItem = {
  id: "synthetic-news-1",
  type: "news",
  title: "Original title",
  content: "  body  ",
  category: undefined,
  status: "pending",
  detected_at: "2026-01-01T00:00:00Z",
  source: "Synthetic source",
  detection_type: "NEW",
};

function renderEditor(overrides: Partial<StagingItem> = {}) {
  const onOpenChange = vi.fn();
  const onSaved = vi.fn();
  render(
    <ArticleEditor
      item={{ ...item, ...overrides }}
      open
      onOpenChange={onOpenChange}
      onSaved={onSaved}
    />,
  );
  return { onOpenChange, onSaved };
}

async function save() {
  await userEvent.click(screen.getByRole("button", { name: /save changes/i }));
}

describe("ArticleEditor", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(intelligenceApi.editItem).mockResolvedValue({
      success: true,
      message: "saved",
      id: item.id,
      updated_fields: {},
    });
  });

  it("sends only a changed title when category is unset", async () => {
    renderEditor();
    await userEvent.clear(screen.getByLabelText("Title *"));
    await userEvent.type(screen.getByLabelText("Title *"), "Updated title");

    await save();

    await waitFor(() =>
      expect(intelligenceApi.editItem).toHaveBeenCalledWith("news", item.id, {
        title: "Updated title",
      }),
    );
  });

  it("sends only trimmed changed content", async () => {
    renderEditor();
    await userEvent.clear(screen.getByLabelText("Content (Markdown)"));
    await userEvent.type(
      screen.getByLabelText("Content (Markdown)"),
      "  revised body  ",
    );

    await save();

    await waitFor(() =>
      expect(intelligenceApi.editItem).toHaveBeenCalledWith("news", item.id, {
        content: "revised body",
      }),
    );
  });

  it("sends only a changed category", async () => {
    renderEditor();
    await userEvent.selectOptions(screen.getByRole("combobox"), "visa");

    await save();

    await waitFor(() =>
      expect(intelligenceApi.editItem).toHaveBeenCalledWith("news", item.id, {
        category: "visa",
      }),
    );
  });

  it("closes without requesting or saving when nothing changed", async () => {
    const { onOpenChange, onSaved } = renderEditor();

    await save();

    expect(intelligenceApi.editItem).not.toHaveBeenCalled();
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("keeps Save disabled and sends nothing while the title is empty", async () => {
    renderEditor();
    await userEvent.clear(screen.getByLabelText("Title *"));

    expect(
      screen.getByRole("button", { name: /save changes/i }),
    ).toBeDisabled();
    await save();

    expect(intelligenceApi.editItem).not.toHaveBeenCalled();
  });

  it("sends title and content together when both changed, and no category", async () => {
    renderEditor({ category: "property" });
    await userEvent.clear(screen.getByLabelText("Title *"));
    await userEvent.type(screen.getByLabelText("Title *"), "  Both  ");
    await userEvent.clear(screen.getByLabelText("Content (Markdown)"));
    await userEvent.type(screen.getByLabelText("Content (Markdown)"), "new");

    await save();

    await waitFor(() =>
      expect(intelligenceApi.editItem).toHaveBeenCalledWith("news", item.id, {
        title: "Both",
        content: "new",
      }),
    );
  });

  it("calls onSaved and closes after a successful changed title save", async () => {
    const { onOpenChange, onSaved } = renderEditor();
    await userEvent.clear(screen.getByLabelText("Title *"));
    await userEvent.type(screen.getByLabelText("Title *"), "Updated title");

    await save();

    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});
