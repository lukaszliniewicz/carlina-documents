import { fireEvent, render, screen } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";

import type { FrontendKnowledgeXray } from "@carlina/sdk";

import KnowledgeXrayDialog from "../../src/routes/knowledge/KnowledgeXrayDialog.svelte";

const xray: FrontendKnowledgeXray = {
  id: "item-1",
  source_type: "markdown",
  source_id: "source-1",
  metadata: { title: "X-ray fixture" },
  raw_content: "The extracted source remains available for comparison.",
  chunks: [
    {
      id: "chunk-1",
      text: "This chunk body is only revealed when its summary is expanded.",
      meta: { section: "Introduction", page: 1 },
    },
  ],
  chunk_total: 31,
  chunk_pages: 2,
  chunk_page: 1,
  has_next: true,
  has_prev: false,
};

describe("KnowledgeXrayDialog", () => {
  it("keeps raw provenance visible and expands compact chunk details on demand", async () => {
    const onpage = vi.fn();

    render(KnowledgeXrayDialog, {
      props: {
        open: true,
        title: "X-ray fixture",
        xray,
        loading: false,
        error: "",
        onclose: vi.fn(),
        onpage,
      },
    });

    expect(
      screen.getByText("The extracted source remains available for comparison."),
    ).toBeTruthy();
    expect(screen.getByText("#chunk-1")).toBeTruthy();

    const summary = screen.getByText("Chunk 01").closest("summary");
    const chunk = summary?.closest("details");
    expect(chunk?.open).toBe(false);

    await fireEvent.click(summary as HTMLElement);
    expect(chunk?.open).toBe(true);
    expect(
      screen.getByText("This chunk body is only revealed when its summary is expanded."),
    ).toBeTruthy();

    await fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(onpage).toHaveBeenCalledWith(2);
  });
});
