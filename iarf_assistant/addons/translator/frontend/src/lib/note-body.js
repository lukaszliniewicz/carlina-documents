// The server supplies one derived rendering for notes and exports. Rebuild only
// its inert inline vocabulary in the browser; never trust arbitrary API HTML.
export function renderNoteBody(element, note) {
  function update(value) {
    if (typeof value?.body_html !== "string") {
      element.textContent = value?.body || "";
      return;
    }
    const doc = element.ownerDocument;
    const parsed = new doc.defaultView.DOMParser().parseFromString(value.body_html, "text/html");
    const allowed = new Set(["EM", "STRONG", "CODE", "BR"]);
    if (parsed.head.childNodes.length || [...parsed.body.querySelectorAll("*")].some((node) => !allowed.has(node.tagName) || node.attributes.length)) {
      element.textContent = value.body || "";
      return;
    }
    element.replaceChildren(...[...parsed.body.childNodes].map((node) => doc.importNode(node, true)));
  }
  update(note);
  return { update };
}
