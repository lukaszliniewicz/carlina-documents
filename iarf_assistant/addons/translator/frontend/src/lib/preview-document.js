import { installNotePopovers } from './note-popovers.js';
import { installPreviewSearch } from './preview-search.js';

function focusPreviewAnchor(root, id) {
  const element = root.getElementById(id);
  if (!element) return;
  element.scrollIntoView({ block: "center" });
  element.tabIndex = -1;
  element.focus({ preventScroll: true });
}

export async function preparePreviewDocument(html, { initialAnchor = "" } = {}) {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  // The preview retains an opaque origin. Only our fixed controller receives
  // script permission; author scripts, event handlers and network loads do not.
  doc.querySelectorAll('script, base, meta[http-equiv]').forEach((node) => node.remove());
  for (const link of doc.querySelectorAll('a[href^="#"]')) {
    const fragment = link.getAttribute('href');
    let id;
    try { id = decodeURIComponent(fragment.slice(1)); } catch { continue; }
    link.setAttribute('href', `about:srcdoc${fragment}`);
    const note = doc.getElementById(id);
    if (note && (link.matches('[role="doc-noteref"], [rel~="footnote"], .footnote-ref') ||
        note.matches('[role="doc-footnote"], [role="doc-endnote"], [epub\\:type="footnote"], .footnote'))) {
      link.setAttribute('data-carlina-note', id);
      link.setAttribute('aria-haspopup', 'dialog');
      link.setAttribute('aria-expanded', 'false');
    }
  }
  const anchor = JSON.stringify(String(initialAnchor)).replace(/</g, "\\u003c");
  const source = `(${installNotePopovers.toString()})(document);(${installPreviewSearch.toString()})(document);(${focusPreviewAnchor.toString()})(document,${anchor});`;
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(source));
  const hash = btoa(String.fromCharCode(...new Uint8Array(digest)));
  const policy = doc.createElement('meta');
  policy.httpEquiv = 'Content-Security-Policy';
  policy.content = `default-src 'none'; script-src 'sha256-${hash}'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'`;
  doc.head.prepend(policy);
  const script = doc.createElement('script');
  script.textContent = source;
  doc.body.append(script);
  return `<!doctype html>\n${doc.documentElement.outerHTML}`;
}
