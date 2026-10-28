import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent } from '@testing-library/svelte';
import { installPreviewSearch } from './preview-search.js';

afterEach(() => { document.body.replaceChildren(); document.head.querySelectorAll('style').forEach((node) => node.remove()); vi.useRealTimers(); vi.restoreAllMocks(); });

describe('document preview search', () => {
  it('finds case-insensitive phrases across inline formatting, navigates matches, and clears without losing formatting', async () => {
    vi.useFakeTimers();
    Element.prototype.scrollIntoView = vi.fn();
    document.body.innerHTML = '<p>A <em>clear</em> meaning.</p><p>A CLEAR meaning. עברית.</p><p>Literal [.*] query.</p>';
    const original = document.body.innerHTML;
    installPreviewSearch(document);
    const input = document.querySelector('input');
    const status = document.querySelector('[role="status"]');
    await fireEvent.input(input, { target: { value: 'clear meaning' } });
    vi.advanceTimersByTime(150);
    expect(status.textContent).toBe('1 of 2');
    expect(document.querySelector('em mark').textContent).toBe('clear');
    await fireEvent.click(document.querySelector('[aria-label="Next match"]'));
    expect(status.textContent).toBe('2 of 2');
    await fireEvent.keyDown(input, { key: 'Enter', shiftKey: true });
    expect(status.textContent).toBe('1 of 2');
    await fireEvent.input(input, { target: { value: '[.*]' } });
    await fireEvent.keyDown(input, { key: 'Enter' });
    expect(status.textContent).toBe('1 of 1');
    await fireEvent.input(input, { target: { value: 'עברית' } });
    vi.advanceTimersByTime(150);
    expect(status.textContent).toBe('1 of 1');
    await fireEvent.input(input, { target: { value: 'absent' } });
    vi.advanceTimersByTime(150);
    expect(status.textContent).toBe('No matches');
    expect(document.querySelector('[aria-label="Next match"]').disabled).toBe(true);
    await fireEvent.click(document.querySelector('[aria-label="Clear search"]'));
    document.querySelector('[role="search"]').remove();
    expect(document.body.innerHTML).toBe(original);
  });
});
