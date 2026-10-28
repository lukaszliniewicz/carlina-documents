import { afterEach, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import DocumentPreviewModal from "./DocumentPreviewModal.svelte";
import { getDocument } from "../lib/api";

vi.mock("../lib/issues", () => ({
  listIssues: vi.fn().mockResolvedValue({ document_id: "doc-1", current_revision: "current", items: [], total: 0, counts: {} }),
  updateIssue: vi.fn(), reportIssue: vi.fn()
}));

vi.mock("../lib/api", () => ({
  getDocument: vi.fn(),
  getSourceFileUrl: vi.fn((documentId: string, disposition: string) => `/source/${documentId}?disposition=${disposition}`),
  reextractDocumentMetadata: vi.fn(),
  updateDocumentContent: vi.fn(),
  updateDocumentMetadata: vi.fn(),
  cleanupDocument: vi.fn(),
  undoDocumentCleanup: vi.fn(),
  repairDocumentBoundaries: vi.fn()
}));

afterEach(() => {
  document.body.replaceChildren();
  vi.clearAllMocks();
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
  const iframe = target.querySelector("iframe.dp-compare-pdf-frame") as HTMLIFrameElement;
  expect(iframe).toBeTruthy();
  expect(iframe.getAttribute("src")).toBe("/source/doc-1?disposition=inline");
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
