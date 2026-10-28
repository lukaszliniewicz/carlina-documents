import { afterEach, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import DocumentCard from "./DocumentCard.svelte";

afterEach(() => document.body.replaceChildren());

it.each(["completed", "completed_with_errors"])("reopens image crops on a %s PDF, including automatically accepted images", async (status) => {
  const target = document.createElement("div");
  document.body.append(target);
  const reviewImages = vi.fn();
  const component = mount(DocumentCard, {
    target,
    props: { document: { id: "finished", filename: "Book.pdf", status, source_available: true, created_at: "2026-10-10", settings: { pdf_options: { correct_images: false } } } },
    events: { reviewImages }
  });
  await tick();
  target.querySelector<HTMLButtonElement>('button[title="More document actions"]')!.click();
  await tick();
  const action = [...target.querySelectorAll("button")].find(node => node.textContent?.includes("Adjust image crops"));
  expect(action).toBeTruthy();
  action!.click();
  expect(reviewImages.mock.calls[0][0].detail).toBe("finished");
  await unmount(component);
});

it("explains a replay blocker without offering a resume that will be rejected", async () => {
  const target = document.createElement("div");
  document.body.append(target);
  const component = mount(DocumentCard, {
    target,
    props: { document: { id: "blocked", filename: "Book.pdf", status: "suspended", source_available: true, created_at: "2026-09-30",
      settings: { mcp_recovery: { reason: { error_code: "recovery_replay_conflict" } } } } }
  });
  await tick();
  expect(target.textContent).toContain("recovery needs inspection");
  expect([...target.querySelectorAll("button")].some(node => node.textContent?.includes("Resume"))).toBe(false);
  await unmount(component);
});

it("offers resume for an interrupted execution even before a page checkpoint exists", async () => {
  const target = document.createElement("div");
  document.body.append(target);
  const resume = vi.fn();
  const component = mount(DocumentCard, {
    target,
    props: { document: { id: "interrupted", filename: "Book.pdf", status: "suspended", source_available: true, created_at: "2026-09-30", settings: {} } },
    events: { resume }
  });
  await tick();
  expect(target.textContent).toContain("accepted responses are preserved");
  const button = [...target.querySelectorAll("button")].find(node => node.textContent?.includes("Resume"));
  expect(button).toBeTruthy();
  button!.click();
  expect(resume).toHaveBeenCalledOnce();
  expect(resume.mock.calls[0][0].detail).toBe("interrupted");
  await unmount(component);
});
