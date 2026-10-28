import { fireEvent, render } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";
import WorkspaceExportDialog from "./WorkspaceExportDialog.svelte";

describe("WorkspaceExportDialog", () => {
  it("keeps the busy dialog open and reports format changes", async () => {
    const onClose = vi.fn();
    const onFormatChange = vi.fn();
    const view = render(WorkspaceExportDialog, {
      props: {
        open: true,
        exportBusy: true,
        exportOptions: [{ value: "html", label: "Raw HTML", extension: ".html" }, { value: "docx", label: "Word", extension: ".docx" }],
        exportFormat: "html",
        onClose,
        onFormatChange
      }
    });
    expect(view.getByRole("button", { name: "Preparing..." }).disabled).toBe(true);
    await fireEvent.click(view.getByRole("button", { name: "Close dialog" }));
    expect(onClose).not.toHaveBeenCalled();
    await fireEvent.change(view.getByRole("radio", { name: /Word/ }), { target: { value: "docx" } });
    expect(onFormatChange).toHaveBeenCalledWith("docx");
  });
});
