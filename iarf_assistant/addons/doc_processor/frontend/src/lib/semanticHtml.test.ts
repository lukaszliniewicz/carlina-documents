import { describe, expect, it, vi } from "vitest";
import { applyContentPatches, changedContentPatches, editableSemanticBlocks, editorHtmlFromDocument, findHeadingInPreview, headingNavigationTargets, previewHtmlWithListMarkers, scrollHeadingInPreview, validateContentPatches } from "./semanticHtml";

function patchFirstBlock(source: string, html: string) {
  const blocks = editableSemanticBlocks(source);
  const index = blocks.findIndex((block) => block.tag === "p");
  expect(index).toBeGreaterThanOrEqual(0);
  blocks[index] = { ...blocks[index], html };
  return changedContentPatches(source, blocks);
}

describe("semantic HTML text patches", () => {
  it("edits an accessibility description without changing captions, alt wording, graph code or description navigation", () => {
    const source = '<figure class="mermaid-diagram-v1" id="diagram1"><pre class="mermaid-template"><code>flowchart TD\nn1["{{a}}"]</code></pre><div class="mermaid-labels"><p id="diagram1--a">Source label</p></div><figcaption>Printed caption.</figcaption><p class="image-alt-v1">Short alternative.</p><p class="image-description-link"><a href="#diagram-description">Image description</a></p><div class="image-description-v1" id="diagram-description"><p>Source-grounded description.</p></div></figure>';
    const blocks = editableSemanticBlocks(source);
    const index = blocks.findIndex((block) => block.html === "Source-grounded description.");
    expect(index).toBeGreaterThanOrEqual(0);
    blocks[index] = { ...blocks[index], html: "Reviewed source-grounded description." };
    const patches = changedContentPatches(source, blocks);
    expect(validateContentPatches(patches)).toBeNull();
    const result = new DOMParser().parseFromString(applyContentPatches(source, patches), "text/html");
    expect(result.querySelector(".image-description-v1")?.textContent).toBe("Reviewed source-grounded description.");
    expect(result.querySelector(".image-alt-v1")?.textContent).toBe("Short alternative.");
    expect(result.querySelector(".image-description-link a")?.getAttribute("href")).toBe("#diagram-description");
    expect(result.querySelector(".mermaid-template code")?.textContent).toBe('flowchart TD\nn1["{{a}}"]');
    expect(result.querySelector("figcaption")?.textContent).toBe("Printed caption.");
  });
  it("keeps case-sensitive list types and counters in the preview without contaminating editor saves", () => {
    const source = '<ol type="a" start="4" reversed><li value="8">Peer<ol type="A"><li>Nested</li></ol></li></ol><ol type="i"><li>Roman</li></ol><ol type="I"><li>Upper Roman</li></ol>';
    const preview = new DOMParser().parseFromString(previewHtmlWithListMarkers(source), "text/html");
    expect(Array.from(preview.querySelectorAll("ol")).map((list) => list.dataset.dpListStyle)).toEqual([
      "lower-alpha", "upper-alpha", "lower-roman", "upper-roman"
    ]);
    expect(preview.querySelector("ol")?.getAttribute("start")).toBe("4");
    expect(preview.querySelector("ol")?.hasAttribute("reversed")).toBe(true);
    expect(preview.querySelector("li")?.getAttribute("value")).toBe("8");
    const editor = editorHtmlFromDocument({ output_html: source, preview_html: preview.body.innerHTML });
    expect(applyContentPatches(editor, [])).toBe(source);
  });

  it("navigates existing, missing, and duplicate heading IDs without mutating source HTML", () => {
    const source = '<main><h1 id="chapter-one">One<span class="subtitle">Part I</span></h1><h2>Untitled</h2><h2 id="duplicate">First duplicate</h2><h2 id="duplicate">Second duplicate</h2></main>';
    const targets = headingNavigationTargets(source);
    expect(targets.map((target) => target.key)).toEqual([
      "heading:0.0:0",
      "heading:0.1:1",
      "heading:0.2:2",
      "heading:0.3:3"
    ]);
    expect(targets.map((target) => target.elementId)).toEqual(["chapter-one", undefined, undefined, undefined]);
    expect(targets[0].text).toBe("One Part I");

    const preview = document.createElement("div");
    preview.innerHTML = source;
    for (const [index, target] of targets.entries()) {
      const heading = findHeadingInPreview(preview, target);
      expect(heading?.textContent).toBe(index === 0 ? "OnePart I" : target.text);
      const scrollIntoView = vi.fn();
      Object.defineProperty(heading as Element, "scrollIntoView", { value: scrollIntoView });
      expect(scrollHeadingInPreview(preview, target)).toBe(true);
      expect(scrollIntoView).toHaveBeenCalledWith({ behavior: "smooth", block: "start" });
    }
    expect(preview.innerHTML).toBe(source);
  });

  it("returns untouched source byte-for-byte", () => {
    const source = '<main xml:lang="en"><p data-source="scan">A <em>word</em>.</p></main>';
    expect(applyContentPatches(source, [])).toBe(source);
  });

  it("uses raw output rather than a normalized preview for an untouched editor save", () => {
    const output_html = '<main xml:lang="en"><p data-source="scanner">A  <em>word</em>.</p></main>';
    const preview_html = '<main lang="en"><p>A <em>word</em>.</p></main>';
    const editorHtml = editorHtmlFromDocument({ output_html, preview_html });
    expect(editorHtml).toBe(output_html);
    expect(applyContentPatches(editorHtml, [])).toBe(output_html);
  });

  it("preserves a full-document envelope on a text correction", () => {
    const source = '<!doctype html><html lang="en" xml:lang="en"><head><meta charset="utf-8"><title>Book</title><style>.chapter{font-weight:bold}</style></head><body class="book"><main epub:type="bodymatter"><p id="intro" data-source="scan">A <em class="term">word</em>.</p><section id="notes" epub:type="endnotes" role="doc-endnotes"><aside id="fn-1" epub:type="footnote" role="doc-footnote">A note.</aside></section></main></body></html>';
    const patches = patchFirstBlock(source, 'A <em class="term">corrected word</em>.');
    const result = applyContentPatches(source, patches);
    expect(result).toMatch(/^<!DOCTYPE html><html lang="en" xml:lang="en">/i);
    expect(result).toContain('<head><meta charset="utf-8"><title>Book</title><style>.chapter{font-weight:bold}</style></head>');
    expect(result).toContain('<body class="book">');
    expect(result).toContain('<main epub:type="bodymatter">');
    expect(result).toContain('id="fn-1" epub:type="footnote" role="doc-footnote"');
    expect(result).toContain('corrected word');
  });

  it("permits text corrections but rejects rich inline, footnote, and template topology changes", () => {
    const source = '<p id="body">Before <a href="#fn-1" epub:type="noteref" role="doc-noteref">1</a><template data-slot="person">{{name}}</template></p>';
    const valid = patchFirstBlock(source, 'After <a href="#fn-1" epub:type="noteref" role="doc-noteref">1</a><template data-slot="person">{{name}}</template>');
    expect(validateContentPatches(valid)).toBeNull();

    const changedLink = patchFirstBlock(source, 'Before <a href="#fn-2" epub:type="noteref" role="doc-noteref">1</a><template data-slot="person">{{name}}</template>');
    expect(validateContentPatches(changedLink)).toMatch(/attributes|links/i);

    const changedTemplate = patchFirstBlock(source, 'Before <a href="#fn-1" epub:type="noteref" role="doc-noteref">1</a><template data-slot="other">{{name}}</template>');
    expect(validateContentPatches(changedTemplate)).toMatch(/template structure/i);
    expect(() => applyContentPatches(source, changedTemplate)).toThrow(/Only text-node corrections/);
  });
});
