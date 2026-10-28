import { cleanup, fireEvent, render } from "@testing-library/svelte";
import { afterEach, describe, expect, it } from "vitest";
import BlockList from "./BlockList.svelte";

const editor = (content) => ({ contract_version: "translation-block-editor-v1", content, fields: [] });

afterEach(cleanup);

describe("BlockList review mode", () => {
  it("passes aligned mode through and moves between groups in adjacent blocks", async () => {
    const first = {
      id: "block-1",
      order: 1,
      role: "paragraph",
      source: { editor: editor("Source.") },
      target: {
        active_version: 1,
        editor: editor("Cel."),
        validation: { valid: true, findings: [] },
        translator_notes: [],
        alignment: {
          origin: "model_reported",
          review_state: "needs_review",
          groups: [{
            order: 1,
            kind: "one_to_one",
            source_sentences: [{ order: 1, text: "Source." }],
            target_sentences: [{ order: 1, text: "Cel." }]
          }]
        }
      },
      reader_notes: [],
      review: { decision: "pending", version: 0 }
    };
    const blocks = [
      first,
      {
        ...first,
        id: "block-2",
        order: 2,
        source: { editor: editor("Next source.") },
        target: {
          ...first.target,
          editor: editor("Następny cel."),
          alignment: {
            ...first.target.alignment,
            groups: [{
              order: 1,
              kind: "one_to_one",
              source_sentences: [{ order: 1, text: "Next source." }],
              target_sentences: [{ order: 1, text: "Następny cel." }]
            }]
          }
        }
      }
    ];
    const view = render(BlockList, { props: { blocks, totalBlocks: 2, reviewMode: "aligned_sentences" } });
    expect(view.container.querySelector('[data-block-id="block-1"]')?.getAttribute("data-review-mode")).toBe("aligned_sentences");
    expect(view.getAllByRole("img", { name: /Model-reported pairing/ })).toHaveLength(2);
    const rows = [...view.container.querySelectorAll('[data-alignment-group-row="true"]')];
    rows[0].focus();
    await fireEvent.keyDown(rows[0], { key: "ArrowDown" });
    expect(document.activeElement).toBe(rows[1]);
  });
});
