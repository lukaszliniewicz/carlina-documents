export type SemanticContentPatch = {
  kind: "content";
  path: number[];
  tag: string;
  originalHtml: string;
  html: string;
};

export type EditableSemanticBlock = {
  id: string;
  path: number[];
  tag: string;
  html: string;
};

export type HeadingNavigationTarget = {
  key: string;
  path: number[];
  index: number;
  tag: string;
  text: string;
  /** Present only when unique in the source document. */
  elementId?: string;
};

type DocumentHtmlPayload = {
  output_html?: string | null;
  preview_html?: string | null;
};

/** The canonical extraction is the only safe base for an edit/save cycle. */
export function editorHtmlFromDocument(document: DocumentHtmlPayload): string {
  return document.output_html || document.preview_html || "";
}

/** Restore list markers in the preview without changing the extraction or editor. */
export function previewHtmlWithListMarkers(html: string): string {
  if (!/<ol[\s>]/i.test(html)) return html;
  const parsed = parseSource(html);
  const styles = new Map([
    ["a", "lower-alpha"], ["A", "upper-alpha"], ["i", "lower-roman"], ["I", "upper-roman"]
  ]);
  for (const list of Array.from(parsed.document.querySelectorAll("ol"))) {
    // HTML type selectors fold case; a private data attribute preserves a/A and i/I.
    list.setAttribute("data-dp-list-style", styles.get(list.getAttribute("type") || "") || "decimal");
  }
  return serializeDocument(parsed.document, parsed.fullDocument);
}

const EDITABLE_TEXT_CONTAINERS = new Set([
  "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre", "td", "th", "figcaption", "aside"
]);

function pathFor(node: Element, root: Element): number[] {
  const path: number[] = [];
  let current: Element | null = node;
  while (current && current !== root) {
    const parent: Element | null = current.parentElement;
    if (!parent) return [];
    const index = Array.prototype.indexOf.call(parent.children, current) as number;
    if (index < 0) return [];
    path.unshift(index);
    current = parent;
  }
  return current === root ? path : [];
}

function elementAtPath(root: Element, path: number[]): Element | null {
  let current: Element = root;
  for (const index of path) {
    const next = current.children.item(index);
    if (!next) return null;
    current = next;
  }
  return current;
}

export function headingNavigationTargets(sourceHtml: string): HeadingNavigationTarget[] {
  const document = parseSource(sourceHtml).document;
  const root = document.body;
  const idCounts = new Map<string, number>();
  for (const element of Array.from(root.querySelectorAll("[id]"))) {
    const id = element.id.trim();
    if (id) idCounts.set(id, (idCounts.get(id) || 0) + 1);
  }
  return Array.from(root.querySelectorAll("h1,h2,h3,h4,h5,h6")).map((heading, index) => {
    const path = pathFor(heading, root);
    const id = heading.id.trim();
    return {
      key: `heading:${path.join(".")}:${index}`,
      path,
      index,
      tag: heading.tagName.toLowerCase(),
      text: headingNavigationText(heading),
      ...(id && idCounts.get(id) === 1 ? { elementId: id } : {})
    };
  });
}

export function findHeadingInPreview(
  previewRoot: Element,
  target: HeadingNavigationTarget
): Element | null {
  const headings = Array.from(previewRoot.querySelectorAll("h1,h2,h3,h4,h5,h6"));
  if (target.elementId) {
    const byId = headings.find((heading) => heading.id === target.elementId);
    if (byId) return byId;
  }
  const byPath = elementAtPath(previewRoot, target.path);
  if (byPath?.tagName.toLowerCase() === target.tag) return byPath;
  const byIndex = headings[target.index];
  return byIndex?.tagName.toLowerCase() === target.tag ? byIndex : null;
}

export function scrollHeadingInPreview(
  previewRoot: Element,
  target: HeadingNavigationTarget
): boolean {
  const heading = findHeadingInPreview(previewRoot, target);
  if (!heading) return false;
  heading.scrollIntoView({ behavior: "smooth", block: "start" });
  return true;
}

type ParsedSource = {
  document: Document;
  fullDocument: boolean;
};

function parseSource(html: string): ParsedSource {
  const fullDocument = /^\s*(?:<!doctype[^>]*>\s*)?<html[\s>]/i.test(html)
    || /^\s*<!doctype/i.test(html);
  return { document: new DOMParser().parseFromString(html, "text/html"), fullDocument };
}

function headingNavigationText(heading: Element): string {
  const clone = heading.cloneNode(true) as Element;
  for (const subtitle of Array.from(clone.querySelectorAll("span.subtitle"))) {
    subtitle.insertAdjacentText("beforebegin", " ");
  }
  return (clone.textContent || "").replace(/\s+/g, " ").trim();
}

function templateAwareChildNodes(node: Node): Node[] {
  if (node instanceof HTMLTemplateElement) {
    return Array.from(node.content.childNodes);
  }
  return Array.from(node.childNodes);
}

function inlineTopology(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) return "#text";
  if (node.nodeType === Node.COMMENT_NODE) return `#comment:${node.nodeValue || ""}`;
  if (node.nodeType !== Node.ELEMENT_NODE) return `#node:${node.nodeType}`;
  const element = node as Element;
  const attributes = Array.from(element.attributes)
    .map((attribute) => `${attribute.namespaceURI || ""}:${attribute.name}=${JSON.stringify(attribute.value)}`)
    .sort()
    .join("|");
  return `<${element.namespaceURI || ""}:${element.tagName.toLowerCase()} ${attributes}>${templateAwareChildNodes(element).map(inlineTopology).join("")}</${element.tagName.toLowerCase()}>`;
}

function validateTextOnlyPatch(patch: SemanticContentPatch): string | null {
  const original = parseSource(patch.originalHtml).document.body;
  const next = parseSource(patch.html).document.body;
  return inlineTopology(original) === inlineTopology(next)
    ? null
    : `The ${patch.tag} edit changes inline elements, attributes, links, IDs, roles, EPUB/XML/data attributes, or template structure. Only text-node corrections are allowed.`;
}

function serializeDocument(document: Document, fullDocument: boolean): string {
  if (!fullDocument) return document.body.innerHTML;
  const doctype = document.doctype;
  if (!doctype) return document.documentElement.outerHTML;
  const publicId = doctype.publicId ? ` PUBLIC "${doctype.publicId}"` : "";
  const systemId = doctype.systemId
    ? `${publicId ? "" : " SYSTEM"} "${doctype.systemId}"`
    : "";
  return `<!DOCTYPE ${doctype.name}${publicId}${systemId}>${document.documentElement.outerHTML}`;
}

/**
 * Return only leaf text containers. Their surrounding list/table/figure/
 * endnote structure is never presented as an editable block, so an edit can
 * be applied to the original DOM without rehoming semantic containers.
 */
export function editableSemanticBlocks(originalHtml: string): EditableSemanticBlock[] {
  const document = parseSource(originalHtml).document;
  const root = document.body;
  return Array.from(root.querySelectorAll("*"))
    .filter((node) => EDITABLE_TEXT_CONTAINERS.has(node.tagName.toLowerCase()))
    .filter((node) => !node.querySelector(Array.from(EDITABLE_TEXT_CONTAINERS).join(",")))
    .map((node, index) => ({
      id: `semantic-block-${index}`,
      path: pathFor(node, root),
      tag: node.tagName.toLowerCase(),
      html: node.innerHTML
    }));
}

export function changedContentPatches(
  originalHtml: string,
  blocks: EditableSemanticBlock[]
): SemanticContentPatch[] {
  const document = parseSource(originalHtml).document;
  const root = document.body;
  return blocks.flatMap((block) => {
    const target = elementAtPath(root, block.path);
    if (!target || target.tagName.toLowerCase() !== block.tag || target.innerHTML === block.html) {
      return [];
    }
    return [{
      kind: "content" as const,
      path: block.path,
      tag: block.tag,
      originalHtml: target.innerHTML,
      html: block.html
    }];
  });
}

export function validateContentPatches(patches: SemanticContentPatch[]): string | null {
  for (const patch of patches) {
    const error = validateTextOnlyPatch(patch);
    if (error) return error;
  }
  return null;
}

/**
 * Apply only content patches to a fresh parse of the original. A zero-patch
 * save is byte-identical by construction; non-zero patches retain every
 * untouched element, attribute, ordering relationship, and endnote location.
 */
export function applyContentPatches(
  originalHtml: string,
  patches: SemanticContentPatch[]
): string {
  if (!patches.length) return originalHtml;
  const { document, fullDocument } = parseSource(originalHtml);
  const root = document.body;
  for (const patch of patches) {
    const topologyError = validateTextOnlyPatch(patch);
    if (topologyError) throw new Error(topologyError);
    const target = elementAtPath(root, patch.path);
    if (!target || target.tagName.toLowerCase() !== patch.tag || target.innerHTML !== patch.originalHtml) {
      throw new Error("The document structure changed while this edit was open. Reload the preview before saving.");
    }
    target.innerHTML = patch.html;
  }
  return serializeDocument(document, fullDocument);
}

export function structuralChangeSummary(): string {
  return "No structural changes. Reordering, deleting, retagging, and footnote relocation are disabled to protect semantic output.";
}
