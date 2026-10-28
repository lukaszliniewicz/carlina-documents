import { fireEvent, render } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";
import QADiagnosticsDialog from "./QADiagnosticsDialog.svelte";

describe("QADiagnosticsDialog", () => {
  it("shows deterministic findings, source pages, and navigation actions", async () => {
    const onrefresh = vi.fn();
    const onloadmore = vi.fn();
    const onnavigate = vi.fn();
    const payload = {
      summary: {
        total: 3,
        by_severity: { integrity: 1, warning: 1, advisory: 1 }
      },
      capabilities: {
        spellcheck: { available: true, requested_language: "en", dictionary: "en_US" }
      },
      source_page_urls: { "7": "/source/page/7" },
      pagination: { next_cursor: "next" },
      items: [
        {
          id: "finding-1",
          code: "citation-key-mutated-or-missing",
          category: "references",
          severity: "integrity",
          message: "A source citation key is not preserved exactly in the target.",
          block_id: "block-3",
          order: 3,
          source_pages: [7],
          source: { text: "[Eingabe, 1845]", span: [0, 15], match: "[Eingabe, 1845]" },
          evidence: { source_key: "[Eingabe, 1845]" }
        },
        {
          id: "finding-2",
          code: "spelling-suspect",
          category: "spelling",
          severity: "warning",
          message: "The target dictionary does not recognize this token.",
          block_id: "block-4",
          order: 4,
          source_pages: [],
          target: { text: "ofthe", span: [0, 5], match: "ofthe" },
          evidence: { suggestions: ["of the"] }
        }
      ]
    };

    const view = render(QADiagnosticsDialog, {
      props: { open: true, payload, onrefresh, onloadmore, onnavigate }
    });

    expect(view.getByText((_, element) => element?.textContent === "3 findings")).toBeTruthy();
    expect(view.getByText("Showing 2 of 3 findings")).toBeTruthy();
    expect(view.getByText("citation-key-mutated-or-missing · references")).toBeTruthy();
    expect(view.getByText("Suggestions: of the")).toBeTruthy();
    expect(view.getByRole("link", { name: "page 7" }).getAttribute("href")).toBe("/source/page/7");

    await fireEvent.click(view.getByRole("button", { name: "Block 3" }));
    expect(onnavigate).toHaveBeenCalledWith(payload.items[0]);

    await fireEvent.click(view.getByRole("button", { name: "Load more (2 of 3)" }));
    expect(onloadmore).toHaveBeenCalledOnce();

    await fireEvent.click(view.getByRole("button", { name: "Run QA again" }));
    expect(onrefresh).toHaveBeenCalledOnce();
  });

  it("drills down into repeated tokens and uses occurrence pagination", async () => {
    const ongroup = vi.fn();
    const ongrouping = vi.fn();
    const group = { id: "group-1", code: "spelling-suspect", category: "spelling", severity: "advisory", token: "ofthe", count: 14,
      preview_occurrences: [{ id: "finding-1", block_id: "block-1", order: 1, target_version: 2 }], preview_omitted: 13 };
    const payload = { summary: { total: 40, grouping: "token" }, capabilities: { spellcheck: { available: true } }, groups: [group], pagination: { total: 3 } };
    const view = render(QADiagnosticsDialog, { props: { open: true, payload, ongroup, ongrouping } });
    expect(view.getByText("Showing 1 of 3 groups")).toBeTruthy();
    expect(view.getByText("14 occurrences")).toBeTruthy();
    await fireEvent.click(view.getByRole("button", { name: "Inspect occurrences" }));
    expect(ongroup).toHaveBeenCalledWith("group-1");
    await view.rerender({ open: true, groupId: "group-1", ongrouping, payload: { ...payload, groups: [{ ...group, preview_occurrences: [] }], occurrences: [{ id: "finding-1", block_id: "block-1", order: 1 }], pagination: { total: 14, next_cursor: "next" } } });
    expect(view.getByText("Showing 1 of 14 occurrences")).toBeTruthy();
    expect(view.getByRole("button", { name: "Load more (1 of 14)" })).toBeTruthy();
    await fireEvent.click(view.getByRole("button", { name: "Back to all groups" }));
    expect(ongrouping).toHaveBeenCalledWith("token");
  });
});
