import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import ImageReviewModal from "./ImageReviewModal.svelte";
import { loadCompletedImageCrops, loadImageReviewPages, saveCompletedImageCrops, saveImageReviewPages } from "../lib/api";
import type { ImageReviewPage } from "../lib/types";

vi.mock("../lib/api", () => ({
  loadCompletedImageCrops: vi.fn(), loadImageReviewPages: vi.fn(),
  saveCompletedImageCrops: vi.fn(), saveImageReviewPages: vi.fn()
}));

const page: ImageReviewPage = {
  page_index: 0, image_url: "/uploads/source.png", label: "Page 3", source_kind: "page", source_page_index: 2,
  images: [{ img_idx: 0, bbox: [100, 200, 700, 800], caption: "Saved caption", keep: true }]
};

beforeEach(() => {
  vi.resetAllMocks();
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value: function () { this.open = true; } });
  Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value: function () { this.open = false; } });
  vi.mocked(loadCompletedImageCrops).mockResolvedValue({ pages: [structuredClone(page)], expected_updated_at: "revision-1", page_count: 5 });
  vi.mocked(loadImageReviewPages).mockResolvedValue([structuredClone(page)]);
  vi.mocked(saveCompletedImageCrops).mockResolvedValue({ success: true });
});
afterEach(() => { document.body.replaceChildren(); });

async function openEditor(completed = true) {
  const target = document.createElement("div");
  document.body.append(target);
  const saved = vi.fn();
  const component = mount(ImageReviewModal, { target, props: { open: true, documentId: "document-1", completed }, events: { saved } });
  await vi.waitFor(() => expect(target.querySelector("textarea")?.value).toBe("Saved caption"));
  return { target, component, saved };
}

function coordinate(target: HTMLElement, label: string) {
  return [...target.querySelectorAll("label")].find(node => node.textContent?.trim() === label)!.querySelector<HTMLInputElement>("input")!;
}
async function setCoordinate(target: HTMLElement, label: string, value: number) {
  const input = coordinate(target, label);
  input.value = String(value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
  await tick();
}
function saveButton(target: HTMLElement) {
  return [...target.querySelectorAll("button")].find(node => node.textContent?.trim().startsWith("Save"))!;
}

it("edits accepted crops with the saved revision while keeping caption controls read-only", async () => {
  const { target, component, saved } = await openEditor();
  expect(target.textContent).toContain("Text and captions stay as saved");
  expect(target.querySelector("textarea")?.readOnly).toBe(true);
  expect(target.textContent).not.toContain("Add Box");
  expect(target.textContent).not.toContain("Remove Image");
  await setCoordinate(target, "Top", 50);
  saveButton(target).click();
  await vi.waitFor(() => expect(saved).toHaveBeenCalledOnce());
  expect(saveCompletedImageCrops).toHaveBeenCalledWith("document-1", [expect.objectContaining({ images: [expect.objectContaining({ bbox: [50, 200, 700, 800], caption: "Saved caption" })] })], "revision-1");
  expect(saveImageReviewPages).not.toHaveBeenCalled();
  await unmount(component);
});

it("reloads after a revision conflict instead of closing the editor", async () => {
  vi.mocked(saveCompletedImageCrops).mockRejectedValueOnce(new Error("Document changed. Reload it and try again."));
  const { target, component, saved } = await openEditor();
  saveButton(target).click();
  await vi.waitFor(() => expect(target.textContent).toContain("Document changed"));
  expect(saved).not.toHaveBeenCalled();
  vi.mocked(loadCompletedImageCrops).mockResolvedValueOnce({ pages: [structuredClone(page)], expected_updated_at: "revision-2", page_count: 5 });
  [...target.querySelectorAll("button")].find(node => node.textContent?.trim() === "Reload image crops")!.click();
  await vi.waitFor(() => expect(loadCompletedImageCrops).toHaveBeenCalledTimes(2));
  await tick();
  saveButton(target).click();
  await vi.waitFor(() => expect(saved).toHaveBeenCalledOnce());
  expect(vi.mocked(saveCompletedImageCrops).mock.lastCall?.[2]).toBe("revision-2");
  await unmount(component);
});

it("reanchors a legacy image to a chosen physical PDF page and can return to its saved image", async () => {
  vi.mocked(loadCompletedImageCrops).mockResolvedValueOnce({ pages: [{ ...structuredClone(page), source_kind: "saved_image", source_page_index: undefined, label: "Image 1", image_url: "/uploads/old-crop.png" }], expected_updated_at: "revision-1", page_count: 5 });
  const { target, component } = await openEditor();
  expect(target.textContent).toContain("original crop coordinates are unavailable");
  const input = target.querySelector<HTMLInputElement>('input[max="5"]')!;
  input.value = "6";
  input.dispatchEvent(new Event("change", { bubbles: true }));
  await tick();
  expect(saveButton(target).disabled).toBe(true);
  input.value = "4";
  input.dispatchEvent(new Event("change", { bubbles: true }));
  await tick();
  expect(saveButton(target).disabled).toBe(false);
  expect(target.querySelector("img")?.getAttribute("src")).toContain("pdf-pages/3/thumbnail");
  expect(coordinate(target, "Top").value).toBe("0");
  saveButton(target).click();
  await vi.waitFor(() => expect(saveCompletedImageCrops).toHaveBeenCalledOnce());
  expect(vi.mocked(saveCompletedImageCrops).mock.lastCall?.[1][0].source_page_index).toBe(3);
  input.value = "";
  input.dispatchEvent(new Event("change", { bubbles: true }));
  await tick();
  expect(target.querySelector("img")?.getAttribute("src")).toBe("/uploads/old-crop.png");
  expect(coordinate(target, "Top").value).toBe("100");
  await unmount(component);
});

it("blocks inverted boxes before saving and preserves the pending-review workflow", async () => {
  const { target, component } = await openEditor(false);
  expect(target.textContent).toContain("Add Box");
  expect(target.querySelector("textarea")?.readOnly).toBe(false);
  await setCoordinate(target, "Top", 750);
  expect(saveButton(target).disabled).toBe(true);
  await setCoordinate(target, "Top", 50);
  saveButton(target).click();
  await vi.waitFor(() => expect(saveImageReviewPages).toHaveBeenCalledOnce());
  expect(saveCompletedImageCrops).not.toHaveBeenCalled();
  await unmount(component);
});
