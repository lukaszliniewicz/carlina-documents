import { cleanup, fireEvent, render, waitFor } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import BlockNotesEditor from "./BlockNotesEditor.svelte";

const block = {
  id: "block-1",
  order: 1,
  target: { active_version: 7 }
};

afterEach(cleanup);

describe("BlockNotesEditor", () => {
  it("renders inert inline formatting while editing the unchanged Markdown body", async () => {
    const body = "A **bold _term_** and `literal <tag>`.";
    const view = render(BlockNotesEditor, { props: {
      block, expanded: true,
      notes: [{ id: "note-1", version: 1, state: "active", body,
        body_html: 'A <strong>bold <em>term</em></strong> and <code>literal &lt;tag&gt;</code>.',
        anchor: { kind: "whole_block" } }]
    } });
    expect(view.container.querySelector('.note-body strong em').textContent).toBe('term');
    expect(view.container.querySelector('.note-body code').textContent).toBe('literal <tag>');
    await fireEvent.click(view.getByRole("button", { name: "Edit", exact: true }));
    expect(view.getByLabelText("Reader-facing note").value).toBe(body);
    await view.rerender({ block, notes: [{ id: "note-1", version: 1, state: "active", body,
      body_html: '<em onclick="alert(1)">unsafe</em><img src="https://example.test/x">',
      anchor: { kind: "whole_block" } }] });
    expect(view.container.querySelector('.note-body').textContent).toBe(body);
    expect(view.container.querySelector('.note-body em, .note-body img')).toBeNull();
  });

  it("maps a whole-block form choice to the strict service anchor", async () => {
    const onCreate = vi.fn().mockResolvedValue(true);
    const view = render(BlockNotesEditor, {
      props: { block, accessRole: "owner", onCreate }
    });

    const disclosure = view.container.querySelector("details.notes-editor");
    expect(disclosure.open).toBe(false);
    expect(view.getByText("No notes")).toBeTruthy();

    await view.rerender({ block, accessRole: "owner", onCreate, expanded: true });
    expect(disclosure.open).toBe(true);
    await fireEvent.click(view.getByRole("button", { name: "Add note" }));
    await fireEvent.input(view.getByLabelText("Reader-facing note"), {
      target: { value: "Context for the whole passage." }
    });
    await fireEvent.submit(view.container.querySelector("form"));

    await waitFor(() => expect(onCreate).toHaveBeenCalledOnce());
    expect(onCreate).toHaveBeenCalledWith({
      body: "Context for the whole passage.",
      anchor: {
        kind: "block",
        anchor_text: null,
        occurrence_index: null,
        target_version: null
      },
      citation_ids: []
    });
  });

  it("posts selected target text with its occurrence and active-version fence", async () => {
    const onCreate = vi.fn().mockResolvedValue(true);
    const view = render(BlockNotesEditor, {
      props: {
        block,
        accessRole: "reviewer",
        createRequest: {
          key: "selection-1",
          anchor: { kind: "target", text: "א", occurrence_index: 1 }
        },
        onCreate
      }
    });

    await waitFor(() => expect(view.container.querySelector("details.notes-editor").open).toBe(true));
    await waitFor(() => expect(view.getByLabelText("Exact phrase").value).toBe("א"));
    await fireEvent.input(view.getByLabelText("Reader-facing note"), {
      target: { value: "The retained Hebrew letter marks the paragraph." }
    });
    await fireEvent.input(view.getByLabelText(/Dossier reference IDs/), {
      target: { value: "ref-hebrew" }
    });
    await fireEvent.submit(view.container.querySelector("form"));

    await waitFor(() => expect(onCreate).toHaveBeenCalledOnce());
    expect(onCreate).toHaveBeenCalledWith({
      body: "The retained Hebrew letter marks the paragraph.",
      anchor: {
        kind: "target",
        anchor_text: "א",
        occurrence_index: 1,
        target_version: 7
      },
      citation_ids: ["ref-hebrew"]
    });
  });

  it("shows note count and review state when the header opens the panel", () => {
    const view = render(BlockNotesEditor, {
      props: {
        block,
        accessRole: "owner",
        expanded: true,
        notes: [
          { id: "note-1", version: 1, state: "active", body: "Context", anchor: { kind: "whole_block" } },
          { id: "note-2", version: 1, state: "needs_review", body: "Check anchor", anchor: { kind: "target" } }
        ]
      }
    });

    const disclosure = view.container.querySelector("details.notes-editor");
    expect(disclosure.open).toBe(true);
    expect(view.getByText("2 notes")).toBeTruthy();
    expect(view.getByText("1 to review")).toBeTruthy();
  });
});
