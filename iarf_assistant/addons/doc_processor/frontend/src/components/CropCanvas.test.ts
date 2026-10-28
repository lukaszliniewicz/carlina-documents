import { describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import CropCanvas from "./CropCanvas.svelte";

describe("CropCanvas empty preview", () => {
  it("shows the resolved empty-stack reason instead of a stale loading label", async () => {
    const getContext = vi
      .spyOn(HTMLCanvasElement.prototype, "getContext")
      .mockReturnValue(null);
    const target = document.createElement("div");
    document.body.append(target);
    const component = mount(CropCanvas, {
      target,
      props: {
        title: "Left Pages",
        imageSrc: "",
        loading: false,
        emptyMessage: "No pages are assigned to this stack."
      }
    });

    try {
      await tick();
      expect(target.textContent).toContain("No pages are assigned to this stack.");
      expect(target.textContent).not.toContain("Generating preview...");
    } finally {
      unmount(component);
      target.remove();
      getContext.mockRestore();
    }
  });
});
