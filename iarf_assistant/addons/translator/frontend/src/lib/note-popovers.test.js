import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent } from '@testing-library/svelte';
import { createHash } from 'node:crypto';
import { installNotePopovers } from './note-popovers.js';
import { preparePreviewDocument } from './preview-document.js';
import { alignmentNotePositions, sentenceNoteParts } from './alignment-notes.js';

let controller;
afterEach(() => {
  controller?.destroy();
  controller = null;
  document.body.replaceChildren();
  vi.useRealTimers();
});

function mount() {
  document.body.innerHTML = '<main><a href="#note" data-carlina-note="note">1</a><details><summary>Notes</summary><li id="note"><p data-note-body>Literal &lt;script&gt; and עברית.</p><button>Edit</button></li></details></main>';
  controller = installNotePopovers(document.querySelector('main'));
  return { trigger: document.querySelector('a'), panel: document.querySelector('[role="dialog"]') };
}

describe('note reading', () => {
  it('preserves inert formatting from source and translator notes without copying attributes or links', async () => {
    const { trigger, panel } = mount();
    document.querySelector('[data-note-body]').innerHTML = '<strong onclick="alert(1)">Bold <em>term</em></strong>, <code>&lt;literal&gt;</code> H<sub>2</sub>O<br><a href="https://example.test">link label</a><script>unsafe()</script><img src="x">';
    await fireEvent.pointerOver(trigger, { pointerType: 'mouse' });
    expect(panel.querySelector('strong em').textContent).toBe('term');
    expect(panel.querySelector('strong').attributes.length).toBe(0);
    expect(panel.querySelector('code').textContent).toBe('<literal>');
    expect(panel.querySelector('sub').textContent).toBe('2');
    expect(panel.querySelector('br')).toBeTruthy();
    expect(panel.querySelector('p a, script, img')).toBeNull();
    expect(panel.querySelector('p').textContent).not.toContain('unsafe()');
  });

  it('opens on hover and focus, allows moving into the note, and dismisses with Escape', async () => {
    vi.useFakeTimers();
    const { trigger, panel } = mount();
    await fireEvent.pointerOver(trigger, { pointerType: 'mouse' });
    expect(panel.hidden).toBe(false);
    expect(panel.querySelector('p').textContent).toBe('Literal <script> and עברית.');
    expect(panel.querySelector('script')).toBeNull();
    await fireEvent.pointerOut(trigger, { relatedTarget: panel });
    await fireEvent.pointerEnter(panel);
    vi.advanceTimersByTime(200);
    expect(panel.hidden).toBe(false);
    trigger.focus();
    await fireEvent.keyDown(trigger, { key: 'Escape' });
    expect(panel.hidden).toBe(true);
    trigger.blur();
    trigger.focus();
    expect(panel.hidden).toBe(false);
  });

  it('pins on tap/click, retains access to the full note and restores focus on close', async () => {
    const { trigger, panel } = mount();
    await fireEvent.click(trigger);
    expect(panel.hidden).toBe(false);
    expect(document.activeElement).toBe(panel);
    await fireEvent.click(panel.querySelector('button'));
    expect(panel.hidden).toBe(true);
    expect(document.activeElement).toBe(trigger);
    await fireEvent.click(trigger);
    await fireEvent.click(panel.querySelector('a'));
    expect(document.querySelector('details').open).toBe(true);
    expect(document.activeElement.id).toBe('note');
    expect(panel.hidden).toBe(true);
  });

  it('prepares local anchors and allows only the exact fixed controller in the preview', async () => {
    const html = await preparePreviewDocument('<head><base href="https://example.com"><meta http-equiv="refresh" content="0;url=https://example.com"></head><body><script>window.authorRan=true</script><a role="doc-noteref" href="#fn%3A1">1</a><aside role="doc-footnote" id="fn:1">A note <a role="doc-backlink" href="#ref">Back</a></aside><p id="ref">Text</p></body>');
    const doc = new DOMParser().parseFromString(html, 'text/html');
    expect(doc.querySelector('base, meta[http-equiv="refresh"]')).toBeNull();
    expect(doc.querySelectorAll('script')).toHaveLength(1);
    const script = doc.querySelector('script').textContent;
    expect(script).not.toContain('authorRan');
    const hash = createHash('sha256').update(script).digest('base64');
    const policy = doc.querySelector('meta').content;
    expect(policy).toContain(`script-src 'sha256-${hash}'`);
    expect(policy).toContain("default-src 'none'");
    expect(policy).not.toContain("script-src 'unsafe-inline'");
    expect(doc.querySelector('[role="doc-noteref"]').getAttribute('href')).toBe('about:srcdoc#fn%3A1');
    expect(doc.querySelector('[data-carlina-note]').getAttribute('data-carlina-note')).toBe('fn:1');
    expect(doc.querySelector('[role="doc-backlink"]').getAttribute('href')).toBe('about:srcdoc#ref');
  });

  it('places repeated and cross-sentence anchors exactly; leaves stale and whole-block notes separate', () => {
    const groups = [
      { source_sentences: [{ order: 1, text: 'עברית.' }], target_sentences: [{ order: 1, text: 'Again. Again.' }] },
      { source_sentences: [{ order: 2, text: 'עוד.' }], target_sentences: [{ order: 2, text: 'Next.' }] }
    ];
    const note = (id, text, occurrence_index = 0, state = 'active', kind = 'target') => ({ id, state, anchor: { kind, text, occurrence_index } });
    const positions = alignmentNotePositions(groups, [note('repeat', 'Again', 1), note('cross', 'Again. Next.'), note('stale', 'Again', 0, 'needs_review'), note('whole', '', 0, 'active', 'whole_block'), note('retired', 'Next', 0, 'retired'), note('source', 'עברית', 0, 'active', 'source')]);
    expect(positions.target.get(1)[0].offset).toBe(12);
    expect(positions.target.get(2)[0].offset).toBe(5);
    expect(positions.source.get(1)[0].offset).toBe(5);
    expect(positions.unplaced.map((item) => item.note.id)).toEqual(['stale', 'whole']);
    const parts = sentenceNoteParts('Again. Again.', positions.target.get(1));
    expect(parts.map((part) => part.text).join('')).toBe('Again. Again.');
    expect(parts[0].text).toBe('Again. Again');
  });
});
