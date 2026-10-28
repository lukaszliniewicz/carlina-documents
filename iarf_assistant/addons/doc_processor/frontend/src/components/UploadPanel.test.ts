import { afterEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import UploadPanel from "./UploadPanel.svelte";
import { defaultProcessingSettings } from "../lib/options";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("UploadPanel PDF options", () => {
  it("mounts PDF controls immediately and removes them when the file is cleared", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify({ slots: {} }), {
            status: 200,
            headers: { "Content-Type": "application/json" }
          })
        )
      )
    );

    const target = document.createElement("div");
    document.body.append(target);
    const component = mount(UploadPanel, {
      target,
      props: {
        settings: defaultProcessingSettings(),
        engines: [
          { id: "auto", name: "Auto" },
          { id: "llm_vision", name: "LLM Vision" }
        ],
        models: [
          {
            id: "model-1",
            name: "Vision model",
            provider: "Test provider",
            supports_vision: true,
            supports_reasoning: true,
            supported_reasoning_levels: []
          }
        ]
      }
    });

    try {
      await tick();
      expect(target.textContent).not.toContain("PDF Cleanup");
      expect(target.textContent).not.toContain("Models");

      const fileInput = target.querySelector('input[type="file"]');
      expect(fileInput).toBeInstanceOf(HTMLInputElement);
      Object.defineProperty(fileInput, "files", {
        configurable: true,
        value: [new File(["%PDF"], "sample.PDF", { type: "application/pdf" })]
      });
      fileInput?.dispatchEvent(new Event("change", { bubbles: true }));
      await tick();

      expect(target.textContent).toContain("Models");
      expect(target.textContent).toContain("PDF Cleanup");
      expect(target.textContent).toContain("Metadata");
      expect(target.textContent).toContain("metadata pre-pass (12 start + 4 end pages)");
      expect(target.textContent).toContain("Images and Cropping");
      expect(target.textContent).toContain("Concurrency");
      expect(target.textContent).not.toContain("Extraction reasoning");
      expect(target.textContent).not.toContain("Metadata reasoning");
      const evaluation = Array.from(target.querySelectorAll("label")).find((label) =>
        label.textContent?.includes("Optional two-pass evaluation")
      )?.querySelector("input") as HTMLInputElement | null;
      expect(evaluation?.checked).toBe(false);

      const boundary = Array.from(target.querySelectorAll("label")).find((label) =>
        label.textContent?.includes("Optional visual boundary correction")
      )?.querySelector("input") as HTMLInputElement;
      expect(boundary.checked).toBe(false);
      boundary.checked = true;
      boundary.dispatchEvent(new Event("change", { bubbles: true }));
      await tick();
      expect(boundary.checked).toBe(true);
      expect((target.querySelector("#docproc-processing-profile") as HTMLSelectElement).value).toBe("book_quality");
      boundary.checked = false;
      boundary.dispatchEvent(new Event("change", { bubbles: true }));
      await tick();
      expect(boundary.checked).toBe(false);

      const clearButton = target.querySelector('button[aria-label="Clear file"]');
      expect(clearButton).toBeInstanceOf(HTMLButtonElement);
      clearButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await tick();

      expect(target.textContent).not.toContain("Models");
      expect(target.textContent).not.toContain("PDF Cleanup");
      expect(target.textContent).not.toContain("Metadata");
      expect(target.textContent).not.toContain("Images and Cropping");
      expect(target.textContent).not.toContain("Concurrency");
    } finally {
      unmount(component);
      target.remove();
    }
  });

  it("offers only the reasoning levels advertised by the selected model", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify({ slots: {} }), {
            status: 200,
            headers: { "Content-Type": "application/json" }
          })
        )
      )
    );

    const target = document.createElement("div");
    document.body.append(target);
    const component = mount(UploadPanel, {
      target,
      props: {
        settings: {
          ...defaultProcessingSettings(),
          parsing_model: "model-1",
          metadata_model: "model-1"
        },
        engines: [{ id: "llm_vision", name: "LLM Vision" }],
        models: [
          {
            id: "model-1",
            name: "Vision model",
            provider: "Test provider",
            supports_vision: true,
            supports_reasoning: true,
            supported_reasoning_levels: ["off", "high"]
          }
        ]
      }
    });

    try {
      const fileInput = target.querySelector('input[type="file"]');
      Object.defineProperty(fileInput, "files", {
        configurable: true,
        value: [new File(["%PDF"], "sample.pdf", { type: "application/pdf" })]
      });
      fileInput?.dispatchEvent(new Event("change", { bubbles: true }));
      await tick();

      for (const labelText of ["Extraction reasoning", "Metadata reasoning"]) {
        const select = Array.from(target.querySelectorAll("label")).find((label) =>
          label.textContent?.includes(labelText)
        )?.querySelector("select");
        expect(Array.from(select?.options ?? []).map((option) => option.value)).toEqual([
          "default",
          "off",
          "high"
        ]);
      }
    } finally {
      unmount(component);
      target.remove();
    }
  });
});
