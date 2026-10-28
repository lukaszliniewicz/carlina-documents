import { cleanup, fireEvent, render, waitFor } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import BlockCard from "./BlockCard.svelte";

const editor = (content) => ({
  contract_version: "translation-block-editor-v1",
  content,
  fields: []
});

const block = {
  id: "block-1",
  order: 1,
  role: "heading",
  source: { editor: editor("א Heading") },
  target: {
    active_version: 2,
    editor: editor("א Heading"),
    validation: { valid: true, findings: [] },
    translator_notes: []
  },
  reader_notes: [],
  review: { decision: "pending", version: 0 }
};

afterEach(cleanup);

describe("BlockCard contextual annotations", () => {
  it("shows changed approval as stale and records coverage independently", async () => {
    const onCoverage = vi.fn();
    const onReview = vi.fn();
    const evidence = { complete: false, coverage_ids: [], reviewed_source_anchor_ids: [], reviewed_source_anchor_count: 0 };
    const view = render(BlockCard, { props: { block: { ...block, review: { decision: "approved", effective_decision: "pending", stale: true, version: 1, current_target_version: 2, coverage: { structural: evidence, target_reading: evidence, bilingual: evidence } } }, onCoverage, onReview } });
    const review = view.getByRole("button", { name: "Review block 1: Translation changed since review" });
    expect(review.classList.contains("approved")).toBe(false);
    await fireEvent.click(review);
    await fireEvent.click(view.getByRole("button", { name: "Compared with source" }));
    expect(onCoverage).toHaveBeenCalledWith("bilingual");
    expect(onReview).not.toHaveBeenCalled();
  });
  it("reviews sentence splits and merges as focusable groups before opening the block editor", async () => {
    const aligned = {
      ...block,
      target: {
        ...block.target,
        alignment: {
          origin: "model_reported",
          review_state: "needs_review",
          groups: [
            {
              order: 1,
              kind: "split",
              source_sentences: [{ order: 1, text: "A long source sentence." }],
              target_sentences: [{ order: 1, text: "Pierwsze zdanie." }, { order: 2, text: "Drugie zdanie." }]
            },
            {
              order: 2,
              kind: "one_to_one",
              source_sentences: [{ order: 2, text: "Another sentence." }],
              target_sentences: [{ order: 3, text: "Kolejne zdanie." }]
            }
          ]
        }
      }
    };
    const view = render(BlockCard, { props: { block: aligned, accessRole: "owner", reviewMode: "aligned_sentences" } });

    expect(view.container.querySelector('[data-block-id="block-1"]')?.getAttribute("data-review-mode")).toBe("aligned_sentences");
    expect(view.getByRole("img", { name: /Model-reported pairing/ })).toBeTruthy();
    expect(view.container.querySelector(".keyboard-hint, .alignment-heading, .review-controls")).toBeNull();
    expect(view.getByText("Pierwsze zdanie.")).toBeTruthy();
    expect(view.queryByTestId("source-target-comparison")).toBeNull();
    const groups = [...view.container.querySelectorAll('[data-alignment-group-row="true"]')];
    groups[0].focus();
    await fireEvent.keyDown(groups[0], { key: "ArrowDown" });
    expect(document.activeElement).toBe(groups[1]);

    await fireEvent.click(view.getByRole("button", { name: "Edit or annotate block" }));
    expect(view.getByTestId("source-target-comparison")).toBeTruthy();
    expect(view.getByRole("button", { name: "Return to sentence groups" })).toBeTruthy();
  });

  it.each([
    ["automatic_fallback", /Automatically derived pairing/],
    ["carried_forward", /Sentence pairing retained after editing/]
  ])("labels %s alignment without claiming a new model response", (origin, label) => {
    const aligned = {
      ...block,
      target: {
        ...block.target,
        alignment: {
          origin,
          review_state: "needs_review",
          groups: [{
            order: 1,
            kind: "one_to_one",
            source_sentences: [{ order: 1, text: "א Heading" }],
            target_sentences: [{ order: 1, text: "א Heading" }]
          }]
        }
      }
    };
    const view = render(BlockCard, { props: { block: aligned, accessRole: "viewer", readOnly: true, reviewMode: "aligned_sentences" } });
    expect(view.getByRole("img", { name: label })).toBeTruthy();
    expect(view.getByRole("button", { name: "View formatted block" })).toBeTruthy();
  });

  it("opens notes and review actions from the header without losing focus or review status", async () => {
    const onReview = vi.fn();
    const notedBlock = { ...block, reader_notes: [{ id: "note-1", version: 1, state: "needs_review", body: "Context", anchor: { kind: "whole_block" } }] };
    const view = render(BlockCard, { props: { block: notedBlock, onReview } });
    const notes = view.getByRole("button", { name: "Notes for block 1: 1, review needed" });
    const panel = view.container.querySelector("details.notes-editor");
    expect(panel.open).toBe(false);
    expect(notes.closest("header")).toBeTruthy();
    await fireEvent.click(notes);
    expect(panel.open).toBe(true);
    expect(notes.getAttribute("aria-expanded")).toBe("true");
    await fireEvent.click(notes);
    expect(panel.open).toBe(false);

    const review = view.getByRole("button", { name: "Review block 1: Pending" });
    expect(view.queryByRole("button", { name: "Approved", exact: true })).toBeNull();
    await fireEvent.click(review);
    await waitFor(() => expect(document.activeElement).toBe(view.getByRole("button", { name: "Approved", exact: true })));
    await fireEvent.keyDown(document.activeElement, { key: "Escape" });
    expect(document.activeElement).toBe(review);
    expect(review.getAttribute("aria-expanded")).toBe("false");
    await fireEvent.click(review);
    await fireEvent.click(view.getByRole("button", { name: "Needs changes", exact: true }));
    expect(onReview).toHaveBeenCalledWith("needs_changes");
    expect(document.activeElement).toBe(review);
    await view.rerender({ block: { ...notedBlock, review: { decision: "needs_changes", version: 1 } }, onReview });
    expect(view.getByRole("button", { name: "Review block 1: Needs changes" }).classList.contains("needs-review")).toBe(true);
    await fireEvent.click(review);
    await fireEvent.pointerDown(document.body);
    expect(review.getAttribute("aria-expanded")).toBe("false");
  });

  it("prefills a source-anchored note from the source selection", async () => {
    const view = render(BlockCard, { props: { block, accessRole: "owner" } });
    const source = view.container.querySelector('[data-note-anchor="source"]');
    await waitFor(() => expect(source?.textContent).toBe("א Heading"));
    const range = document.createRange();
    range.setStart(source.firstChild, 0);
    range.setEnd(source.firstChild, 1);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);

    await fireEvent.mouseUp(source);
    await fireEvent.click(view.getByRole("button", { name: "Add note to selected source text" }));

    expect(view.getByLabelText("Attach to").value).toBe("source");
    expect(view.getByLabelText("Exact phrase").value).toBe("א");
    expect(view.getByLabelText(/Occurrence/).value).toBe("0");
    expect(document.activeElement).toBe(view.getByLabelText("Reader-facing note"));
  });
});
