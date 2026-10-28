import { fireEvent, render } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";
import GlossaryTermDialog from "./GlossaryTermDialog.svelte";

describe("GlossaryTermDialog", () => {
  it("does not close while a save is in flight", async () => {
    const onClose = vi.fn();
    const view = render(GlossaryTermDialog, {
      props: { open: true, busy: true, term: { source_term: "source" }, onClose }
    });

    await fireEvent.click(view.getByRole("button", { name: "Close dialog" }));
    expect(onClose).not.toHaveBeenCalled();
    await view.rerender({ busy: false });
    await fireEvent.click(view.getByRole("button", { name: "Close dialog" }));
    expect(onClose).toHaveBeenCalledOnce();
  });
});
