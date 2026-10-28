// Keep this function self-contained: the same controller runs in the isolated
// preview under a CSP hash of its exact, bundled source.
export function installNotePopovers(root) {
  const doc = root.ownerDocument || root;
  const win = doc.defaultView;
  const selector = '[data-carlina-note]';
  const panel = doc.createElement('div');
  panel.className = 'carlina-note-popover';
  panel.id = `carlina-note-popover-${Math.random().toString(36).slice(2)}`;
  panel.setAttribute('role', 'dialog');
  panel.tabIndex = -1;
  panel.setAttribute('aria-label', 'Note');
  panel.hidden = true;
  const style = doc.createElement('style');
  style.textContent = `
    .carlina-note-popover { position:fixed; z-index:10000; box-sizing:border-box; width:25rem; max-width:calc(100vw - 24px); max-height:calc(100vh - 24px); overflow:auto; padding:1rem; border:1px solid #b9ad9d; border-radius:.5rem; background:#fffdf8; color:#302d29; box-shadow:0 .4rem 1.5rem #302d2933; font:16px/1.6 Georgia,serif; text-align:start; }
    .carlina-note-popover[hidden] { display:none; }
    .carlina-note-popover p { margin:0; white-space:pre-wrap; overflow-wrap:anywhere; }
    .carlina-note-popover footer { display:flex; justify-content:space-between; align-items:center; gap:1rem; margin-top:.75rem; font:13px/1.4 system-ui,sans-serif; }
    .carlina-note-popover a, .carlina-note-popover button { color:#67503c; }
    .carlina-note-popover button { border:1px solid #b9ad9d; border-radius:.25rem; padding:.3rem .6rem; background:transparent; font:inherit; cursor:pointer; }
    .carlina-note-popover :focus-visible { outline:2px solid #67503c; outline-offset:3px; }
  `;
  const body = doc.createElement('p');
  body.id = `${panel.id}-body`;
  panel.setAttribute('aria-describedby', body.id);
  body.dir = 'auto';
  const footer = doc.createElement('footer');
  const fullNote = doc.createElement('a');
  fullNote.textContent = 'Go to note';
  const close = doc.createElement('button');
  close.type = 'button';
  close.textContent = 'Close';
  footer.append(fullNote, close);
  panel.append(body, footer);
  doc.head.append(style);
  doc.body.append(panel);
  let active = null;
  let pinned = false;
  let timer;
  let suppressed = null;

  function position() {
    if (!active || panel.hidden) return;
    if (!active.isConnected) { hide(); return; }
    const rect = active.getBoundingClientRect();
    const box = panel.getBoundingClientRect();
    const left = Math.max(12, Math.min(rect.left, win.innerWidth - box.width - 12));
    const below = rect.bottom + 8;
    const top = below + box.height <= win.innerHeight - 12 ? below : Math.max(12, rect.top - box.height - 8);
    panel.style.left = `${left}px`;
    panel.style.top = `${top}px`;
  }

  function hide(restoreFocus = false) {
    win.clearTimeout(timer);
    const previous = active;
    active?.setAttribute('aria-expanded', 'false');
    panel.hidden = true;
    active = null;
    pinned = false;
    if (restoreFocus && previous?.isConnected) {
      suppressed = previous;
      previous.focus({ preventScroll: true });
    }
  }

  function show(trigger) {
    win.clearTimeout(timer);
    if (suppressed === trigger) return;
    if (active === trigger) return;
    hide();
    const note = doc.getElementById(trigger.getAttribute('data-carlina-note'));
    if (!note) return;
    const copy = (note.querySelector('[data-note-body]') || note).cloneNode(true);
    copy.querySelectorAll('[role="doc-backlink"], [data-note-backlink], a[rev="footnote"], .footnote-backref').forEach((link) => link.remove());
    // Reconstruct inert formatting only, including existing source footnotes.
    // Attributes, links, images and executable/document nodes never enter the popup.
    const inlineTags = new Set(['EM', 'I', 'STRONG', 'B', 'CODE', 'SUB', 'SUP', 'U', 'BR']);
    const omittedTags = new Set(['SCRIPT', 'STYLE', 'TEMPLATE', 'IFRAME', 'OBJECT', 'EMBED', 'SVG', 'MATH', 'IMG', 'INPUT', 'BUTTON']);
    function appendReadingNodes(source, destination) {
      for (const node of source.childNodes) {
        if (node.nodeType === 3) destination.append(doc.createTextNode(node.textContent));
        else if (node.nodeType === 1 && !omittedTags.has(node.tagName)) {
          const container = inlineTags.has(node.tagName) ? doc.createElement(node.tagName.toLowerCase()) : doc.createDocumentFragment();
          appendReadingNodes(node, container);
          destination.append(container);
          if (['P', 'LI', 'BLOCKQUOTE', 'DIV'].includes(node.tagName)) destination.append(doc.createTextNode('\n'));
        }
      }
    }
    body.replaceChildren();
    appendReadingNodes(copy, body);
    panel.setAttribute('aria-label', trigger.getAttribute('aria-label') || `Note ${trigger.textContent.trim()}`);
    fullNote.setAttribute('href', trigger.getAttribute('href'));
    active = trigger;
    trigger.setAttribute('aria-haspopup', 'dialog');
    trigger.setAttribute('aria-controls', panel.id);
    trigger.setAttribute('aria-expanded', 'true');
    panel.hidden = false;
    panel.scrollTop = 0;
    position();
  }

  function scheduleHide() {
    win.clearTimeout(timer);
    timer = win.setTimeout(() => {
      if (!pinned && !panel.matches(':hover') && !active?.matches(':hover') &&
          !panel.contains(doc.activeElement) && doc.activeElement !== active) hide();
    }, 180);
  }

  function over(event) {
    const trigger = event.target.closest?.(selector);
    if (trigger && root.contains(trigger) && event.pointerType !== 'touch') show(trigger);
  }
  function out(event) {
    const trigger = event.target.closest?.(selector);
    if (trigger && !trigger.contains(event.relatedTarget)) {
      if (suppressed === trigger) suppressed = null;
      scheduleHide();
    }
  }
  function focus(event) {
    const trigger = event.target.closest?.(selector);
    if (trigger && root.contains(trigger)) show(trigger);
  }
  function blur(event) {
    if (event.target === suppressed) suppressed = null;
    scheduleHide();
  }
  function click(event) {
    const trigger = event.target.closest?.(selector);
    if (!trigger || !root.contains(trigger) || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    event.stopPropagation();
    suppressed = null;
    show(trigger);
    pinned = true;
    panel.focus({ preventScroll: true });
  }
  function outside(event) {
    if (!panel.contains(event.target) && !active?.contains(event.target)) hide();
  }
  function key(event) {
    if (event.key === 'Escape' && active) {
      event.preventDefault();
      event.stopPropagation();
      suppressed = active;
      hide(panel.contains(doc.activeElement));
    }
  }
  function navigate() {
    const target = active && doc.getElementById(active.getAttribute('data-carlina-note'));
    if (target) {
      for (let parent = target.parentElement; parent; parent = parent.parentElement) {
        if (parent.tagName === 'DETAILS') parent.open = true;
      }
      target.setAttribute('tabindex', '-1');
      target.focus({ preventScroll: true });
      target.scrollIntoView?.({ block: 'center' });
    }
    hide();
  }
  const listeners = [
    [root, 'pointerover', over], [root, 'pointerout', out], [root, 'focusin', focus],
    [root, 'focusout', blur], [root, 'click', click],
    [doc, 'pointerdown', outside], [doc, 'keydown', key],
    [win, 'resize', position], [doc, 'scroll', position, true],
    [panel, 'pointerenter', () => win.clearTimeout(timer)], [panel, 'pointerleave', scheduleHide],
    [panel, 'focusout', scheduleHide], [close, 'click', () => hide(true)], [fullNote, 'click', navigate]
  ];
  listeners.forEach(([node, name, handler, capture]) => node.addEventListener(name, handler, capture));
  return { destroy() {
    hide();
    listeners.forEach(([node, name, handler, capture]) => node.removeEventListener(name, handler, capture));
    panel.remove();
    style.remove();
  } };
}
