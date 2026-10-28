export const COMPACT_EDITOR_CONTRACT = "translation-block-editor-v1";

const INLINE_TAGS = new Set([
  "a", "abbr", "b", "br", "cite", "code", "del", "em", "i", "ins",
  "kbd", "mark", "q", "s", "samp", "small", "span", "strong", "sub",
  "sup", "time", "u", "var", "wbr", "lit"
]);
const VOID_TAGS = new Set(["br", "wbr"]);
const TARGET_LOCAL_FORMAT_TAGS = new Set([
  "b", "em", "i", "strong", "sub", "sup", "u"
]);
const LOCAL_REFERENCE = /^[1-9][0-9]*$/;

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function parseMarkup(content, label = "editor content") {
  if (typeof content !== "string") throw new Error(`Invalid ${label}.`);
  if (content.includes("<!") || content.includes("<?")) {
    throw new Error(`Invalid ${label}: active markup is not allowed.`);
  }
  const parsed = new DOMParser().parseFromString(`<root>${content}</root>`, "application/xml");
  if (parsed.querySelector("parsererror") || parsed.documentElement.localName !== "root") {
    throw new Error(`Invalid ${label}: markup must be a well-formed fragment.`);
  }
  const references = new Set();

  const visit = (node) => {
    if (node.nodeType === Node.TEXT_NODE) return;
    if (node.nodeType !== Node.ELEMENT_NODE) {
      throw new Error(`Invalid ${label}: only text and approved inline elements are allowed.`);
    }
    const tag = node.localName;
    if (!INLINE_TAGS.has(tag) || node.tagName !== tag) {
      throw new Error(`Invalid ${label}: unsupported inline element.`);
    }
    const attributes = [...node.attributes];
    if (attributes.some((attribute) => attribute.name !== "r")) {
      throw new Error(`Invalid ${label}: unsupported inline attribute.`);
    }
    const reference = node.getAttribute("r");
    if (reference !== null && !LOCAL_REFERENCE.test(reference)) {
      throw new Error(`Invalid ${label}: malformed local reference.`);
    }
    if (reference !== null) {
      if (references.has(reference)) {
        throw new Error(`Invalid ${label}: duplicate local reference.`);
      }
      references.add(reference);
    }
    if (tag === "lit") {
      if (reference === null || [...node.childNodes].some((child) => child.nodeType !== Node.TEXT_NODE)) {
        throw new Error(`Invalid ${label}: malformed fixed source text.`);
      }
      return;
    }
    if (VOID_TAGS.has(tag) && node.childNodes.length) {
      throw new Error(`Invalid ${label}: a line-break element cannot contain text.`);
    }
    for (const child of node.childNodes) visit(child);
  };

  for (const child of parsed.documentElement.childNodes) visit(child);
  return parsed.documentElement;
}

function structureEntries(content) {
  const root = parseMarkup(content);
  const entries = [];
  const visit = (node) => {
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const reference = node.getAttribute("r") || "";
    entries.push(node.localName === "lit"
      ? `lit:${reference}:${node.textContent}`
      : `tag:${node.localName}:${reference}`);
    for (const child of node.children) visit(child);
  };
  for (const child of root.children) visit(child);
  return entries.sort();
}

function fieldNames(editor) {
  return editor.fields.map((field) => field.name);
}

function structureCounts(content) {
  const counts = new Map();
  for (const entry of structureEntries(content)) {
    counts.set(entry, (counts.get(entry) || 0) + 1);
  }
  return counts;
}

function targetStructurePreservesSource(sourceContent, targetContent) {
  const source = structureCounts(sourceContent);
  const target = structureCounts(targetContent);
  for (const [entry, required] of source) {
    if ((target.get(entry) || 0) < required) return false;
  }
  for (const [entry, observed] of target) {
    const required = source.get(entry) || 0;
    if (observed <= required) continue;
    if (!entry.startsWith("tag:") || !entry.endsWith(":")) return false;
    const tag = entry.slice(4, -1);
    if (!TARGET_LOCAL_FORMAT_TAGS.has(tag)) return false;
  }
  return true;
}

export function validateCompactEditor(editor, label = "compact editor projection") {
  if (!isRecord(editor)
    || Object.keys(editor).sort().join(",") !== "content,contract_version,fields"
    || editor.contract_version !== COMPACT_EDITOR_CONTRACT
    || typeof editor.content !== "string"
    || !Array.isArray(editor.fields)) {
    throw new Error(`Invalid ${label}.`);
  }
  const names = new Set();
  for (const field of editor.fields) {
    if (!isRecord(field)
      || Object.keys(field).sort().join(",") !== "name,value"
      || typeof field.name !== "string"
      || !field.name.trim()
      || field.name !== field.name.trim()
      || typeof field.value !== "string"
      || names.has(field.name)) {
      throw new Error(`Invalid ${label}: translated field.`);
    }
    names.add(field.name);
  }
  parseMarkup(editor.content, `${label} content`);
  return editor;
}

export function requireMatchingEditorStructure(sourceEditor, targetEditor) {
  validateCompactEditor(sourceEditor, "source editor projection");
  validateCompactEditor(targetEditor, "target editor projection");
  if (!targetStructurePreservesSource(sourceEditor.content, targetEditor.content)
    || JSON.stringify(fieldNames(sourceEditor)) !== JSON.stringify(fieldNames(targetEditor))) {
    throw new Error("Invalid target editor projection: source structure changed.");
  }
  return targetEditor;
}

function escapeText(value) {
  return String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

export function serializeCompactContent(root) {
  if (!(root instanceof Element)) throw new Error("The translation editor is unavailable.");
  const serialize = (node) => {
    if (node.nodeType === Node.TEXT_NODE) return escapeText(node.nodeValue || "");
    if (node.nodeType !== Node.ELEMENT_NODE) {
      throw new Error("Only text and source formatting can be edited here.");
    }
    const tag = node.localName;
    if (!INLINE_TAGS.has(tag)) throw new Error("Pasted layout markup is not supported here.");
    const reference = node.getAttribute("r");
    if (reference !== null && !LOCAL_REFERENCE.test(reference)) {
      throw new Error("A source formatting reference was damaged.");
    }
    const referenceAttribute = reference === null ? "" : ` r="${reference}"`;
    if (tag === "lit") {
      if (reference === null || [...node.childNodes].some((child) => child.nodeType !== Node.TEXT_NODE)) {
        throw new Error("Fixed source text was damaged.");
      }
      return `<lit${referenceAttribute}>${escapeText(node.textContent || "")}</lit>`;
    }
    if (VOID_TAGS.has(tag)) {
      if (node.childNodes.length) throw new Error("A source line break was damaged.");
      return `<${tag}${referenceAttribute}/>`;
    }
    return `<${tag}${referenceAttribute}>${[...node.childNodes].map(serialize).join("")}</${tag}>`;
  };
  return [...root.childNodes].map(serialize).join("");
}

export function editorStructureMatches(sourceEditor, content) {
  try {
    return targetStructurePreservesSource(sourceEditor.content, content);
  } catch {
    return false;
  }
}

export function decorateProtectedLiterals(root) {
  const literals = [...(root?.querySelectorAll?.("lit") || [])];
  for (const literal of literals) {
    literal.setAttribute("contenteditable", "false");
    literal.setAttribute("spellcheck", "false");
    literal.setAttribute("title", "Fixed source text");
    literal.removeAttribute("data-boundary-whitespace");
  }
  const boundaryLiteral = (node) => node?.nodeType === Node.ELEMENT_NODE
    && node.localName === "lit"
    && !(node.textContent || "").trim();
  let leading = root?.firstChild || null;
  while (leading?.nodeType === Node.TEXT_NODE && !(leading.nodeValue || "").trim()) {
    leading = leading.nextSibling;
  }
  if (boundaryLiteral(leading)) leading.setAttribute("data-boundary-whitespace", "leading");
  let trailing = root?.lastChild || null;
  while (trailing?.nodeType === Node.TEXT_NODE && !(trailing.nodeValue || "").trim()) {
    trailing = trailing.previousSibling;
  }
  if (boundaryLiteral(trailing)) trailing.setAttribute("data-boundary-whitespace", "trailing");
}

export function decorateSourceContent(root) {
  decorateProtectedLiterals(root);
  return {
    update: () => decorateProtectedLiterals(root)
  };
}

export function compactContentText(content) {
  try {
    const root = parseMarkup(content);
    const pieces = [];
    const visit = (node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        pieces.push(node.nodeValue || "");
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      if (VOID_TAGS.has(node.localName)) pieces.push(" ");
      for (const child of node.childNodes) visit(child);
    };
    for (const child of root.childNodes) visit(child);
    return pieces.join("").replace(/\s+/g, " ").trim();
  } catch {
    return "";
  }
}
