import { fireEvent, render } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";
import WorkspaceSegmentEditor from "./WorkspaceSegmentEditor.svelte";

const blocks = [{
  key: "1:p",
  label: "Paragraph",
  segments: [{ id: 1, status: "translated", highlighted_source_text: "Source" }]
}];

describe("WorkspaceSegmentEditor", () => {
  it("keeps guest workspaces read-only and does not invoke mutation callbacks", async () => {
    const callbacks = {
      input: vi.fn(), focus: vi.fn(), blur: vi.fn(), merge: vi.fn(), split: vi.fn(), translate: vi.fn(), status: vi.fn()
    };
    const { getByRole, queryByRole } = render(WorkspaceSegmentEditor, {
      props: {
        blocks,
        guestMode: true,
        onDraftInput: callbacks.input,
        onDraftFocus: callbacks.focus,
        onDraftBlur: callbacks.blur,
        onMerge: callbacks.merge,
        onSplit: callbacks.split,
        onTranslate: callbacks.translate,
        onStatus: callbacks.status
      }
    });
    const textbox = getByRole("textbox");
    expect(textbox.readOnly).toBe(true);
    expect(queryByRole("button", { name: "Merge" })).toBeNull();
    expect(queryByRole("button", { name: "Split" })).toBeNull();
    expect(queryByRole("button", { name: "Translate" })).toBeNull();
    await fireEvent.focus(textbox);
    await fireEvent.input(textbox, { target: { value: "attempt" } });
    await fireEvent.blur(textbox);
    expect(Object.values(callbacks).every((callback) => callback.mock.calls.length === 0)).toBe(true);
  });

  it("renders reader-facing translator notes separately from QA context", () => {
    const notedBlocks = [{
      key: "1:p",
      label: "Paragraph",
      segments: [{
        id: 1,
        status: "translated",
        highlighted_source_text: "Source",
        context_note: "Review this ambiguity.",
        translator_notes: [{
          id: "note-1",
          anchor_text: "Hanshi Festival",
          body: "A traditional observance associated with abstaining from fire."
        }]
      }]
    }];
    const { getByRole, getByText } = render(WorkspaceSegmentEditor, {
      props: { blocks: notedBlocks, drafts: { 1: "Target" } }
    });

    expect(getByRole("complementary", { name: "Translator's notes" })).toBeTruthy();
    expect(getByText(/Translator.s note — Hanshi Festival:/)).toBeTruthy();
    expect(getByText("Review this ambiguity.")).toBeTruthy();
  });
});
