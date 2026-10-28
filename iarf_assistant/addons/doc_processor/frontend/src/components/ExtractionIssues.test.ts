import { afterEach, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import ExtractionIssues from "./ExtractionIssues.svelte";
import { listIssues, updateIssue, reportIssue, type ExtractionIssue } from "../lib/issues";

vi.mock("../lib/issues", () => ({ listIssues: vi.fn(), updateIssue: vi.fn(), reportIssue: vi.fn() }));
const issue: ExtractionIssue = {
  id: "issue-1", document_id: "doc-1", page_number: 7, source_revision: "old", current_revision: "current",
  context_stale: true, status: "open", version: 2, origin: "extraction",
  report: { key: "name-1", kind: "name_check", excerpt: "Łucja — Ζωή", reason: "Check the name against the scan.", suggested_readings: ["Łucja"], references: [{ url: "https://example.org/source", title: "Catalogue", note: "Same edition." }] },
  history: [], created_at: "2026-09-27", updated_at: "2026-09-27",
};
let component: ReturnType<typeof mount> | undefined;
afterEach(async () => { if (component) await unmount(component); component = undefined; document.body.replaceChildren(); vi.resetAllMocks(); });
async function setup() {
  vi.mocked(listIssues).mockResolvedValue({ document_id: "doc-1", current_revision: "current", items: [issue], total: 1, counts: { open: 1, confirmed: 0, resolved: 0, dismissed: 0 } });
  const target = document.createElement("div"); document.body.append(target);
  component = mount(ExtractionIssues, { target, props: { documentId: "doc-1", pdfUrl: "/source.pdf" } });
  await vi.waitFor(() => expect(target.textContent).toContain("Łucja"));
  return target;
}
function clickText(target: Element, text: string) {
  const button = Array.from(target.querySelectorAll("button")).find(item => item.textContent?.trim() === text);
  expect(button).toBeTruthy(); button!.click();
}
it("shows source evidence and sends fenced ledger-only review with a stable key after uncertain delivery", async () => {
  const target = await setup();
  (target.querySelector(".issue-row") as HTMLButtonElement).click(); await tick();
  expect(target.textContent).toContain("document changed after this report");
  expect(target.querySelector(".source-link")?.getAttribute("href")).toBe("/source.pdf#page=7");
  expect(target.querySelector('a[href="https://example.org/source"]')?.getAttribute("rel")).toContain("noopener");
  const note = target.querySelector("textarea")!;
  note.value = "Verified against page 7; problem confirmed."; note.dispatchEvent(new Event("input", { bubbles: true })); await tick();
  vi.mocked(updateIssue).mockRejectedValueOnce(new Error("Delivery uncertain")).mockResolvedValueOnce({ ...issue, status: "confirmed", version: 3 });
  clickText(target, "Save review");
  await vi.waitFor(() => expect(target.textContent).toContain("Delivery uncertain"));
  clickText(target, "Save review");
  await vi.waitFor(() => expect(updateIssue).toHaveBeenCalledTimes(2));
  const [first, second] = vi.mocked(updateIssue).mock.calls;
  expect(first).toEqual(second);
  expect(first.slice(0, 4)).toEqual(["doc-1", issue, "confirmed", note.value]);
  expect(reportIssue).not.toHaveBeenCalled();
});
it("records a manual Unicode issue without changing extraction content", async () => {
  const target = await setup(); clickText(target, "Add issue"); await tick();
  const fields = target.querySelectorAll("textarea");
  fields[0].value = "שָׁלוֹם — e\u0301"; fields[0].dispatchEvent(new Event("input", { bubbles: true }));
  fields[1].value = "Check combining marks."; fields[1].dispatchEvent(new Event("input", { bubbles: true })); await tick();
  vi.mocked(reportIssue).mockResolvedValue(issue);
  clickText(target, "Save issue");
  await vi.waitFor(() => expect(reportIssue).toHaveBeenCalledOnce());
  expect(vi.mocked(reportIssue).mock.calls[0].slice(0, 3)).toEqual(["doc-1", "current", null]);
  expect(vi.mocked(reportIssue).mock.calls[0][3].excerpt).toBe("שָׁלוֹם — e\u0301");
  expect(updateIssue).not.toHaveBeenCalled();
});
