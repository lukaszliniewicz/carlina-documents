import { describe, expect, it, vi } from "vitest";
import {
  ALIGNED_BLOCK_WORKSPACE_CONTRACT,
  BLOCK_GLOSSARY_CONTRACT,
  BLOCK_WORKSPACE_CONTRACT,
  DOCUMENT_BLOCK_ENGINE,
  createBlockWorkspaceClient,
  validateBlockGlossaryPayload,
  validateBlockWorkspacePayload
} from "./block-workspace-api.js";
import { PROJECT_STATUS_CONTRACT, validateProjectStatusPayload } from "../lib/utils.js";

function payload(overrides = {}) {
  return {
    contract: BLOCK_WORKSPACE_CONTRACT,
    access: { role: "owner", label: null },
    project: { engine_contract: DOCUMENT_BLOCK_ENGINE },
    source_plan: { id: "plan-1", version: 1 },
    latest_operation: {},
    blocks: [],
    pagination: { offset: 0, limit: 40, total: 0, has_more: false },
    target_summary: {
      total_blocks: 0,
      active_targets: 0,
      structurally_valid_active_targets: 0,
      document_complete: true
    },
    ...overrides
  };
}

const sourceEditor = {
  contract_version: "translation-block-editor-v1",
  content: "Source",
  fields: []
};

describe("QA diagnostics API", () => {
  it("sends bounded QA filters without inventing client-side diagnostics", async () => {
    const response = { contract: "translator-qa-diagnostics-v1", items: [] };
    const fetchJson = vi.fn().mockResolvedValue(response);
    const client = createBlockWorkspaceClient({ fetchJson }, "project/1");

    await expect(client.loadQaDiagnostics({
      categories: ["references", "spelling"],
      severities: ["integrity"],
      blockIds: ["block/2"],
      includeSpelling: false,
      cursor: "next cursor",
      limit: 25
    })).resolves.toEqual(response);

    const [url, options] = fetchJson.mock.calls[0];
    const parsed = new URL(url, "https://example.test");
    expect(parsed.pathname).toBe("/addons/translator/api/projects/project%2F1/qa-diagnostics");
    expect(parsed.searchParams.get("categories")).toBe("references,spelling");
    expect(parsed.searchParams.get("severities")).toBe("integrity");
    expect(parsed.searchParams.getAll("block_id")).toEqual(["block/2"]);
    expect(parsed.searchParams.get("include_spelling")).toBe("0");
    expect(parsed.searchParams.get("cursor")).toBe("next cursor");
    expect(parsed.searchParams.get("limit")).toBe("25");
    expect(options).toEqual({ signal: undefined });
  });
});

describe("block workspace API boundary", () => {
  it("sends source and edition CAS witnesses with reusable EPUB choices", async () => {
    const fetchJson = vi.fn().mockResolvedValue({});
    const client = createBlockWorkspaceClient({ fetchJson }, "project/1");
    const recipe = { epub_options: { profile: "publishing-v2" }, epub_cover: { mode: "pdf_page", page: 1, crop: [0, 0, .5, 1] }, epub_image_mode: "auto" };
    await client.getEpubEdition();
    await client.saveEpubEdition({ recipe, expectedVersion: 3, expectedSourceRevision: "a".repeat(64) });
    await client.clearEpubEdition({ expectedVersion: 4 });
    expect(fetchJson.mock.calls[0][0]).toBe("/addons/translator/api/projects/project%2F1/epub/edition");
    expect(JSON.parse(fetchJson.mock.calls[1][1].body)).toEqual({ recipe, expected_version: 3, expected_source_revision: "a".repeat(64) });
    expect(fetchJson.mock.calls[1][1].method).toBe("PUT");
    expect(JSON.parse(fetchJson.mock.calls[2][1].body)).toEqual({ expected_version: 4 });
    expect(fetchJson.mock.calls[2][1].method).toBe("DELETE");
  });

  it("accepts only a complete safe aligned-sentence workspace projection", () => {
    const alignedTarget = {
      active_version: 1,
      editor: { contract_version: "translation-block-editor-v1", content: "Pierwsze. Drugie.", fields: [] },
      validation: { valid: true, findings: [] },
      translator_notes: [],
      alignment: {
        origin: "model_reported",
        review_state: "needs_review",
        groups: [
          {
            order: 1,
            kind: "merge",
            source_sentences: [{ order: 1, text: "First. " }, { order: 2, text: "Second." }],
            target_sentences: [{ order: 1, text: "Pierwsze. " }]
          },
          {
            order: 2,
            kind: "one_to_one",
            source_sentences: [{ order: 3, text: "Third." }],
            target_sentences: [{ order: 2, text: "Drugie." }]
          }
        ]
      }
    };
    const alignedBlock = {
      id: "b1",
      order: 1,
      role: "paragraph",
      source: { editor: sourceEditor },
      target: alignedTarget,
      reader_notes: [],
      review: { decision: "pending", version: 0, rationale: null, actor: null, updated_at: null }
    };
    const alignedPayload = payload({
      contract: ALIGNED_BLOCK_WORKSPACE_CONTRACT,
      review_mode: "aligned_sentences",
      blocks: [alignedBlock],
      pagination: { offset: 0, limit: 40, total: 1, has_more: false },
      target_summary: { total_blocks: 1, active_targets: 1, structurally_valid_active_targets: 1, document_complete: true }
    });

    expect(validateBlockWorkspacePayload(alignedPayload).blocks[0].target.alignment.origin).toBe("model_reported");
    const carriedPayload = structuredClone(alignedPayload);
    carriedPayload.blocks[0].target.alignment.origin = "carried_forward";
    expect(validateBlockWorkspacePayload(carriedPayload).blocks[0].target.alignment.origin).toBe("carried_forward");
    expect(() => validateBlockWorkspacePayload({ ...alignedPayload, review_mode: "block" })).toThrow(/review mode/);
    expect(() => validateBlockWorkspacePayload({
      ...alignedPayload,
      blocks: [{ ...alignedBlock, target: { ...alignedTarget, alignment: { ...alignedTarget.alignment, groups: [
        { ...alignedTarget.alignment.groups[0], kind: "split" },
        alignedTarget.alignment.groups[1]
      ] } } }]
    })).toThrow(/group kind/);
    const leaked = {
      ...alignedTarget.alignment,
      confidence: 0.5
    };
    expect(() => validateBlockWorkspacePayload({
      ...alignedPayload,
      blocks: [{ ...alignedBlock, target: { ...alignedTarget, alignment: leaked } }]
    })).toThrow(/sentence alignment/);
  });

  it("loads the lightweight project status contract", async () => {
    const status = {
      contract: PROJECT_STATUS_CONTRACT,
      project_id: "project/1",
      status: "translating",
      latest_operation: { id: "op-1", state: "running", progress: 35 }
    };
    expect(validateProjectStatusPayload(status, "project/1").latest_operation.progress).toBe(35);

    const fetchJson = vi.fn().mockResolvedValue(status);
    const client = createBlockWorkspaceClient({ fetchJson }, "project/1");
    await expect(client.loadStatus()).resolves.toEqual(status);
    expect(fetchJson).toHaveBeenCalledWith(
      "/addons/translator/api/projects/project%2F1/status",
      expect.objectContaining({})
    );
  });

  it("validates the glossary projection and writes only the owner-facing fields", async () => {
    const glossary = {
      contract: BLOCK_GLOSSARY_CONTRACT,
      project_id: "project/1",
      source_plan_id: "plan-1",
      entries: [{
        concept_id: "concept-1",
        active_version: 2,
        source_term: "Widget",
        target_term: "Moduł",
        notes: "Domain term",
        scope: "document",
        usage: {
          sense: "",
          match_policy: "preferred",
          source_forms: ["Widget"],
          accepted_target_forms: ["Module"],
          observations: [],
          provenance: "human-owner"
        },
        updated_at: null
      }],
      proposals: [{
        proposal_id: "proposal-1",
        concept_id: "concept-2",
        source_term: "Signal",
        target_term: "Sygnał",
        notes: "Analysis suggestion",
        scope: "document",
        proposed_at: "2026-09-01T00:00:00+00:00"
      }],
      suggestions: []
    };
    expect(validateBlockGlossaryPayload(glossary).entries[0].notes).toBe("Domain term");
    expect(validateBlockGlossaryPayload(glossary).proposals[0].proposal_id).toBe("proposal-1");
    expect(() => validateBlockGlossaryPayload({ ...glossary, contract: "legacy" })).toThrow(/incompatible/);
    const fetchJson = vi.fn().mockResolvedValue(glossary);
    const client = createBlockWorkspaceClient({ fetchJson }, "project/1");

    await client.loadGlossary();
    await client.saveGlossaryEntry(glossary.entries[0]);
    await client.saveGlossaryEntry(glossary.proposals[0]);

    expect(fetchJson.mock.calls[0][0]).toBe("/addons/translator/api/projects/project%2F1/block-glossary");
    expect(fetchJson.mock.calls[1][0]).toBe("/addons/translator/api/projects/project%2F1/block-glossary/concept-1");
    expect(JSON.parse(fetchJson.mock.calls[1][1].body)).toEqual({
      source_term: "Widget",
      target_term: "Moduł",
      notes: "Domain term",
      accepted_target_forms: ["Module"],
      expected_active_version: 2,
      expected_proposal_id: null
    });
    expect(fetchJson.mock.calls[2][0]).toBe("/addons/translator/api/projects/project%2F1/block-glossary/concept-2");
    expect(JSON.parse(fetchJson.mock.calls[2][1].body)).toEqual({
      source_term: "Signal",
      target_term: "Sygnał",
      notes: "Analysis suggestion",
      accepted_target_forms: [],
      expected_active_version: 0,
      expected_proposal_id: "proposal-1"
    });
  });

  it("accepts only the explicit block workspace contract and orders blocks", () => {
    const result = validateBlockWorkspacePayload(payload({
      blocks: [
        { id: "b2", order: 2, role: "paragraph", source: { editor: sourceEditor }, target: null, reader_notes: [], review: { decision: "pending", version: 0, rationale: null, actor: null, updated_at: null } },
        { id: "b1", order: 1, role: "paragraph", source: { editor: sourceEditor }, target: null, reader_notes: [], review: { decision: "pending", version: 0, rationale: null, actor: null, updated_at: null } }
      ],
      pagination: { offset: 0, limit: 40, total: 2, has_more: false },
      target_summary: {
        total_blocks: 2,
        active_targets: 2,
        structurally_valid_active_targets: 2,
        document_complete: true
      }
    }));
    expect(result.blocks.map((block) => block.id)).toEqual(["b1", "b2"]);
    expect(result.target_summary.document_complete).toBe(true);
    expect(() => validateBlockWorkspacePayload(payload({ contract: "legacy" }))).toThrow(/incompatible/);
    expect(() => validateBlockWorkspacePayload(payload({ project: { engine_contract: "segment-v1" } }))).toThrow(/document-block/);
    expect(() => validateBlockWorkspacePayload(payload({ target_summary: { total_blocks: 1 } }))).toThrow(/target summary/);
    expect(() => validateBlockWorkspacePayload(payload({ target_summary: {
      total_blocks: "0",
      active_targets: 0,
      structurally_valid_active_targets: 0,
      document_complete: true
    } }))).toThrow(/target summary/);
  });

  it("validates whole-block and textual reader-note anchors by anchor kind", () => {
    const note = {
      id: "note-1",
      version: 1,
      state: "active",
      body: "A reader-facing explanation.",
      citation_ids: [],
      anchor: { kind: "whole_block", text: null, occurrence_index: null },
      author: { type: "human", label: "Owner" }
    };
    const block = {
      id: "b1",
      order: 1,
      role: "paragraph",
      source: { editor: sourceEditor },
      target: null,
      reader_notes: [note],
      review: { decision: "pending", version: 0, rationale: null, actor: null, updated_at: null }
    };
    const withNote = (readerNote) => payload({
      blocks: [{ ...block, reader_notes: [readerNote] }],
      pagination: { offset: 0, limit: 40, total: 1, has_more: false },
      target_summary: {
        total_blocks: 1,
        active_targets: 0,
        structurally_valid_active_targets: 0,
        document_complete: false
      }
    });

    expect(validateBlockWorkspacePayload(withNote(note)).blocks[0].reader_notes[0]).toEqual(note);
    const sourceNote = {
      ...note,
      anchor: { kind: "source", text: "Source phrase", occurrence_index: 0 }
    };
    expect(validateBlockWorkspacePayload(withNote(sourceNote)).blocks[0].reader_notes[0]).toEqual(sourceNote);
    const targetNote = {
      ...note,
      anchor: { kind: "target", text: "Target phrase", occurrence_index: 2 }
    };
    expect(validateBlockWorkspacePayload(withNote(targetNote)).blocks[0].reader_notes[0]).toEqual(targetNote);

    expect(() => validateBlockWorkspacePayload(withNote({
      ...note,
      anchor: { kind: "whole_block", text: null, occurrence_index: 0 }
    }))).toThrow(/whole-block note anchor/);
    expect(() => validateBlockWorkspacePayload(withNote({
      ...note,
      anchor: { kind: "whole_block", text: "Source phrase", occurrence_index: null }
    }))).toThrow(/whole-block note anchor/);
    expect(() => validateBlockWorkspacePayload(withNote({
      ...note,
      anchor: { kind: "source", text: "Source phrase", occurrence_index: null }
    }))).toThrow(/reader note anchor/);
    expect(() => validateBlockWorkspacePayload(withNote({
      ...note,
      anchor: { kind: "target", text: "Target phrase", occurrence_index: -1 }
    }))).toThrow(/reader note anchor/);
  });

  it("rejects active markup and source/target structure drift before rendering", () => {
    const formattedSource = {
      contract_version: "translation-block-editor-v1",
      content: "Source <em>text</em><lit r=\"1\">7319</lit>",
      fields: []
    };
    const valid = {
      id: "b1",
      order: 1,
      role: "paragraph",
      source: { editor: formattedSource },
      reader_notes: [],
      review: { decision: "pending", version: 0, rationale: null, actor: null, updated_at: null },
      target: {
        active_version: 1,
        editor: {
          contract_version: "translation-block-editor-v1",
          content: "Przekład <em>tekstu</em><lit r=\"1\">7319</lit>",
          fields: []
        },
        validation: { valid: true, findings: [] },
        translator_notes: []
      }
    };
    expect(validateBlockWorkspacePayload(payload({
      blocks: [valid],
      pagination: { offset: 0, limit: 40, total: 1, has_more: false },
      target_summary: {
        total_blocks: 1,
        active_targets: 1,
        structurally_valid_active_targets: 1,
        document_complete: true
      }
    })).blocks[0].id).toBe("b1");
    expect(() => validateBlockWorkspacePayload(payload({
      blocks: [{ ...valid, source: { editor: { ...formattedSource, content: '<img src="x" onerror="alert(1)"/>' } } }],
      pagination: { offset: 0, limit: 40, total: 1, has_more: false },
      target_summary: { total_blocks: 1, active_targets: 1, structurally_valid_active_targets: 1, document_complete: true }
    }))).toThrow(/unsupported inline element/);
    expect(() => validateBlockWorkspacePayload(payload({
      blocks: [{ ...valid, target: { ...valid.target, editor: { ...valid.target.editor, content: "Przekład bez struktury" } } }],
      pagination: { offset: 0, limit: 40, total: 1, has_more: false },
      target_summary: { total_blocks: 1, active_targets: 1, structurally_valid_active_targets: 1, document_complete: true }
    }))).toThrow(/source structure changed/);
  });

  it("uses the block endpoint and sends the exact candidate CAS DTO", async () => {
    const fetchJson = vi.fn()
      .mockResolvedValueOnce(payload())
      .mockResolvedValueOnce({ block: { id: "b1" } });
    const client = createBlockWorkspaceClient({ fetchJson }, "project/1");
    await client.load({ offset: 40, limit: 40 });
    const candidate = {
      contract_version: "translation-block-editor-v1",
      content: "translated",
      fields: []
    };
    await client.saveCandidate("b1", candidate, 3);
    expect(fetchJson).toHaveBeenCalledTimes(2);
    expect(fetchJson.mock.calls[0][0]).toContain("block-workspace?offset=40&limit=40");
    expect(JSON.parse(fetchJson.mock.calls[1][1].body)).toEqual({
      candidate,
      expected_active_version: 3
    });
    expect(fetchJson.mock.calls[1][1].headers).toEqual({
      "Content-Type": "application/json",
      "Idempotency-Key": expect.stringMatching(/^translator\.block\.b1\./)
    });
  });

  it("sends structural corrections against the explicit active plan", async () => {
    const fetchJson = vi.fn().mockResolvedValue({
      success: true,
      source_plan: { id: "plan-2", version: 2 }
    });
    const client = createBlockWorkspaceClient({ fetchJson }, "project/1");

    await client.correctBlock(
      "block/1",
      "merge_next",
      { id: "plan-1", version: 1 }
    );

    expect(fetchJson.mock.calls[0][0]).toBe(
      "/addons/translator/api/projects/project%2F1/blocks/block%2F1/correction"
    );
    expect(fetchJson.mock.calls[0][1]).toEqual(expect.objectContaining({
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": expect.stringMatching(
          /^translator\.block\.correction\.block\/1\.merge_next\./
        )
      }
    }));
    expect(JSON.parse(fetchJson.mock.calls[0][1].body)).toEqual({
      action: "merge_next",
      expected_source_plan_id: "plan-1",
      expected_active_version: 1
    });
  });

  it("starts block translation with an idempotency key and fetches the verified preview", async () => {
    const fetchJson = vi.fn().mockResolvedValue({ operation: { id: "op-1", state: "queued" } });
    const fetchText = vi.fn().mockResolvedValue("<main>preview</main>");
    const client = createBlockWorkspaceClient({ fetchJson, fetchText }, "project/1");

    await expect(client.translateAll()).resolves.toMatchObject({ operation: { id: "op-1" } });
    await expect(client.preview()).resolves.toBe("<main>preview</main>");

    expect(fetchJson).toHaveBeenCalledWith(
      "/addons/translator/api/projects/project%2F1/translate_all",
      expect.objectContaining({
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": expect.stringMatching(/^translator\.block\.translate\.project\/1\./)
        },
        body: JSON.stringify({ scope: { mode: "pending", block_ids: [] } })
      })
    );
    expect(fetchText).toHaveBeenCalledWith(
      "/addons/translator/api/projects/project%2F1/preview",
      expect.objectContaining({})
    );
  });

  it("requests cancellation through the owner-only project endpoint", async () => {
    const fetchJson = vi.fn().mockResolvedValue({
      success: true,
      operation: { id: "op-1", state: "cancel_requested" }
    });
    const client = createBlockWorkspaceClient({ fetchJson }, "project/1");

    await expect(client.cancelActiveOperation()).resolves.toMatchObject({
      operation: { state: "cancel_requested" }
    });
    expect(fetchJson).toHaveBeenCalledWith(
      "/addons/translator/api/projects/project%2F1/cancel",
      expect.objectContaining({ method: "POST", headers: { Accept: "application/json" } })
    );
  });

  it("sends the page-break choice only for paginated formats", async () => {
    const fetchJson = vi.fn().mockResolvedValue({ job_id: "export" });
    const client = createBlockWorkspaceClient({ fetchJson }, "project-1");
    await client.exportDocument("pdf", { startH2OnNewPage: true });
    expect(JSON.parse(fetchJson.mock.calls[0][1].body)).toEqual({ export_metadata: {}, start_h2_on_new_page: true });
    await client.exportDocument("epub", { startH2OnNewPage: true });
    expect(JSON.parse(fetchJson.mock.calls[1][1].body)).toEqual({ export_metadata: {}, epub_options: {} });
  });

  it("pins an explicit export review requirement and rejects an unknown kind", async () => {
    const fetchJson = vi.fn().mockResolvedValue({ job_id: "export" });
    const client = createBlockWorkspaceClient({ fetchJson }, "project-1");
    await client.exportDocument("epub", { reviewRequirement: "bilingual" });
    expect(JSON.parse(fetchJson.mock.calls[0][1].body)).toEqual({ export_metadata: {}, epub_options: {}, review_requirement: "bilingual" });
    await expect(client.exportDocument("epub", { reviewRequirement: "approval" })).rejects.toThrow("Invalid export review requirement");
    expect(fetchJson).toHaveBeenCalledOnce();
  });

  it("binds JPEG to both preflight and export and rejects invalid image modes", async () => {
    const fetchJson = vi.fn().mockResolvedValue({ job_id: "export" });
    const client = createBlockWorkspaceClient({ fetchJson }, "project-1");
    await client.epubPreflight({ epubImageMode: "jpeg" });
    await client.exportDocument("epub", { epubImageMode: "jpeg" });
    for (const [, options] of fetchJson.mock.calls) expect(JSON.parse(options.body).epub_image_mode).toBe("jpeg");
    await expect(client.epubPreflight({ epubImageMode: "avif" })).rejects.toThrow("Invalid EPUB image format");
    await expect(client.exportDocument("pdf", { epubImageMode: "jpeg" })).rejects.toThrow("Invalid EPUB image format");
    expect(fetchJson).toHaveBeenCalledTimes(2);
  });

  it("queues only supported semantic export formats and exposes the exact job status URL", async () => {
    const fetchJson = vi.fn()
      .mockResolvedValueOnce({
        operation: { id: "export-1", state: "queued" },
        job_id: "job-1",
        status_url: "/addons/translator/api/projects/project%2F1/jobs/job-1"
      })
      .mockResolvedValueOnce({ status: "completed", result: { kind: "file_download", download_url: "/addons/translator/api/projects/project%2F1/jobs/job-1/download" } });
    const client = createBlockWorkspaceClient({ fetchJson }, "project/1");

    await expect(client.exportDocument("epub")).resolves.toMatchObject({
      operation: { id: "export-1" },
      status_url: "/addons/translator/api/projects/project%2F1/jobs/job-1"
    });
    await expect(client.jobStatus("/addons/translator/api/projects/project%2F1/jobs/job-1")).resolves.toMatchObject({ status: "completed" });
    await expect(client.exportDocument("txt")).rejects.toThrow(/only EPUB, DOCX, PDF, and HTML/);

    expect(fetchJson.mock.calls[0][0]).toBe("/addons/translator/api/projects/project%2F1/export?format=epub");
    expect(fetchJson.mock.calls[0][1]).toEqual(expect.objectContaining({
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "Idempotency-Key": expect.stringMatching(/^translator\.block\.export\.epub\./)
      }
    }));
    expect(fetchJson.mock.calls[1]).toEqual([
      "/addons/translator/api/projects/project%2F1/jobs/job-1",
      expect.objectContaining({})
    ]);
  });
});
