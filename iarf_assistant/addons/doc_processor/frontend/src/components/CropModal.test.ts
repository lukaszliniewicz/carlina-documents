import { afterEach,beforeEach,expect,it,vi } from 'vitest';
import { mount,tick,unmount } from 'svelte';
import CropModal from './CropModal.svelte';
import { loadCropPages,loadCropPreview } from '../lib/api';

vi.mock('../lib/api',()=>({loadCropPages:vi.fn(),loadCropPreview:vi.fn(),saveCrop:vi.fn()}));
beforeEach(()=>{
  Object.defineProperty(HTMLDialogElement.prototype,'showModal',{configurable:true,value:function(){this.open=true;}});
  Object.defineProperty(HTMLDialogElement.prototype,'close',{configurable:true,value:function(){this.open=false;}});
  vi.spyOn(HTMLCanvasElement.prototype,'getContext').mockReturnValue(null);
  vi.stubGlobal('ResizeObserver',class { observe(){} unobserve(){} disconnect(){} });
  vi.mocked(loadCropPages).mockResolvedValue({success:true,total_pages:360,crop_settings:{},pages:Array.from({length:360},(_,i)=>({page_index:i,page_number:i+1,thumbnail_url:`/pages/${i}`,in_page_range:true}))});
  vi.mocked(loadCropPreview).mockResolvedValue({success:true} as never);
});
afterEach(()=>{document.body.replaceChildren();vi.restoreAllMocks();vi.unstubAllGlobals();});

it('keeps a long PDF bounded, jumps to its final page, and protects exclusions',async ()=>{
  const target=document.createElement('div');document.body.append(target);const closed=vi.fn();
  const component=mount(CropModal,{target,props:{open:true,documentId:'doc-1'},events:{close:closed}});
  await vi.waitFor(()=>expect(target.textContent).toContain('360/360 included'));
  expect(target.querySelectorAll('.dp-crop-page-card').length).toBeLessThan(15);
  expect(target.querySelector('.dp-crop-page-card .dp-crop-flags')?.textContent).toContain('Right');
  const jump=target.querySelector<HTMLInputElement>('[aria-label="Preview PDF page"]')!;
  jump.value='360';jump.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));await tick();
  expect(target.querySelector('.dp-crop-page-card.is-selected')?.textContent).toContain('Page 360');
  expect(target.querySelector('.dp-crop-page-card.is-selected .dp-crop-flags')?.textContent).toContain('Left');
  expect(target.querySelector<HTMLElement>('.dp-crop-page-list')!.scrollTop).toBe(359*224);
  const card=target.querySelector('.dp-crop-page-card.is-selected')!;
  [...card.querySelectorAll('button')].find(b=>b.textContent?.trim()==='Exclude')!.click();await tick();
  expect(target.textContent).toContain('359/360 included');
  target.querySelector<HTMLButtonElement>('[aria-label="Close"]')!.click();await tick();
  await vi.waitFor(()=>expect([...document.querySelectorAll('button')].some(b=>b.textContent?.trim()==='Keep editing')).toBe(true));
  [...document.querySelectorAll('button')].find(b=>b.textContent?.trim()==='Keep editing')!.click();await tick();
  expect(closed).not.toHaveBeenCalled();
  expect(target.textContent).toContain('359/360 included');
  await unmount(component);
});
