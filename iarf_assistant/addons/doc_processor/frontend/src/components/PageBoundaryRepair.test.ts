import { afterEach, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import PageBoundaryRepair from "./PageBoundaryRepair.svelte";
import { repairDocumentBoundaries, undoDocumentCleanup, type BoundaryRepairResult } from "../lib/api";

vi.mock("../lib/api", () => ({ repairDocumentBoundaries: vi.fn(), undoDocumentCleanup: vi.fn() }));
afterEach(() => { document.body.replaceChildren(); vi.resetAllMocks(); });
function button(target: HTMLElement, text: string) {
  const found = [...target.querySelectorAll("button")].find((node) => node.textContent?.includes(text));
  if (!found) throw new Error(`Missing button: ${text}`);
  return found;
}
const preview: BoundaryRepairResult = {
  revision: "before", boundary_repair: {
    candidate_count: 1, selected_count: 0, offset: 0, limit: 50, has_more: false,
    skipped_count: 0, skipped_reasons: {}, policy: "Review before saving.",
    candidates: [{ id: "candidate", page_number: 2, suggested_action: "dehyphenate", reason: "possible_page_split_word", left: "ge-", right: "macht", preview: "gemacht" }]
  }
};
async function setup() {
  const target = document.createElement("div"); document.body.append(target);
  const component = mount(PageBoundaryRepair, { target, props: { documentId: "doc" } });
  await tick(); button(target, "Repair page continuations").click(); await tick();
  return { target, component };
}
async function choose(target: HTMLElement, action: string) {
  const select = target.querySelector("select") as HTMLSelectElement;
  select.value = action; select.dispatchEvent(new Event("change", { bubbles: true })); await tick();
}
it("requires an explicit selection and saves one revision-fenced undoable batch", async () => {
  vi.mocked(repairDocumentBoundaries).mockResolvedValueOnce(preview).mockResolvedValueOnce({ revision: "after", change_id: "change" });
  vi.mocked(undoDocumentCleanup).mockResolvedValue({ revision: "before" });
  const { target, component } = await setup();
  expect(repairDocumentBoundaries).not.toHaveBeenCalled();
  button(target, "Scan page").click();
  await vi.waitFor(() => expect(target.textContent).toContain("1 possible continuations"));
  expect(target.textContent).not.toContain("Confirm 1");
  expect((target.querySelector("select") as HTMLSelectElement).value).toBe("");
  await choose(target, "keep_hyphen");
  expect(target.querySelector(".after")?.textContent).toBe("ge-macht");
  button(target, "Confirm 1").click();
  await vi.waitFor(() => expect(target.textContent).toContain("Page repairs saved"));
  expect(repairDocumentBoundaries).toHaveBeenLastCalledWith("doc", {
    decisions: [{ id: "candidate", action: "keep_hyphen" }], expected_revision: "before", idempotency_key: expect.any(String), dry_run: false
  });
  button(target, "Undo this repair").click();
  await vi.waitFor(() => expect(undoDocumentCleanup).toHaveBeenCalledWith("doc", "change", "after", expect.any(String)));
  await unmount(component);
});
it("preserves the same save key on a lost response and escapes source markup", async () => {
  const hostile = structuredClone(preview);
  hostile.boundary_repair!.candidates[0].left = '<img src=x onerror="attack()">ge-';
  vi.mocked(repairDocumentBoundaries).mockResolvedValueOnce(hostile).mockRejectedValueOnce(new Error("Lost response")).mockResolvedValueOnce({ revision: "after", change_id: "change" });
  const { target, component } = await setup(); button(target, "Scan page").click();
  await vi.waitFor(() => expect(target.textContent).toContain("onerror"));
  expect(target.querySelector("img")).toBeNull();
  await choose(target, "dehyphenate"); button(target, "Confirm 1").click();
  await vi.waitFor(() => expect(target.textContent).toContain("Lost response"));
  const firstSave = vi.mocked(repairDocumentBoundaries).mock.calls[1][1];
  button(target, "Confirm 1").click();
  await vi.waitFor(() => expect(target.textContent).toContain("Page repairs saved"));
  expect(vi.mocked(repairDocumentBoundaries).mock.calls[2][1]).toEqual(firstSave);
  await unmount(component);
});
it("uses an icon-only compact trigger without changing the repair panel", async () => {
  const target = document.createElement("div"); document.body.append(target);
  const component = mount(PageBoundaryRepair, { target, props: { documentId: "doc", compact: true } });
  await tick();
  const trigger = target.querySelector('button[aria-label="Repair page continuations"]') as HTMLButtonElement;
  expect(trigger).toBeTruthy();
  expect(trigger.querySelector("svg")).not.toBeNull();
  expect(trigger.textContent?.trim()).toBe("");
  trigger.click(); await tick();
  expect(target.querySelector(".repair-panel")).not.toBeNull();
  await unmount(component);
});
it("does not discard selected repairs by paging past them", async () => {
  const paginated = structuredClone(preview); paginated.boundary_repair!.has_more = true;
  vi.mocked(repairDocumentBoundaries).mockResolvedValue(paginated);
  const { target, component } = await setup(); button(target, "Scan page").click();
  await vi.waitFor(() => expect(target.textContent).toContain("Next candidates"));
  await choose(target, "join_with_space");
  expect(button(target, "Next candidates").disabled).toBe(true);
  await choose(target, "");
  expect(button(target, "Next candidates").disabled).toBe(false);
  button(target, "Next candidates").click();
  await vi.waitFor(() => expect(repairDocumentBoundaries).toHaveBeenLastCalledWith("doc", { offset: 50, limit: 50, expected_revision: "before", dry_run: true }));
  await unmount(component);
});
