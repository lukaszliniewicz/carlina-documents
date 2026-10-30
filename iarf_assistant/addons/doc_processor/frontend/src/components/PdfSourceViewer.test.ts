import { afterEach,expect,it,vi } from 'vitest';
import { mount,tick,unmount } from 'svelte';
import PdfSourceViewer from './PdfSourceViewer.svelte';
import { loadCropPages } from '../lib/api';

vi.mock('../lib/api',()=>({loadCropPages:vi.fn(),getPdfPageImageUrl:(id:string,page:number)=>`/page/${id}/${page}`,getSourceFileUrl:()=>'/source.pdf'}));
afterEach(()=>{document.body.replaceChildren();vi.restoreAllMocks();});
it('renders one page at a time and exposes paging, zoom and retry without a PDF plugin',async ()=>{
  vi.mocked(loadCropPages).mockResolvedValue({total_pages:360,pages:[]} as never);
  const target=document.createElement('div');document.body.append(target);
  const component=mount(PdfSourceViewer,{target,props:{documentId:'doc-1',filename:'Book.pdf'}});
  await vi.waitFor(()=>expect(target.querySelector('img')?.getAttribute('src')).toContain('/page/doc-1/0'));
  expect(target.querySelectorAll('img')).toHaveLength(1);
  expect(target.querySelector('iframe')).toBeNull();
  target.querySelector<HTMLButtonElement>('[aria-label="Next PDF page"]')!.click();await tick();
  expect(target.querySelector('img')?.getAttribute('src')).toContain('/page/doc-1/1');
  target.querySelector('img')!.dispatchEvent(new Event('error'));await tick();
  expect(target.textContent).toContain('could not be loaded');
  [...target.querySelectorAll('button')].find(b=>b.textContent?.trim()==='Retry page')!.click();await tick();
  expect(target.querySelector('img')?.getAttribute('src')).toContain('retry=1');
  await unmount(component);
});
