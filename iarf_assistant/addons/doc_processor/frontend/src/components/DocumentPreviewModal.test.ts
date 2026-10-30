import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import DocumentPreviewModal from "./DocumentPreviewModal.svelte";
import { getDocument, updateDocumentContent, loadCropPages } from "../lib/api";

vi.mock("../lib/issues", () => ({
  listIssues: vi.fn().mockResolvedValue({ document_id: "doc-1", current_revision: "current", items: [], total: 0, counts: {} }),
  updateIssue: vi.fn(), reportIssue: vi.fn()
}));

vi.mock("../lib/api", () => ({
  getDocument: vi.fn(),
  getSourceFileUrl: vi.fn((documentId: string, disposition: string) => `/source/${documentId}?disposition=${disposition}`),
  getPdfPageImageUrl: vi.fn((id: string, page: number) => `/page/${id}/${page}`),
  loadCropPages: vi.fn(),
  reextractDocumentMetadata: vi.fn(),
  updateDocumentContent: vi.fn(),
  updateDocumentMetadata: vi.fn(),
  cleanupDocument: vi.fn(),
  undoDocumentCleanup: vi.fn(),
  repairDocumentBoundaries: vi.fn()
}));

beforeEach(() => {
  Object.defineProperty(HTMLDialogElement.prototype,"showModal",{ configurable:true,value:function(){ this.open = true; } });
  Object.defineProperty(HTMLDialogElement.prototype,"close",{ configurable:true,value:function(){ this.open = false; } });
  vi.mocked(loadCropPages).mockResolvedValue({ total_pages:12,pages:[] } as never);
});
afterEach(() => {
  document.body.replaceChildren();
  vi.clearAllMocks();
  vi.restoreAllMocks();
});

it("shows the source PDF beside text and keeps preview actions compact and labelled", async () => {
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
    configurable: true,
    value: function () { this.open = true; }
  });
  Object.defineProperty(HTMLDialogElement.prototype, "close", {
    configurable: true,
    value: function () { this.open = false; }
  });
  vi.mocked(getDocument).mockResolvedValue({
    id: "doc-1",
    filename: "source.pdf",
    status: "completed",
    source_available: true,
    output_html: "<main><h1>Heading</h1><p>Extracted text.</p></main>",
    preview_html: "<main><h1>Heading</h1><p>Extracted text.</p></main>",
    output_markdown: "# Heading\n\nExtracted text."
  } as never);

  const target = document.createElement("div");
  document.body.append(target);
  const component = mount(DocumentPreviewModal, {
    target,
    props: { open: true, documentId: "doc-1" }
  });

  await vi.waitFor(() => expect(getDocument).toHaveBeenCalledWith("doc-1", expect.any(AbortSignal)));
  await vi.waitFor(() => expect(target.textContent).toContain("Heading"));

  const controls = target.querySelector('button[aria-label="Show preview controls"]') as HTMLButtonElement;
  expect(controls).toBeTruthy();
  expect(controls.querySelector("svg")).not.toBeNull();
  expect(controls.textContent?.trim()).toBe("");
  controls.click();
  await tick();

  const compare = target.querySelector('button[aria-label="Compare PDF and extracted text"]') as HTMLButtonElement;
  expect(compare).toBeTruthy();
  expect(compare.querySelector("svg")).not.toBeNull();
  expect(compare.textContent?.trim()).toBe("");
  expect(target.querySelector('button[aria-label="Remove page headers and footers"] svg')).not.toBeNull();
  expect(target.querySelector('button[aria-label="Repair page continuations"] svg')).not.toBeNull();

  compare.click();
  await tick();
  await vi.waitFor(() => expect(target.querySelector('.pdf-source-viewer img')?.getAttribute('src')).toContain('/page/doc-1/0'));
  expect(target.querySelector('iframe')).toBeNull();
  expect(target.querySelector('a[aria-label="Download original PDF"]')?.getAttribute("href")).toBe("/source/doc-1?disposition=attachment");

  const issues = Array.from(target.querySelectorAll("button")).find(button => button.textContent?.trim() === "Review issues")!;
  issues.click();
  await vi.waitFor(() => expect(target.textContent).toContain("Back to document"));
  vi.mocked(getDocument).mockResolvedValue({ id: "doc-1", filename: "source.pdf", status: "completed", output_html: "<main><p>Freshly corrected source.</p></main>", preview_html: "<main><p>Freshly corrected source.</p></main>" } as never);
  Array.from(target.querySelectorAll("button")).find(button => button.textContent?.trim() === "Back to document")!.click();
  await vi.waitFor(() => expect(target.textContent).toContain("Freshly corrected source."));
  expect(getDocument).toHaveBeenCalledTimes(2);

  await unmount(component);
});

function button(target: HTMLElement,label: string) {
  return [...target.querySelectorAll<HTMLButtonElement>('button')].find(b => b.getAttribute('aria-label') === label || b.textContent?.trim() === label)!;
}
async function change(input: HTMLTextAreaElement,text: string) {
  input.value = text;
  input.dispatchEvent(new Event('input',{bubbles:true}));
  await tick();
}
async function chooseDiscard(discard = false) {
  let choice: HTMLButtonElement | undefined;
  await vi.waitFor(() => { choice = [...document.querySelectorAll<HTMLButtonElement>('button')].find(b=>b.textContent?.trim()===(discard?'Discard changes':'Keep editing')); expect(choice).toBeTruthy(); });
  choice!.click(); await tick();
}
async function editor(html = '<main><h1>Heading</h1><p>Original text.</p></main>') {
  const target = document.createElement('div'); document.body.append(target);
  const doc = { id:'doc-1',filename:'source.pdf',status:'completed',updated_at:'revision-1',source_available:true,output_html:html,preview_html:html };
  vi.mocked(getDocument).mockResolvedValue(doc as never);
  const closed = vi.fn();
  const component = mount(DocumentPreviewModal,{target,props:{open:true,documentId:'doc-1'},events:{close:closed}});
  await vi.waitFor(() => expect(target.textContent).toContain('Heading'));
  button(target,'Correct text').click(); await tick();
  return {target,component,closed,doc};
}

it('bounds book editors, keeps drafts across pages, and saves protected markup with its revision',async () => {
  const html = '<main><h1>Heading</h1><p id="opening">A <em>careful</em> edit <a href="#note" role="doc-noteref">[1]</a>.</p>' + Array.from({length:2000},(_,i)=>`<p>Paragraph ${i+1}.</p>`).join('') + '<aside id="note"><p>Footnote.</p></aside></main>';
  const {target,component,doc} = await editor(html);
  expect(target.querySelectorAll('textarea').length).toBeLessThan(60);
  const parse = vi.spyOn(DOMParser.prototype,'parseFromString');
  await change(target.querySelector('textarea[aria-label^="Edit p content"]')!,'A revised ');
  expect(parse.mock.calls.every(([input]) => String(input).length < 1000)).toBe(true);
  button(target,'Next blocks').click(); await tick();
  await change(target.querySelector('textarea')!,'Second page correction.');
  button(target,'Previous blocks').click(); await tick();
  expect(target.querySelector<HTMLTextAreaElement>('textarea[aria-label^="Edit p content"]')!.value).toBe('A revised ');
  vi.mocked(updateDocumentContent).mockImplementation(async (_id,content) => ({...doc,output_html:content,preview_html:content,updated_at:'revision-2'} as never));
  button(target,'Save edits').click(); await tick(); button(target,'Confirm and save').click();
  await vi.waitFor(() => expect(updateDocumentContent).toHaveBeenCalledOnce());
  const call = vi.mocked(updateDocumentContent).mock.calls[0];
  expect(call[2]).toBe('revision-1');
  expect(call[1]).toContain('<em>careful</em>');
  expect(call[1]).toContain('<a href="#note" role="doc-noteref">[1]</a>');
  expect(call[1]).toContain('A revised ');
  expect(call[1]).toContain('Second page correction.');
  await unmount(component);
},15000);

it('keeps correction drafts when closing or leaving is cancelled',async () => {
  const {target,component,closed} = await editor();
  await change(target.querySelector('textarea[aria-label="Edit p content"]')!,'Unsaved correction.');
  button(target,'Close').click(); await tick();
  await chooseDiscard();
  button(target,'Cancel editing').click(); await tick();
  await chooseDiscard();
  expect(closed).not.toHaveBeenCalled();
  expect(target.querySelector<HTMLTextAreaElement>('textarea[aria-label="Edit p content"]')!.value).toBe('Unsaved correction.');
  expect(window.dispatchEvent(new Event('carlina:before-navigate',{cancelable:true}))).toBe(false);
  expect(window.dispatchEvent(new Event('beforeunload',{cancelable:true}))).toBe(false);
  button(target,'Close').click(); await chooseDiscard(true);
  await vi.waitFor(()=>expect(closed).toHaveBeenCalledOnce());
  await unmount(component);
});

it('keeps a conflicted draft and requires an explicit discard before reload',async () => {
  const {target,component} = await editor();
  vi.mocked(updateDocumentContent).mockRejectedValueOnce(Object.assign(new Error('Changed elsewhere'),{status:409}));
  await change(target.querySelector('textarea[aria-label="Edit p content"]')!,'Local draft.');
  button(target,'Save edits').click(); await tick(); button(target,'Confirm and save').click();
  await vi.waitFor(() => expect(target.textContent).toContain('Changed elsewhere'));
  expect(target.querySelector<HTMLTextAreaElement>('textarea[aria-label="Edit p content"]')!.value).toBe('Local draft.');
  expect(button(target,'Save edits').disabled).toBe(true);
  button(target,'Reload document').click(); await tick();
  expect(getDocument).toHaveBeenCalledTimes(1);
  await chooseDiscard();
  await unmount(component);
});

it('prevents dismissing an extraction editor while a save is unresolved',async () => {
  const {target,component,closed,doc} = await editor();
  let resolve!: (value: never) => void;
  vi.mocked(updateDocumentContent).mockReturnValueOnce(new Promise(done => {resolve = done;}));
  await change(target.querySelector('textarea[aria-label="Edit p content"]')!,'Saving correction.');
  button(target,'Save edits').click(); await tick(); button(target,'Confirm and save').click(); await tick();
  expect(button(target,'Close').disabled).toBe(true);
  expect(window.dispatchEvent(new Event('carlina:before-navigate',{cancelable:true}))).toBe(false);
  expect(closed).not.toHaveBeenCalled();
  resolve({...doc,updated_at:'revision-2'} as never);
  await vi.waitFor(() => expect(button(target,'Close').disabled).toBe(false));
  await unmount(component);
});
