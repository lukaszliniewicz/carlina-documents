import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import ExportModal from "./ExportModal.svelte";
import ProjectExportModal from "./ProjectExportModal.svelte";
import { requestJson } from "../lib/api";

vi.mock("../lib/api", () => ({
  requestJson: vi.fn(async (url: string, init?: RequestInit) => String(url).endsWith("/epub/edition") ? {
    version: 0, recipe: null, effective_recipe: null, source_revision: null, current_source_revision: "d".repeat(64), stale: false,
  } : String(url).endsWith("/epub/preflight") ? {
    revision: "a".repeat(64), edition_revision: "d".repeat(64), review_hash: "c".repeat(64), options: JSON.parse(String(init?.body || "{}")).epub_options,
    outline: { input_sha256: "a".repeat(64), headings: [], tables: [], images: [], pages: [] }, suggested_chapter_level: 2,
    quality: { schema: "epub-quality-v1", profile: "publishing-v1", input_sha256: "a".repeat(64), options_sha256: "b".repeat(64), artifact_sha256: "0".repeat(64), content_sha256: "0".repeat(64), counts: {}, epubcheck_version: null, issue_count: 0, issues: [], checks: { structure: "not_run", epubcheck: "not_run", ace: "not_run", manual: "not_reviewed" } }
  } : { job_id: "job-1" }),
  waitForJob: vi.fn(async () => ({ result: { download_url: "/prepared/book" } }))
}));

const defaultRequestHandler = vi.mocked(requestJson).getMockImplementation()!;
beforeEach(() => vi.mocked(requestJson).mockReset().mockImplementation(defaultRequestHandler));

async function waitEditionLoad(target: HTMLElement) {
  await vi.waitFor(() => {
    const button = Array.from(target.querySelectorAll("button")).find((item) => item.textContent?.trim() === "Save edition choices");
    expect(button?.matches(":disabled")).toBe(false);
  });
}

afterEach(() => { document.body.replaceChildren(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("reviews a replacement cover without carrying across the old description", async () => {
  vi.mocked(requestJson).mockClear();
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value: function () { this.open = true; } });
  Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value: function () { this.open = false; } });
  const preflight = {
    revision: "a".repeat(64), review_hash: "c".repeat(64), options: {}, suggested_chapter_level: 2,
    outline: { input_sha256: "a".repeat(64), headings: [], tables: [], images: [
      { id: "old-cover", alt: "Old painting", decorative: false, is_cover: true },
      { id: "art", alt: "Interior image", decorative: false, is_cover: false },
    ], pages: [] },
    quality: { schema: "epub-quality-v1", profile: "publishing-v1", input_sha256: "a".repeat(64), options_sha256: "b".repeat(64), artifact_sha256: "0".repeat(64), content_sha256: "0".repeat(64), counts: {}, epubcheck_version: null, issue_count: 0, issues: [], checks: { structure: "not_run", epubcheck: "not_run", ace: "not_run", manual: "not_reviewed" } },
  };
  const defaultRequest = vi.mocked(requestJson).getMockImplementation()!;
  vi.mocked(requestJson).mockImplementation(async (url, init) => String(url).endsWith("/epub/preflight") ? preflight : defaultRequest(url, init));
  const target = document.createElement("div"); document.body.append(target);
  const component = mount(ExportModal, { target, props: { open: true, documentId: "cover-review", document: { id: "cover-review", filename: "book.pdf", status: "completed", created_at: "2026-10-10" } } });
  await tick();
  (target.querySelector('input[value="epub"]') as HTMLInputElement).click(); await tick();
  await waitEditionLoad(target);
  const review = () => Array.from(target.querySelectorAll("button")).find((button) => button.textContent?.trim().startsWith("Review"));
  review()?.click();
  await vi.waitFor(() => expect(target.querySelectorAll("textarea")).toHaveLength(2));
  for (const [index, value] of ["Reviewed old painting", "Reviewed interior image"].entries()) {
    const input = target.querySelectorAll("textarea")[index]; input.value = value; input.dispatchEvent(new Event("change", { bubbles: true })); await tick();
  }
  (target.querySelector('input[value="generated"]') as HTMLInputElement).click(); await tick();
  review()?.click();
  await vi.waitFor(() => expect(vi.mocked(requestJson).mock.calls.filter(([url]) => String(url).endsWith("/epub/preflight"))).toHaveLength(2));
  expect(JSON.parse(String(vi.mocked(requestJson).mock.lastCall?.[1]?.body))).toMatchObject({
    epub_cover: { mode: "generated" }, epub_options: { image_descriptions: { art: "Reviewed interior image" }, accessibility_evaluation: null },
  });
  expect(JSON.parse(String(vi.mocked(requestJson).mock.lastCall?.[1]?.body)).epub_options.image_descriptions["old-cover"]).toBeUndefined();
  await unmount(component);
  vi.mocked(requestJson).mockImplementation(defaultRequest);
});

it("discards a review response when the cover changes while it is pending", async () => {
  const result = await requestJson("/fixture/epub/preflight", { body: JSON.stringify({ epub_options: {} }) });
  vi.mocked(requestJson).mockClear();
  let finish: () => void = () => { throw new Error("Review has not started"); };
  const defaultRequest = vi.mocked(requestJson).getMockImplementation()!;
  vi.mocked(requestJson).mockImplementation((url, init) => String(url).endsWith("/epub/preflight") ? new Promise((resolve) => { finish = () => resolve(result); }) : defaultRequest(url, init));
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value: function () { this.open = true; } });
  Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value: function () { this.open = false; } });
  const target = document.createElement("div"); document.body.append(target);
  const component = mount(ExportModal, { target, props: { open: true, documentId: "pending-cover" } });
  await tick(); (target.querySelector('input[value="epub"]') as HTMLInputElement).click(); await tick();
  await waitEditionLoad(target);
  Array.from(target.querySelectorAll("button")).find((button) => button.textContent?.trim() === "Review EPUB")?.click();
  await vi.waitFor(() => expect(vi.mocked(requestJson).mock.calls.filter(([url]) => String(url).endsWith("/epub/preflight"))).toHaveLength(1));
  (target.querySelector('input[value="generated"]') as HTMLInputElement).click(); await tick();
  finish();
  await vi.waitFor(() => expect(target.querySelector('[role="alert"]')?.textContent).toContain("cover changed during review"));
  expect(target.querySelector("textarea")).toBeNull();
  await unmount(component);
  vi.mocked(requestJson).mockImplementation(defaultRequest);
});

it.each(["auto", "jpeg", "webp"])("loads and saves a cropped %s edition with fresh source and version witnesses", async (imageMode) => {
  const recipe = { epub_options: { profile: "publishing-v2", direction: "rtl" }, epub_cover: { mode: "pdf_page", page: 3, crop: [0.1, 0, 0.9, 1] }, epub_image_mode: imageMode };
  const edition = { version: 3, recipe, effective_recipe: recipe, source_revision: "d".repeat(64), current_source_revision: "d".repeat(64), stale: false };
  let savedBody: Record<string, unknown> | undefined;
  vi.mocked(requestJson).mockImplementation(async (url, init) => {
    if (String(url).endsWith("/epub/edition")) {
      if (init?.method === "PUT") { savedBody = JSON.parse(String(init.body)); return { ...edition, version: 4 }; }
      return edition;
    }
    return defaultRequestHandler(url, init);
  });
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value: function () { this.open = true; } });
  Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value: function () { this.open = false; } });
  const target = document.createElement("div"); document.body.append(target);
  const component = mount(ExportModal, { target, props: { open: true, documentId: "saved-book", document: { id: "saved-book", filename: "book.pdf", status: "completed", created_at: "2026-10-11" } } });
  await tick(); (target.querySelector('input[value="epub"]') as HTMLInputElement).click(); await tick();
  await waitEditionLoad(target);
  expect((target.querySelector('select') as HTMLSelectElement).value).toBe(imageMode);
  const left = Array.from(target.querySelectorAll("label")).find(label => label.textContent?.trim() === "Left (%)")?.querySelector("input");
  expect(left?.value).toBe("10");
  Array.from(target.querySelectorAll("button")).find(button => button.textContent?.trim() === "Save edition choices")?.click();
  await vi.waitFor(() => expect(savedBody).toEqual({ expected_version: 3, expected_source_revision: "d".repeat(64), recipe }));
  await vi.waitFor(() => expect(target.textContent).toContain("Edition choices saved."));
  await unmount(component);
});

it.each(["document", "project"])("sends the chosen JPEG format from the %s export", async (kind) => {
  vi.mocked(requestJson).mockClear();
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value: function () { this.open = true; } });
  Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value: function () { this.open = false; } });
  vi.stubGlobal("URL", Object.assign(class extends URL {}, { createObjectURL: () => "blob:jpeg-book", revokeObjectURL: vi.fn() }));
  vi.stubGlobal("fetch", vi.fn(async () => new Response("synthetic EPUB")));
  const target = document.createElement("div"); document.body.append(target);
  const component = kind === "document"
    ? mount(ExportModal, { target, props: { open: true, documentId: "jpeg-book" } })
    : mount(ProjectExportModal, { target, props: { open: true, projectId: "jpeg-project" } });
  await tick(); (target.querySelector('input[value="epub"]') as HTMLInputElement).click(); await tick();
  if (kind === "document") await waitEditionLoad(target);
  const selector = target.querySelector("select") as HTMLSelectElement;
  selector.value = "jpeg"; selector.dispatchEvent(new Event("change", { bubbles: true })); await tick();
  Array.from(target.querySelectorAll("button")).find((button) => button.textContent?.trim() === "Download")?.click();
  await vi.waitFor(() => expect(vi.mocked(requestJson).mock.calls.some(([url]) => String(url).includes("epub_image_mode=jpeg"))).toBe(true));
  if (kind === "document") {
    const review = vi.mocked(requestJson).mock.calls.find(([url]) => String(url).endsWith("/epub/preflight"));
    expect(JSON.parse(String(review?.[1]?.body)).epub_image_mode).toBe("jpeg");
  }
  await vi.waitFor(() => expect(target.querySelector("a[download]")).toBeTruthy());
  await unmount(component);
});

it.each(["pdf_page", "generated"])("exports the selected %s cover without changing the source", async (mode) => {
  vi.mocked(requestJson).mockClear();
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value: function () { this.open = true; } });
  Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value: function () { this.open = false; } });
  vi.stubGlobal("URL", Object.assign(class extends URL {}, { createObjectURL: () => "blob:cover-book", revokeObjectURL: vi.fn() }));
  vi.stubGlobal("fetch", vi.fn(async () => new Response("synthetic EPUB")));
  const source = { id: "document-cover", filename: "book.pdf", status: "completed", created_at: "2026-10-10", metadata: { title: "Book title", cover_candidate: { page: 3, kind: "title_page" as const, evidence: "Title and author are visible." } } };
  const before = JSON.stringify(source);
  const target = document.createElement("div");
  document.body.append(target);
  const component = mount(ExportModal, { target, props: { open: true, documentId: source.id, document: source } });
  await tick();
  (target.querySelector('input[value="epub"]') as HTMLInputElement).click();
  await tick();
  await waitEditionLoad(target);
  expect((target.querySelector("fieldset.epub-image-format select") as HTMLSelectElement).value).toBe("jpeg");
  (target.querySelector(`input[value="${mode}"]`) as HTMLInputElement).click();
  await tick();
  if (mode === "pdf_page") {
    expect(target.textContent).toContain("Suggested page 3");
    expect(target.querySelector("img")?.getAttribute("src")).toContain("pdf-pages/2/thumbnail");
  }
  Array.from(target.querySelectorAll("button")).find((item) => item.textContent?.trim() === "Download")?.click();
  await vi.waitFor(() => expect(vi.mocked(requestJson).mock.calls.some(([url]) => String(url).includes("/export?"))).toBe(true));
  const query = new URL(String(vi.mocked(requestJson).mock.lastCall?.[0]), "http://localhost").searchParams;
  expect(query.has("epub_cover_mode")).toBe(false);
  expect(query.get("epub_image_mode")).toBe("jpeg");
  const exportRequest = vi.mocked(requestJson).mock.lastCall?.[1];
  expect(exportRequest?.method).toBe("POST");
  expect(JSON.parse(String(exportRequest?.body))).toMatchObject({ epub_options: {}, epub_cover: mode === "pdf_page" ? { mode, page: 3 } : { mode }, expected_revision: "a".repeat(64), epub_review_hash: "c".repeat(64) });
  const reviewRequest = vi.mocked(requestJson).mock.calls.find(([url]) => String(url).endsWith("/epub/preflight"))?.[1];
  expect(JSON.parse(String(reviewRequest?.body))).toMatchObject({ epub_image_mode: "jpeg", epub_cover: { mode } });
  expect(JSON.stringify(source)).toBe(before);
  await vi.waitFor(() => expect(target.querySelector("a[download]")).toBeTruthy());
  await unmount(component);
});

it.each([["document", "epub"], ["project", "epub"], ["document", "pdf"], ["project", "pdf"]])("keeps %s %s exports available as explicit downloads", async (kind, format) => {
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value: function () { this.open = true; } });
  Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value: function () { this.open = false; } });
  const revokeObjectURL = vi.fn();
  vi.stubGlobal("URL", Object.assign(class extends URL {}, { createObjectURL: () => "blob:prepared-book", revokeObjectURL }));
  vi.stubGlobal("fetch", vi.fn(async () => new Response("prepared EPUB")));
  const open = vi.spyOn(window, "open").mockImplementation(() => null);
  const target = document.createElement("div");
  document.body.append(target);
  const component = kind === "document"
    ? mount(ExportModal, { target, props: { open: true, documentId: "document-1" } })
    : mount(ProjectExportModal, { target, props: { open: true, projectId: "project-1" } });
  await tick();
  const option = target.querySelector(`input[value="${format}"]`) as HTMLInputElement;
  option.checked = true;
  option.dispatchEvent(new Event("change", { bubbles: true }));
  await tick();
  if (kind === "document" && format === "epub") await waitEditionLoad(target);
  if (format === "pdf") {
    const pagination = target.querySelector('fieldset input[type="checkbox"]') as HTMLInputElement;
    pagination.click();
    await tick();
  }
  const button = Array.from(target.querySelectorAll("button")).find((item) => item.textContent?.trim() === "Download");
  button?.click();
  await vi.waitFor(() => expect(target.querySelector('a[download]')).toBeTruthy());
  const link = target.querySelector('a[download]') as HTMLAnchorElement;
  expect(link.getAttribute("href")).toBe("blob:prepared-book");
  expect(link.getAttribute("download")).toMatch(kind === "project" && format === "epub" ? /\.zip$/ : new RegExp(`\\.${format}$`));
  expect(open).not.toHaveBeenCalled();
  if (format === "pdf") {
    expect(vi.mocked(requestJson).mock.lastCall?.[0]).toContain("start_h2_on_new_page=true");
  }
  await unmount(component);
  expect(revokeObjectURL).toHaveBeenCalledWith("blob:prepared-book");
});
