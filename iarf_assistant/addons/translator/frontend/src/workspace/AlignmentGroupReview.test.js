import { cleanup, fireEvent, render, waitFor } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import AlignmentGroupReview from "./AlignmentGroupReview.svelte";

afterEach(cleanup);

function setup(overrides = {}) {
  const onSentenceEdit = vi.fn(async () => ({ ok: true }));
  const onEditingChange = vi.fn();
  const props = {
    blockId: "block-1", targetVersion: 4, editable: true, onSentenceEdit, onEditingChange,
    alignment: { groups: [
      { order: 1, kind: "one_to_one", source_sentences: [{ order: 1, text: "Source one." }], target_sentences: [{ order: 1, text: "  A 😀 sentence. " }] },
      { order: 2, kind: "one_to_one", source_sentences: [{ order: 2, text: "Source two." }], target_sentences: [{ order: 2, text: "Second sentence." }] }
    ] },
    ...overrides
  };
  return { ...render(AlignmentGroupReview, { props }), props, onSentenceEdit, onEditingChange };
}

describe("sentence editing", () => {
  it("edits wording with the opened version and keeps segmentation explicit", async () => {
    const view = setup();
    await fireEvent.click(view.getByRole("button", { name: "Edit translated sentence 1" }));
    const editor = view.getByRole("textbox");
    expect(editor.value).toBe("A 😀 sentence.");
    await fireEvent.input(editor, { target: { value: "Now two. Still one segment." } });
    await fireEvent.keyDown(editor, { key: "Enter", ctrlKey: true });
    expect(view.onSentenceEdit).toHaveBeenCalledWith({ action: "replace", sentence_order: 1, text: "Now two. Still one segment." }, 4);
    await waitFor(() => expect(view.queryByRole("textbox")).toBeNull());
    expect(view.onEditingChange.mock.calls).toEqual([[true], [false]]);
  });

  it("splits at a Unicode character offset and requires a nonempty second part", async () => {
    const view = setup();
    await fireEvent.click(view.getByRole("button", { name: "Edit translated sentence 1" }));
    const editor = view.getByRole("textbox");
    expect(view.getByRole("button", { name: "Split at cursor" }).disabled).toBe(true);
    editor.setSelectionRange(5, 5); // A, space, surrogate pair, space = four characters.
    await fireEvent.select(editor);
    await fireEvent.click(view.getByRole("button", { name: "Split at cursor" }));
    expect(view.onSentenceEdit).toHaveBeenCalledWith({ action: "split", sentence_order: 1, text: "A 😀 sentence.", offset: 4 }, 4);
  });

  it("merges only unchanged wording with the next sentence", async () => {
    const view = setup();
    await fireEvent.click(view.getByRole("button", { name: "Edit translated sentence 1" }));
    await fireEvent.input(view.getByRole("textbox"), { target: { value: "Changed." } });
    expect(view.getByRole("button", { name: "Merge with next" }).disabled).toBe(true);
    await fireEvent.input(view.getByRole("textbox"), { target: { value: "A 😀 sentence." } });
    await fireEvent.click(view.getByRole("button", { name: "Merge with next" }));
    expect(view.onSentenceEdit).toHaveBeenCalledWith({ action: "merge_next", sentence_order: 1 }, 4);
    await waitFor(() => expect(view.queryByRole("textbox")).toBeNull());
    await fireEvent.click(view.getByRole("button", { name: "Edit translated sentence 2" }));
    expect(view.getByRole("button", { name: "Merge with next" }).disabled).toBe(true);
  });

  it("retains a failed draft and its version when the block changes", async () => {
    const onSentenceEdit = vi.fn(async () => ({ ok: false, error: "A newer translation is available." }));
    const view = setup({ onSentenceEdit });
    await fireEvent.click(view.getByRole("button", { name: "Edit translated sentence 1" }));
    await fireEvent.input(view.getByRole("textbox"), { target: { value: "My draft." } });
    await view.rerender({ ...view.props, targetVersion: 5 });
    await fireEvent.click(view.getByRole("button", { name: "Save", exact: true }));
    expect(onSentenceEdit).toHaveBeenCalledWith({ action: "replace", sentence_order: 1, text: "My draft." }, 4);
    expect((await view.findByRole("alert")).textContent).toContain("A newer translation");
    expect(view.getByRole("textbox").value).toBe("My draft.");
    await fireEvent.keyDown(view.getByRole("textbox"), { key: "Escape" });
    expect(view.queryByRole("textbox")).toBeNull();
  });

  it("offers no edit controls in read-only mode", () => {
    const view = setup({ editable: false });
    expect(view.queryByRole("button", { name: /Edit translated sentence/ })).toBeNull();
    expect(view.getByText("Second sentence.")).toBeTruthy();
  });
});
