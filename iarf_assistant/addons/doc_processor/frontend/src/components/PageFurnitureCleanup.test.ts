import { afterEach, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import PageFurnitureCleanup from "./PageFurnitureCleanup.svelte";
import { cleanupDocument, undoDocumentCleanup } from "../lib/api";

vi.mock("../lib/api", () => ({ cleanupDocument: vi.fn(), undoDocumentCleanup: vi.fn() }));
afterEach(() => { document.body.replaceChildren(); vi.clearAllMocks(); });
function button(target: HTMLElement, label: string) {
  const found = [...target.querySelectorAll("button")].find((item) => item.textContent?.includes(label));
  if (!found) throw new Error(`Missing button ${label}`);
  return found;
}
async function setup() {
  const target = document.createElement("div"); document.body.append(target);
  const component = mount(PageFurnitureCleanup, { target, props: { documentId: "doc" } });
  await tick(); button(target, "Remove headers").click(); await tick();
  return { target, component };
}
it("previews before write, confirms the frozen selection and supports undo", async () => {
  vi.mocked(cleanupDocument).mockResolvedValueOnce({ revision: "old", cleanup: {
    removed_count: 2, skipped_count: 1, examples: [{ role: "header", page_numbers: [2], text: "Running head" }], examples_truncated: false
  } }).mockResolvedValueOnce({ revision: "new", change_id: "change", undo_available: true });
  vi.mocked(undoDocumentCleanup).mockResolvedValue({ revision: "old" });
  const { target, component } = await setup();
  const input = target.querySelector('input[type="text"]') as HTMLInputElement;
  input.value = "2-3, 7"; input.dispatchEvent(new Event("input", { bubbles: true })); await tick();
  expect(cleanupDocument).not.toHaveBeenCalled();
  button(target, "Preview removal").click();
  await vi.waitFor(() => expect(target.textContent).toContain("2 marked elements"));
  expect(cleanupDocument).toHaveBeenLastCalledWith("doc", { roles: ["header", "footer"], page_numbers: [2, 3, 7], dry_run: true });
  expect(target.textContent).toContain("1 protected elements will be kept");
  button(target, "Confirm removal").click();
  await vi.waitFor(() => expect(target.textContent).toContain("Cleanup saved"));
  expect(cleanupDocument).toHaveBeenLastCalledWith("doc", expect.objectContaining({ expected_revision: "old", page_numbers: [2, 3, 7], dry_run: false, idempotency_key: expect.any(String) }));
  button(target, "Undo this cleanup").click();
  await vi.waitFor(() => expect(undoDocumentCleanup).toHaveBeenCalledWith("doc", "change", "new", expect.any(String)));
  await unmount(component);
});
it("changing the scope invalidates confirmation and invalid ranges never submit", async () => {
  vi.mocked(cleanupDocument).mockResolvedValue({ revision: "old", cleanup: { removed_count: 1, skipped_count: 0, examples: [], examples_truncated: false } });
  const { target, component } = await setup();
  button(target, "Preview removal").click();
  await vi.waitFor(() => expect(target.textContent).toContain("Confirm removal"));
  const input = target.querySelector('input[type="text"]') as HTMLInputElement;
  input.value = "4-1"; input.dispatchEvent(new Event("input", { bubbles: true })); await tick();
  expect(target.textContent).not.toContain("Confirm removal");
  button(target, "Preview removal").click();
  await vi.waitFor(() => expect(target.textContent).toContain("Invalid page range"));
  expect(cleanupDocument).toHaveBeenCalledTimes(1);
  await unmount(component);
});
it("uses an icon-only compact trigger without changing the cleanup panel", async () => {
  const target = document.createElement("div"); document.body.append(target);
  const component = mount(PageFurnitureCleanup, { target, props: { documentId: "doc", compact: true } });
  await tick();
  const trigger = target.querySelector('button[aria-label="Remove page headers and footers"]') as HTMLButtonElement;
  expect(trigger).toBeTruthy();
  expect(trigger.querySelector("svg")).not.toBeNull();
  expect(trigger.textContent?.trim()).toBe("");
  trigger.click(); await tick();
  expect(target.querySelector(".cleanup-panel")).not.toBeNull();
  await unmount(component);
});
it("renders server examples as text rather than active HTML", async () => {
  vi.mocked(cleanupDocument).mockResolvedValue({ revision: "old", cleanup: { removed_count: 1, skipped_count: 0, examples: [{ role: "footer", page_numbers: [1], text: '<img src=x onerror="alert(1)">' }], examples_truncated: false } });
  const { target, component } = await setup(); button(target, "Preview removal").click();
  await vi.waitFor(() => expect(target.textContent).toContain("onerror"));
  expect(target.querySelector("img")).toBeNull(); await unmount(component);
});
