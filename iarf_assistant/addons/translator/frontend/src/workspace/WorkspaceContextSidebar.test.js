import { fireEvent, render } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";
import WorkspaceContextSidebar from "./WorkspaceContextSidebar.svelte";

describe("WorkspaceContextSidebar", () => {
  it("keeps guest context read-only and hides mutation actions", async () => {
    const onMemoryChange = vi.fn();
    const onEditGlossary = vi.fn();
    const onDeleteGlossary = vi.fn();
    const view = render(WorkspaceContextSidebar, {
      props: {
        guestMode: true,
        currentTab: "memory",
        workingMemory: "Read-only memory",
        onMemoryChange,
        humanGlossary: [{ id: 1, source_term: "source", target_term: "target" }],
        discoveredGlossary: [{ id: 2, source_term: "found", target_term: "term" }],
        onEditGlossary,
        onDeleteGlossary
      }
    });

    const memory = view.getByRole("textbox");
    expect(memory.readOnly).toBe(true);
    expect(view.queryByRole("button", { name: "Save Memory" })).toBeNull();
    expect(view.queryByRole("button", { name: "Knowledge" })).toBeNull();
    expect(view.queryByRole("button", { name: "Edit term" })).toBeNull();
    expect(view.queryByRole("button", { name: "Delete term" })).toBeNull();
    await fireEvent.input(memory, { target: { value: "attempt" } });
    expect(onMemoryChange).not.toHaveBeenCalled();
    expect(onEditGlossary).not.toHaveBeenCalled();
    expect(onDeleteGlossary).not.toHaveBeenCalled();
  });
});
