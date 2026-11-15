import { cleanup, fireEvent, render } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import ReviewFlagsDialog from "./ReviewFlagsDialog.svelte";

const openFlag = {
  id: "flag-1",
  block_id: "b-3",
  order: 3,
  kind: "source_defect",
  origin: "agent",
  state: "open",
  sequence: 1,
  body: "The source repeats a line, probably a misprint.",
  excerpt: "the line, the line",
  history: []
};

function payload(items, summary = { open: 1, resolved: 0, dismissed: 0 }) {
  return { items, summary, pagination: { offset: 0, limit: 50, total: items.length, has_more: false } };
}

afterEach(cleanup);

describe("ReviewFlagsDialog", () => {
  it("lists flags with their kind and origin and resolves one with a note", async () => {
    const ontransition = vi.fn().mockResolvedValue(true);
    const onnavigate = vi.fn();
    const view = render(ReviewFlagsDialog, { props: { open: true, canEdit: true, payload: payload([openFlag]), ontransition, onnavigate } });

    expect(view.getByText("Source problem · Raised by an agent")).toBeTruthy();
    expect(view.getByText(openFlag.body)).toBeTruthy();
    await fireEvent.click(view.getByRole("button", { name: "Block 3" }));
    expect(onnavigate).toHaveBeenCalledWith(openFlag);

    await fireEvent.click(view.getByRole("button", { name: "Resolve…" }));
    const confirm = view.getByRole("button", { name: "Resolve flag" });
    expect(confirm.disabled).toBe(true);
    await fireEvent.input(view.getByLabelText("How was it settled?"), { target: { value: "Kept one line; noted for the reader." } });
    await fireEvent.click(view.getByRole("button", { name: "Resolve flag" }));
    expect(ontransition).toHaveBeenCalledWith(openFlag, "resolve", "Kept one line; noted for the reader.");
  });

  it("adds a flag only when scoped to a block and reopens closed flags", async () => {
    const oncreate = vi.fn().mockResolvedValue(true);
    const ontransition = vi.fn().mockResolvedValue(true);
    const onclearblock = vi.fn();
    const resolved = { ...openFlag, state: "resolved", sequence: 2, history: [{ sequence: 2, action: "resolve", note: "Checked." }] };
    const view = render(ReviewFlagsDialog, {
      props: { open: true, canEdit: true, block: { id: "b-3", order: 3 }, stateFilter: "all", payload: payload([resolved], { open: 0, resolved: 1, dismissed: 0 }), oncreate, ontransition, onclearblock }
    });

    expect(view.getByText(/Resolved.*Checked\./)).toBeTruthy();
    await fireEvent.change(view.getByLabelText("Kind"), { target: { value: "unverified" } });
    await fireEvent.input(view.getByLabelText("What should the next reader know?"), { target: { value: "Date not checked." } });
    await fireEvent.click(view.getByRole("button", { name: "Add flag" }));
    expect(oncreate).toHaveBeenCalledWith({ kind: "unverified", body: "Date not checked." });

    await fireEvent.click(view.getByRole("button", { name: "Reopen" }));
    await fireEvent.click(view.getByRole("button", { name: "Reopen flag" }));
    expect(ontransition).toHaveBeenCalledWith(resolved, "reopen", "");

    await fireEvent.click(view.getByRole("button", { name: "Show every block" }));
    expect(onclearblock).toHaveBeenCalledOnce();
  });

  it("is read-only for viewers", () => {
    const view = render(ReviewFlagsDialog, { props: { open: true, block: { id: "b-3", order: 3 }, payload: payload([openFlag]) } });
    expect(view.queryByRole("button", { name: "Resolve…" })).toBeNull();
    expect(view.queryByRole("button", { name: "Add flag" })).toBeNull();
  });
});
