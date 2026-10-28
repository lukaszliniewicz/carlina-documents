import { cleanup, fireEvent, render, waitFor } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import TargetBlockCandidateEditor from "./TargetBlockCandidateEditor.svelte";

const candidate = {
  contract_version: "translation-block-editor-v1",
  content: "Hello <em>world</em><lit r=\"1\">AXIOM-7319</lit><br/>",
  fields: [{ name: "alt", value: "alt text" }]
};

const source = {
  editor: {
    contract_version: "translation-block-editor-v1",
    content: "Hello <em>world</em><lit r=\"1\">AXIOM-7319</lit><br/>",
    fields: [{ name: "alt", value: "Old description" }]
  }
};

afterEach(cleanup);

describe("TargetBlockCandidateEditor", () => {
  it("edits one formatted translation surface without exposing scaffolding", async () => {
    const onChange = vi.fn();
    const view = render(TargetBlockCandidateEditor, { props: { candidate, source, onChange } });
    const editor = view.getByRole("textbox", { name: "Translation text" });
    await waitFor(() => expect(editor.textContent).toContain("AXIOM-7319"));

    expect(editor.querySelector("em").textContent).toBe("world");
    expect(editor.querySelector("lit").getAttribute("contenteditable")).toBe("false");
    expect(view.container.querySelector(".protected-chip")).toBeNull();
    expect(view.container.querySelector(".literal-chip")).toBeNull();
    expect(view.container.textContent).not.toContain("line break");
    expect(view.container.textContent).not.toContain("token");

    editor.innerHTML = "Cześć <em>świecie</em><lit r=\"1\">AXIOM-7319</lit><br>";
    await fireEvent.input(editor);
    expect(onChange.mock.calls.at(-1)[0]).toEqual({
      ...candidate,
      content: "Cześć <em>świecie</em><lit r=\"1\">AXIOM-7319</lit><br/>"
    });

    const lexical = view.getByRole("textbox", { name: "Translated alt" });
    await fireEvent.input(lexical, { target: { value: "Nowy tekst alternatywny" } });
    expect(onChange.mock.calls.at(-1)[0].fields).toEqual([
      { name: "alt", value: "Nowy tekst alternatywny" }
    ]);
  });

  it("undoes edits that remove source formatting or fixed text", async () => {
    const onChange = vi.fn();
    const view = render(TargetBlockCandidateEditor, { props: { candidate, source, onChange } });
    const editor = view.getByRole("textbox", { name: "Translation text" });
    await waitFor(() => expect(editor.querySelector("em")).toBeTruthy());

    editor.innerHTML = "Cześć bez struktury";
    await fireEvent.input(editor);

    expect(onChange).not.toHaveBeenCalled();
    expect(editor.querySelector("em").textContent).toBe("world");
    expect(editor.querySelector("lit").textContent).toBe("AXIOM-7319");
    expect(view.getByRole("status").textContent).toContain("removed");
  });

  it("adds target-only emphasis with the standard keyboard shortcuts", async () => {
    const onChange = vi.fn();
    const plainCandidate = { ...candidate, content: "Hello world" };
    const plainSource = {
      editor: { ...source.editor, content: "Hello world" }
    };
    const view = render(TargetBlockCandidateEditor, {
      props: { candidate: plainCandidate, source: plainSource, onChange }
    });
    const editor = view.getByRole("textbox", { name: "Translation text" });
    await waitFor(() => expect(editor.textContent).toBe("Hello world"));
    const range = document.createRange();
    range.setStart(editor.firstChild, 0);
    range.setEnd(editor.firstChild, 5);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);

    await fireEvent.keyDown(editor, { key: "b", ctrlKey: true });

    expect(onChange.mock.calls.at(-1)[0].content).toBe(
      "<strong>Hello</strong> world"
    );
  });

  it("offers only the target-safe inline formats when text is selected", async () => {
    const onChange = vi.fn();
    const plainCandidate = { ...candidate, content: "Hello world", fields: [] };
    const plainSource = {
      editor: { ...source.editor, content: "Hello world", fields: [] }
    };
    const view = render(TargetBlockCandidateEditor, {
      props: { candidate: plainCandidate, source: plainSource, onChange }
    });
    const editor = view.getByRole("textbox", { name: "Translation text" });
    await waitFor(() => expect(editor.textContent).toBe("Hello world"));
    const range = document.createRange();
    range.setStart(editor.firstChild, 0);
    range.setEnd(editor.firstChild, 5);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);

    await fireEvent.mouseUp(editor);

    expect(view.getByRole("button", { name: "Bold selected text" })).toBeTruthy();
    expect(view.getByRole("button", { name: "Italicize selected text" })).toBeTruthy();
    expect(view.getByRole("button", { name: "Underline selected text" })).toBeTruthy();
    expect(view.getByRole("button", { name: "Superscript selected text" })).toBeTruthy();
    expect(view.getByRole("button", { name: "Subscript selected text" })).toBeTruthy();

    await fireEvent.click(view.getByRole("button", { name: "Superscript selected text" }));
    expect(onChange.mock.calls.at(-1)[0].content).toBe("<sup>Hello</sup> world");
  });

  it("offers an annotation action for selected fixed text without offering unsafe formatting", async () => {
    const onAddNote = vi.fn();
    const fixedCandidate = { ...candidate, content: '<lit r="1">א</lit> Heading', fields: [] };
    const fixedSource = {
      editor: { ...source.editor, content: fixedCandidate.content, fields: [] }
    };
    const view = render(TargetBlockCandidateEditor, {
      props: { candidate: fixedCandidate, source: fixedSource, canAnnotate: true, onAddNote }
    });
    const editor = view.getByRole("textbox", { name: "Translation text" });
    await waitFor(() => expect(editor.querySelector("lit")?.textContent).toBe("א"));
    const range = document.createRange();
    range.selectNodeContents(editor.querySelector("lit"));
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);

    await fireEvent.mouseUp(editor);

    expect(view.queryByRole("button", { name: "Bold selected text" })).toBeNull();
    await fireEvent.click(view.getByRole("button", { name: "Add note to selected translation" }));
    expect(onAddNote).toHaveBeenCalledWith({
      kind: "target",
      text: "א",
      occurrence_index: 0
    });
  });

  it("keeps boundary-only source literals canonical but visually ignorable", async () => {
    const boundaryCandidate = {
      ...candidate,
      content: '<lit r="1">\n</lit><span r="2">TENKO SAN</span><lit r="3">\n</lit>',
      fields: []
    };
    const boundarySource = {
      editor: {
        ...source.editor,
        content: boundaryCandidate.content,
        fields: []
      }
    };
    const view = render(TargetBlockCandidateEditor, {
      props: { candidate: boundaryCandidate, source: boundarySource }
    });
    const editor = view.getByRole("textbox", { name: "Translation text" });
    await waitFor(() => expect(editor.textContent).toContain("TENKO SAN"));

    expect(editor.querySelector('lit[data-boundary-whitespace="leading"]')).toBeTruthy();
    expect(editor.querySelector('lit[data-boundary-whitespace="trailing"]')).toBeTruthy();
  });

  it("presents a lone translated alt value as ordinary editorial text", () => {
    const altCandidate = { ...candidate, content: "", fields: [{ name: "alt", value: "Portret" }] };
    const altSource = {
      editor: { ...source.editor, content: "", fields: [{ name: "alt", value: "Portrait" }] }
    };
    const view = render(TargetBlockCandidateEditor, {
      props: { candidate: altCandidate, source: altSource }
    });

    expect(view.getByRole("textbox", { name: "Translated alt" }).value).toBe("Portret");
    expect(view.container.querySelector("fieldset")).toBeNull();
    expect(view.container.textContent).not.toContain("Accessible text");
  });

  it("is read-only in shared mode", async () => {
    const onChange = vi.fn();
    const view = render(TargetBlockCandidateEditor, { props: { candidate, source, guestMode: true, onChange } });
    const editor = view.getByRole("textbox", { name: "Translation text" });
    await waitFor(() => expect(editor.textContent).toContain("Hello"));
    expect(view.container.querySelector(".candidate-editor").dataset.guestMode).toBe("true");
    expect(editor.getAttribute("contenteditable")).toBe("false");
    expect(view.container.querySelector('button[type="button"]')).toBeNull();
    editor.textContent = "ignored";
    await fireEvent.input(editor);
    expect(onChange).not.toHaveBeenCalled();
  });
});
