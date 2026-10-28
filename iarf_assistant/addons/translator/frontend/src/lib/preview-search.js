// Serialized into the opaque preview with an exact CSP hash. Keep self-contained.
export function installPreviewSearch(doc) {
  const win = doc.defaultView;
  const bar = doc.createElement('div');
  bar.className = 'carlina-preview-search';
  bar.setAttribute('role', 'search');
  bar.setAttribute('aria-label', 'Search document');
  const input = doc.createElement('input');
  input.type = 'search';
  input.placeholder = 'Search document';
  input.setAttribute('aria-label', 'Search document');
  input.maxLength = 200;
  const status = doc.createElement('span');
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  const button = (label, text, action) => {
    const node = doc.createElement('button');
    node.type = 'button';
    node.textContent = text;
    node.title = label;
    node.setAttribute('aria-label', label);
    node.addEventListener('click', action);
    return node;
  };
  const previous = button('Previous match', '↑', () => select(-1));
  const next = button('Next match', '↓', () => select(1));
  const clear = button('Clear search', 'Clear', () => { input.value = ''; search(); input.focus(); });
  bar.append(input, status, previous, next, clear);
  const style = doc.createElement('style');
  style.textContent = `
    .carlina-preview-search { position:sticky; top:0; z-index:200; display:flex; flex-wrap:wrap; align-items:center; gap:.4rem; padding:.6rem; margin-bottom:1rem; border:1px solid #cfc5b7; background:#fffdf8; color:#302d29; font:14px/1.4 system-ui,sans-serif; }
    .carlina-preview-search input { box-sizing:border-box; flex:1 1 9rem; width:9rem; min-width:0; padding:.45rem; border:1px solid #b9ad9d; background:#fff; color:#302d29; font:inherit; }
    .carlina-preview-search button { padding:.4rem .65rem; border:1px solid #b9ad9d; background:transparent; color:inherit; cursor:pointer; font:inherit; }
    .carlina-preview-search button:disabled { opacity:.45; cursor:default; }
    .carlina-preview-search :focus-visible { outline:2px solid #67503c; outline-offset:2px; }
    mark.carlina-search-match { background:#ffe69a; color:#302d29; }
    mark.carlina-search-match.is-current { background:#e7aa58; outline:2px solid #805516; }
  `;
  doc.head.append(style);
  doc.body.prepend(bar);
  let groups = [];
  let selected = -1;
  let capped = false;
  let timer;

  function select(delta) {
    groups[selected]?.forEach((mark) => mark.classList.remove('is-current'));
    selected = groups.length ? (selected + delta + groups.length) % groups.length : -1;
    groups[selected]?.forEach((mark) => mark.classList.add('is-current'));
    groups[selected]?.[0].scrollIntoView({ block: 'center' });
    status.textContent = input.value ? groups.length ? `${selected + 1} of ${groups.length}${capped ? '+' : ''}` : 'No matches' : '';
    previous.disabled = next.disabled = !groups.length;
    clear.disabled = !input.value;
  }

  function search() {
    win.clearTimeout(timer);
    timer = undefined;
    for (const group of groups) for (const mark of group) {
      const parent = mark.parentNode;
      mark.replaceWith(doc.createTextNode(mark.textContent));
      parent.normalize();
    }
    groups = [];
    selected = -1;
    capped = false;
    const query = input.value;
    if (!query) { select(0); return; }
    const walker = doc.createTreeWalker(doc.body, 4);
    const nodes = [];
    let text = '';
    let previousBlock;
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (!node.textContent || node.parentElement.closest('script,style,noscript,.carlina-preview-search,.carlina-note-popover,[hidden]')) continue;
      const block = node.parentElement.closest('p,li,td,th,h1,h2,h3,h4,h5,h6,blockquote,pre');
      if (block !== previousBlock) text += '\n';
      previousBlock = block;
      nodes.push({ node, start: text.length, end: text.length + node.textContent.length });
      text += node.textContent;
    }
    const pattern = new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'giu');
    const matches = [];
    for (const match of text.matchAll(pattern)) {
      if (matches.length === 2000) { capped = true; break; }
      matches.push({ start: match.index, end: match.index + match[0].length });
    }
    groups = matches.map(() => []);
    // Work backwards so splitting text nodes cannot invalidate earlier offsets.
    for (let i = matches.length - 1; i >= 0; i--) {
      const match = matches[i];
      for (let j = nodes.length - 1; j >= 0; j--) {
        const item = nodes[j];
        if (item.end <= match.start) break;
        if (item.start >= match.end) continue;
        const range = doc.createRange();
        range.setStart(item.node, Math.max(0, match.start - item.start));
        range.setEnd(item.node, Math.min(item.end, match.end) - item.start);
        const mark = doc.createElement('mark');
        mark.className = 'carlina-search-match';
        range.surroundContents(mark);
        groups[i].unshift(mark);
      }
    }
    select(1);
  }
  input.addEventListener('input', () => { win.clearTimeout(timer); timer = win.setTimeout(search, 150); });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); if (timer) search(); else select(event.shiftKey ? -1 : 1); }
    if (event.key === 'Escape') { event.preventDefault(); input.value = ''; search(); }
  });
  select(0);
}
