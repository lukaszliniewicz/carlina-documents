import { describe, expect, it } from "vitest";
import { ACTIVE_PROJECT_STATUSES, allowedExportFormats, buildSegmentBlocks, containsHtmlMarkup, highlightGlossaryTerms, MAX_CHUNK_SIZE_CHARS, modelSetupMessage, normaliseModels, PROJECT_STATUS_CONTRACT, selectReadyModel, validateProjectStatusPayload } from "./utils.js";

describe("translator frontend readiness and exports", () => {
  it("keeps the chunk-size control aligned with the backend maximum", () => {
    expect(MAX_CHUNK_SIZE_CHARS).toBe(20000);
  });

  it("keeps every long-running project operation visible to status polling", () => {
    expect(ACTIVE_PROJECT_STATUSES).toEqual(new Set([
      "extracting",
      "processing_pdf",
      "analyzing_structure",
      "analyzing_terminology",
      "translating",
      "exporting"
    ]));
  });

  it("accepts only the exact lightweight project status projection", () => {
    const status = {
      contract: PROJECT_STATUS_CONTRACT,
      project_id: "project/1",
      status: "translating",
      latest_operation: { id: "op-1", state: "running", progress: 35 }
    };
    expect(validateProjectStatusPayload(status, "project/1")).toEqual(status);
    expect(() => validateProjectStatusPayload({ ...status, project_id: "other" }, "project/1")).toThrow(/project mismatch/);
    expect(() => validateProjectStatusPayload({ ...status, status: "" }, "project/1")).toThrow(/status/);
    expect(() => validateProjectStatusPayload({ ...status, latest_operation: null }, "project/1")).toThrow(/latest operation/);
    expect(() => validateProjectStatusPayload({ ...status, extra: true }, "project/1")).toThrow(/incompatible/);
  });

  it("does not invent a model when the API has no ready model", () => {
    expect(normaliseModels([])).toEqual([]);
    expect(modelSetupMessage([])).toMatch(/No usable model.*direct API translation/);
    expect(modelSetupMessage([])).toContain("passive jobs through MCP");
  });

  it("filters models explicitly reported as unavailable", () => {
    expect(normaliseModels([{ id: "not-ready", ready: false }, { id: "ready", ready: true }]).map((m) => m.id)).toEqual(["ready"]);
  });

  it("keeps the provider label used to distinguish equal model names", () => {
    expect(normaliseModels([{ id: "model-1", name: "Flash", provider_name: "OpenCode Go" }])[0].provider).toBe("OpenCode Go");
  });

  it("restricts guest exports to HTML", () => {
    expect(allowedExportFormats({ guestMode: true }).map((option) => option.value)).toEqual(["html"]);
  });

  it("falls back to the first ready model when a saved model is unavailable", () => {
    expect(selectReadyModel([{ id: "ready-1" }, { id: "ready-2" }], "retired-model")).toBe("ready-1");
    expect(selectReadyModel([{ id: "ready-1" }], "ready-1")).toBe("ready-1");
  });

  it("renders canonical merged parent text without appending hidden children", () => {
    const blocks = buildSegmentBlocks([
      { id: 1, block_index: 0, html_tag: "p", original_text: "First and second", merged_into_id: null },
      { id: 2, block_index: 0, html_tag: "p", original_text: "second", merged_into_id: 1 }
    ], []);
    expect(blocks).toHaveLength(1);
    expect(blocks[0].segments[0].full_source_text).toBe("First and second");
  });

  it("detects markup-bearing segments that cannot be split safely", () => {
    expect(containsHtmlMarkup("<i>First. Second.</i>")).toBe(true);
    expect(containsHtmlMarkup("First < Second.")).toBe(false);
  });

  it("highlights multiple terms without matching generated mark attributes", () => {
    const rendered = highlightGlossaryTerms("Source class data.", [
      { id: 1, source_term: "Source", target_term: "Źródło", context_notes: "" },
      { id: 2, source_term: "class", target_term: "klasa", context_notes: "" },
      { id: 3, source_term: "data", target_term: "dane", context_notes: "" }
    ]);

    expect(rendered.match(/<mark /g)).toHaveLength(3);
    expect(rendered).toContain('data-glossary-source="Source"');
    expect(rendered).toContain('data-glossary-source="class"');
    expect(rendered).not.toContain('data-glossary-<mark');
    expect(rendered).not.toContain('class="glossary-highlight" tabindex="0" role="button" aria-label="Glossary term <mark');
  });
});
