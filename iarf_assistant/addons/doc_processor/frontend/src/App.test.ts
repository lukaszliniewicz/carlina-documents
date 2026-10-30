import { afterEach,beforeEach,expect,it,vi } from 'vitest';
import { mount,tick,unmount } from 'svelte';
import App from './App.svelte';
import { loadDocuments } from './lib/api';

vi.mock('./lib/api',async importOriginal=>({...await importOriginal<typeof import('./lib/api')>(),loadReasoningSettings:vi.fn().mockResolvedValue({}),loadModels:vi.fn().mockResolvedValue([]),loadEngines:vi.fn().mockResolvedValue([]),loadPromptEditor:vi.fn().mockResolvedValue({sections:[]}),loadProjects:vi.fn().mockResolvedValue([]),loadDocuments:vi.fn()}));
beforeEach(()=>{
  vi.useFakeTimers();
  Object.defineProperty(HTMLDialogElement.prototype,'showModal',{configurable:true,value:function(){this.open=true;}});
  Object.defineProperty(HTMLDialogElement.prototype,'close',{configurable:true,value:function(){this.open=false;}});
  vi.spyOn(HTMLCanvasElement.prototype,'getContext').mockReturnValue(null);
  vi.stubGlobal('ResizeObserver',class {observe(){} unobserve(){} disconnect(){}});
});
afterEach(()=>{document.body.replaceChildren();vi.useRealTimers();vi.restoreAllMocks();vi.unstubAllGlobals();});
it('recovers polling after a failed request and includes suspended work in attention views',async ()=>{
  const doc={id:'doc-1',filename:'Interrupted.pdf',status:'processing',created_at:'2026-10-30'};
  vi.mocked(loadDocuments).mockResolvedValueOnce([doc]).mockRejectedValueOnce(new Error('Connection interrupted')).mockResolvedValue([{...doc,status:'suspended'}]);
  const target=document.createElement('div');document.body.append(target);
  const component=mount(App,{target});
  await vi.waitFor(()=>expect(loadDocuments).toHaveBeenCalledTimes(1));await tick();
  await vi.advanceTimersByTimeAsync(3000);await tick();
  expect(target.textContent).toContain('retrying automatically');
  await vi.advanceTimersByTimeAsync(6000);await tick();
  expect(loadDocuments).toHaveBeenCalledTimes(3);
  expect(target.textContent).not.toContain('retrying automatically');
  const filter=target.querySelector<HTMLSelectElement>('.dp-library-tools select')!;
  filter.value='review';filter.dispatchEvent(new Event('change',{bubbles:true}));await tick();
  expect(target.textContent).toContain('Interrupted.pdf');
  expect(target.textContent).toContain('Suspended');
  const summary=target.querySelector('[aria-label="Document library summary"]')!;
  expect([...summary.querySelectorAll('div')].find(e=>e.textContent?.includes('Review'))?.querySelector('strong')?.textContent).toBe('1');
  await unmount(component);
});

it('clears the manual refresh failure after recovery without leaving a stale error',async ()=>{
  const doc={id:'doc-1',filename:'Ready.pdf',status:'completed',created_at:'2026-10-30'};
  vi.mocked(loadDocuments).mockReset().mockResolvedValueOnce([doc]).mockRejectedValueOnce(new Error('Failed to fetch')).mockResolvedValue([doc]);
  const target=document.createElement('div');document.body.append(target);
  const component=mount(App,{target});
  await vi.waitFor(()=>expect(target.textContent).toContain('Ready.pdf'));
  [...target.querySelectorAll('button')].find(b=>b.textContent?.trim()==='Refresh')!.click();
  await vi.waitFor(()=>expect(target.textContent).toContain('retrying automatically'));
  expect(target.querySelector('[role="alert"]')).toBeNull();
  await vi.advanceTimersByTimeAsync(6000);await tick();
  expect(loadDocuments).toHaveBeenCalledTimes(3);
  expect(target.textContent).not.toContain('retrying automatically');
  expect(target.textContent).not.toContain('Failed to fetch');
  expect(target.textContent).toContain('Ready.pdf');
  await unmount(component);
});
