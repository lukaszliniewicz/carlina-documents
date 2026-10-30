import { cleanup, fireEvent, render, waitFor } from "@testing-library/svelte";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import BlockWorkspaceApp from "./BlockWorkspaceApp.svelte";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const candidate = {
  contract_version: "target-block-result-v1",
  source_plan_id: "plan-1",
  source_plan_hash: "a".repeat(64),
  block_id: "block-1",
  structural_fingerprint: "b".repeat(64),
  parts: [{ kind: "text", text: "original" }],
  lexical_attributes: []
};

const sourceEditor = {
  contract_version: "translation-block-editor-v1",
  content: "source",
  fields: []
};

function editorFromCandidate(candidateValue) {
  return {
    contract_version: "translation-block-editor-v1",
    content: (candidateValue?.parts || []).filter((part) => part.kind === "text").map((part) => part.text).join(""),
    fields: []
  };
}

function block(candidateValue, activeVersion) {
  return {
    id: "block-1",
    order: 1,
    role: "paragraph",
    source: { editor: sourceEditor },
    reader_notes: [],
    review: { decision: "pending", version: 0, rationale: null, actor: null, updated_at: null },
    target: {
      active_version: activeVersion,
      editor: editorFromCandidate(candidateValue),
      validation: { valid: true, findings: [] },
      translator_notes: []
    }
  };
}

function verifiedBlock(candidateValue = candidate, activeVersion = 1) {
  const item = block(candidateValue, activeVersion);
  return {
    ...item,
    target: {
      ...item.target,
      validation: { valid: true, findings: [] }
    }
  };
}

function workspace(blockValue) {
  return workspacePage([blockValue]);
}

function workspacePage(blockValues, {
  offset = 0,
  total = blockValues.length,
  latestOperation = {},
  sourcePlan = { id: "plan-1", version: 1 }
} = {}) {
  const activeTargets = blockValues.filter((item) => item.target).length;
  return {
    contract: "translator-block-workspace-v5",
    access: { role: "owner", label: null },
    project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1", source_lang: "English", target_lang: "Polish" },
    source_plan: sourcePlan,
    latest_operation: latestOperation,
    target_summary: {
      total_blocks: total,
      active_targets: activeTargets,
      structurally_valid_active_targets: activeTargets,
      document_complete: activeTargets === total
    },
    blocks: blockValues,
    pagination: { offset, limit: 40, total, has_more: offset + blockValues.length < total }
  };
}

function workspaceStatus(latestOperation = {}) {
  return {
    contract: "translator-project-status-v1",
    project_id: "project-1",
    status: ["completed", "partial", "failed", "cancelled"].includes(latestOperation.state)
      ? latestOperation.state
      : "translating",
    latest_operation: latestOperation
  };
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });
}

function epubPreflightResponse(options = {}) {
  return jsonResponse({
    options: JSON.parse(options.body || "{}").epub_options || {},
    review_hash: "c".repeat(64), numbering_revision: 0, suggested_chapter_level: 2,
    outline: { input_sha256: "a".repeat(64), headings: [], tables: [], images: [], pages: [] },
    quality: { schema: "epub-quality-v1", profile: "publishing-v1", input_sha256: "a".repeat(64), options_sha256: "b".repeat(64), artifact_sha256: "0".repeat(64), content_sha256: "0".repeat(64), counts: {}, epubcheck_version: null, issue_count: 0, issues: [], checks: { structure: "not_run", epubcheck: "not_run", ace: "not_run", manual: "not_reviewed" } }
  });
}

describe("BlockWorkspaceApp presentation", () => {
  it("suggests missing book details while preserving entered values and credits", async () => {
    const proposal = deferred();
    let requested;
    vi.stubGlobal("fetch", vi.fn((url, options = {}) => {
      if (String(url).includes("/block-workspace")) return Promise.resolve(jsonResponse(workspace(verifiedBlock())));
      if (String(url).endsWith("/export-metadata")) return Promise.resolve(jsonResponse({ version: 3, metadata: { title: "Saved title", translators: ["Named translator"], rights: "User rights" } }));
      if (String(url).includes("/api/llm/models")) return Promise.resolve(jsonResponse([{ id: "test/model", name: "Test model", provider: "Test" }]));
      if (String(url).endsWith("/settings")) return Promise.resolve(jsonResponse({ translator_model: "test/model" }));
      if (String(url).endsWith("/export-metadata/generate")) {
        expect(new Headers(options.headers).get("Content-Type")).toBe("application/json");
        requested = JSON.parse(options.body); return proposal.promise;
      }
      throw new Error(`Unexpected request: ${url}`);
    }));
    const view = render(BlockWorkspaceApp, { props: { project: { id: "project-1", engine_contract: "document-block-v1" }, bootstrapData: { projectId: "project-1" } } });
    await waitFor(() => expect(view.getByRole("button", { name: "Export document" }).disabled).toBe(false));
    await fireEvent.click(view.getByRole("button", { name: "Export document" }));
    const suggest = await view.findByRole("button", { name: "Suggest missing details" });
    await fireEvent.input(view.getByLabelText("Title"), { target: { value: "My title" } });
    await fireEvent.click(suggest);
    expect(requested).toMatchObject({ expected_version: 3, model_id: "test/model", metadata: { title: "My title", translators: ["Named translator"], rights: "User rights" } });
    expect(view.getByRole("button", { name: "Export and download" }).disabled).toBe(true);
    proposal.resolve(jsonResponse({ metadata: { title: "Ignore this overwrite", authors: ["Known source author"], subtitle: "Suggested subtitle", description: "A bounded description.", subjects: ["Philosophy"], translators: ["Never apply model credits"] } }));
    await view.findByText(/Suggestions ready/);
    expect(view.getByLabelText("Title").value).toBe("My title");
    expect(view.getByLabelText("Translators").value).toBe("Named translator");
    expect(view.getByLabelText("Authors").value).toBe("Known source author");
    expect(view.getByLabelText("Subtitle").value).toBe("Suggested subtitle");
    expect(view.getByLabelText("Description").value).toBe("A bounded description.");
  });

  it("refreshes externally updated export metadata when the dialog is reopened without discarding local edits", async () => {
    let metadataReads = 0;
    vi.stubGlobal("fetch", vi.fn((url) => {
      const path = String(url);
      if (path.includes("/block-workspace")) return Promise.resolve(jsonResponse(workspace(verifiedBlock())));
      if (path.endsWith("/export-metadata")) {
        metadataReads += 1;
        return Promise.resolve(jsonResponse(metadataReads === 1
          ? { version: 1, metadata: { title: "Saved title", authors: ["Old editor"], translators: ["Old translator"] } }
          : { version: 2, metadata: { title: "Remote title", authors: ["Lothar Geis (ed.)"], translators: ["Lukasz Liniewicz"] } }));
      }
      if (path.includes("/api/llm/models")) return Promise.resolve(jsonResponse([]));
      if (path.endsWith("/settings")) return Promise.resolve(jsonResponse({}));
      throw new Error(`Unexpected request: ${url}`);
    }));

    const view = render(BlockWorkspaceApp, {
      props: { project: { id: "project-1", engine_contract: "document-block-v1" }, bootstrapData: { projectId: "project-1" } }
    });
    await waitFor(() => expect(view.getByRole("button", { name: "Export document" }).disabled).toBe(false));
    await fireEvent.click(view.getByRole("button", { name: "Export document" }));
    await waitFor(() => expect(view.getByLabelText("Authors").value).toBe("Old editor"));
    await fireEvent.input(view.getByLabelText("Title"), { target: { value: "Local draft title" } });
    await fireEvent.click(view.getByRole("button", { name: "Close", exact: true }));

    await fireEvent.click(view.getByRole("button", { name: "Export document" }));
    await waitFor(() => expect(view.getByLabelText("Authors").value).toBe("Lothar Geis (ed.)"));
    expect(metadataReads).toBe(2);
    expect(view.getByLabelText("Title").value).toBe("Local draft title");
    expect(view.getByLabelText("Translators").value).toBe("Lukasz Liniewicz");
    expect(view.getByText(/Saved book details changed elsewhere/)).toBeTruthy();
  });

  it("retries a sentence edit with the same receipt key and keeps its draft after failure", async () => {
    const aligned = verifiedBlock();
    aligned.target.alignment = { origin: "model_reported", review_state: "needs_review", groups: [{ order: 1, kind: "one_to_one", source_sentences: [{ order: 1, text: "source" }], target_sentences: [{ order: 1, text: "original" }] }] };
    const writes = [];
    const fetchMock = vi.fn((url, options = {}) => {
      if (String(url).includes("/block-workspace")) return Promise.resolve(jsonResponse({ ...workspace(aligned), contract: "translator-block-workspace-v6", review_mode: "aligned_sentences" }));
      if (String(url).endsWith("/candidate")) {
        writes.push(options);
        if (writes.length === 1) return Promise.resolve(jsonResponse({ error: "unavailable" }, 503));
        const updated = structuredClone(aligned);
        updated.target.active_version = 2;
        updated.target.editor.content = "Changed. Still one segment.";
        updated.target.alignment.origin = "user_edited";
        updated.target.alignment.groups[0].target_sentences[0].text = updated.target.editor.content;
        return Promise.resolve(jsonResponse({ block: updated }));
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceApp, { props: { project: { id: "project-1", engine_contract: "document-block-v1" }, bootstrapData: { projectId: "project-1" } } });
    await fireEvent.click(await view.findByRole("button", { name: "Edit translated sentence 1" }));
    expect(view.getByRole("button", { name: "Export document" }).disabled).toBe(true);
    await fireEvent.input(view.getByRole("textbox", { name: "Translation · sentence 1" }), { target: { value: "Changed. Still one segment." } });
    await fireEvent.click(view.getByRole("button", { name: "Save", exact: true }));
    await waitFor(() => expect(view.getByRole("alert")).toBeTruthy());
    expect(view.getByRole("textbox", { name: "Translation · sentence 1" }).value).toBe("Changed. Still one segment.");
    await fireEvent.click(view.getByRole("button", { name: "Save", exact: true }));
    await waitFor(() => expect(view.queryByRole("textbox", { name: "Translation · sentence 1" })).toBeNull());
    expect(JSON.parse(writes[0].body)).toEqual({ expected_active_version: 1, sentence_edit: { action: "replace", sentence_order: 1, text: "Changed. Still one segment." } });
    expect(new Headers(writes[0].headers).get("Idempotency-Key")).toBe(new Headers(writes[1].headers).get("Idempotency-Key"));
    expect(view.container.querySelectorAll(".target-side [data-sentence-order]")).toHaveLength(1);
  });

  it("applies note and review save responses without requesting a whole block", async () => {
    const original = verifiedBlock();
    const note = { id: "note-1", version: 1, body: "A saved note.", state: "active", anchor: { kind: "whole_block", text: null, occurrence_index: null }, citation_ids: [], author: { type: "owner", label: "Owner" } };
    const review = { decision: "approved", version: 1, rationale: null, actor: { type: "owner", label: "Owner" }, updated_at: null };
    const fetchMock = vi.fn((url, options) => {
      if (String(url).includes("/block-workspace")) return Promise.resolve(jsonResponse(workspace(original)));
      if (String(url).endsWith("/review")) {
        expect(JSON.parse(options.body).rationale).toBe("");
        return Promise.resolve(jsonResponse({ review }));
      }
      if (String(url).endsWith("/reader-notes")) return Promise.resolve(jsonResponse({ note }, 201));
      if (String(url).endsWith("/reader-notes/note-1/state")) return Promise.resolve(jsonResponse({ note: { ...note, state: "retired", version: 2 } }));
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceApp, { props: { project: { id: "project-1", engine_contract: "document-block-v1" }, bootstrapData: { projectId: "project-1" } } });
    await fireEvent.click(await view.findByRole("button", { name: "Review block 1: Pending" }));
    await fireEvent.click(view.getByRole("button", { name: "Approved", exact: true }));
    await view.findByRole("button", { name: "Review block 1: Approved" });
    await fireEvent.click(view.getByRole("button", { name: "Notes for block 1: 0" }));
    await fireEvent.click(view.getByRole("button", { name: "Add note", exact: true }));
    await fireEvent.input(view.getByLabelText("Reader-facing note"), { target: { value: note.body } });
    await fireEvent.click(view.getByRole("button", { name: "Save annotation" }));
    await view.findByRole("button", { name: "Notes for block 1: 1" });
    expect(view.getByText(note.body)).toBeTruthy();
    expect(view.queryByRole("alert")).toBeNull();
    await fireEvent.click(view.getByRole("button", { name: "Retire", exact: true }));
    await view.findByRole("button", { name: "Notes for block 1: 0" });
    expect(view.queryByRole("alert")).toBeNull();
    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("/block-workspace"))).toHaveLength(1);
  });

  it("keeps its padded workspace inside the shell width", () => {
    const source = readFileSync(
      resolve(process.cwd(), "src/workspace/BlockWorkspaceApp.svelte"),
      "utf8"
    );
    expect(source).toMatch(
      /\.block-workspace\s*\{[^}]*box-sizing:border-box;[^}]*width:100%;[^}]*min-width:0;/
    );
  });

  it("shows source beside translation without exposing alignment scaffolding", async () => {
    const fetchMock = vi.fn((url) => {
      if (String(url).includes("/block-workspace")) return Promise.resolve(jsonResponse(workspace(verifiedBlock())));
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });

    await waitFor(() => expect(view.getByTestId("source-target-comparison")).toBeTruthy());
    const comparison = view.getByTestId("source-target-comparison");
    expect(comparison.textContent).toContain("source");
    expect(comparison.querySelector('[contenteditable="true"]').textContent).toBe("original");
    expect(view.container.textContent).toContain("Translated");
    expect(view.container.textContent).not.toContain("Needs review");
    expect(view.container.textContent).not.toContain("Low confidence");
    expect(view.container.textContent).not.toContain("target-block-result-v1");
    expect(view.container.textContent).not.toContain("deterministic");
    expect(comparison.textContent).not.toContain("Formatting and fixed references");
    expect(view.container.querySelector('[data-block-status="translated"]')).toBeTruthy();
    expect(view.queryByRole("button", { name: "No unsaved changes." })).toBeNull();
  });

  it("uses the aligned-sentence review surface only for a v6 sentence workspace", async () => {
    const alignedBlock = verifiedBlock();
    alignedBlock.target.alignment = {
      origin: "model_reported",
      review_state: "needs_review",
      groups: [{
        order: 1,
        kind: "one_to_one",
        source_sentences: [{ order: 1, text: "source" }],
        target_sentences: [{ order: 1, text: "original" }]
      }]
    };
    const payload = {
      ...workspace(alignedBlock),
      contract: "translator-block-workspace-v6",
      review_mode: "aligned_sentences"
    };
    const fetchMock = vi.fn((url) => {
      if (String(url).includes("/block-workspace")) return Promise.resolve(jsonResponse(payload));
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });

    expect((await view.findAllByText("Aligned sentence groups")).length).toBeGreaterThanOrEqual(1);
    await waitFor(() => expect(view.container.querySelector('[data-block-id="block-1"]')?.dataset.reviewMode).toBe("aligned_sentences"));
    expect(view.container.querySelector('[data-block-id="block-1"]')?.dataset.hasAlignment).toBe("true");
    expect(view.getByRole("img", { name: /Model-reported pairing/ })).toBeTruthy();
    expect(view.queryByTestId("source-target-comparison")).toBeNull();
  });

  it("shows active translator notes separately from editable target text", async () => {
    const withNote = verifiedBlock();
    withNote.target.translator_notes = [{
      id: "note-1",
      anchor_text: "Hanshi Festival",
      body: "A traditional observance during which lighting fires was avoided.",
      author_type: "model"
    }];
    const fetchMock = vi.fn((url) => {
      if (String(url).includes("/block-workspace")) return Promise.resolve(jsonResponse(workspace(withNote)));
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });

    const notes = await waitFor(() => view.getByRole("group", { name: /translator notes for block 1/i }));
    expect(notes.open).toBe(false);
    await fireEvent.click(view.getByRole("button", { name: "Notes for block 1: 1" }));
    expect(notes.open).toBe(true);
    expect(view.container.textContent).toContain("Hanshi Festival");
    expect(view.container.textContent).toContain("lighting fires was avoided");
  });

  it("keeps split and merged sentences together in their own paired row", async () => {
    const aligned = verifiedBlock();
    aligned.target.alignment = {
      origin: "model_reported",
      review_state: "needs_review",
      groups: [
        {
          order: 1, kind: "split",
          source_sentences: [{ order: 1, text: "משפט אחד." }],
          target_sentences: [{ order: 1, text: "First rendering." }, { order: 2, text: "A continuation." }]
        },
        {
          order: 2, kind: "merge",
          source_sentences: [{ order: 2, text: "משפט שני." }, { order: 3, text: "משפט שלישי." }],
          target_sentences: [{ order: 3, text: "Two statements become one." }]
        }
      ]
    };
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse({
      ...workspace(aligned), contract: "translator-block-workspace-v6", review_mode: "aligned_sentences"
    }))));
    const view = render(BlockWorkspaceApp, {
      props: { project: { id: "project-1", engine_contract: "document-block-v1" }, bootstrapData: { projectId: "project-1" } }
    });
    await waitFor(() => expect(view.container.querySelectorAll('[data-alignment-group-row="true"]').length).toBe(2));
    const [split, merge] = view.container.querySelectorAll('[data-alignment-group-row="true"]');
    expect(split.querySelector(".source-side").textContent).toContain("משפט אחד.");
    expect(split.querySelector(".target-side").getAttribute("aria-label")).toContain("Translated sentences 1–2");
    expect(split.querySelectorAll(".target-side p").length).toBe(2);
    expect(split.textContent).not.toContain("Two statements become one.");
    expect(merge.querySelector(".source-side").getAttribute("aria-label")).toContain("Source sentences 2–3");
    expect(merge.querySelectorAll(".source-side p").length).toBe(2);
    expect(merge.querySelector(".target-side").textContent).toContain("Two statements become one.");
    expect([...view.container.querySelectorAll("[data-sentence-order]")].every((sentence) => sentence.dir === "auto")).toBe(true);
  });

  it("offers quiet block corrections and refreshes against the successor plan", async () => {
    const first = verifiedBlock();
    const second = {
      ...verifiedBlock(),
      id: "block-2",
      order: 2,
      source: {
        editor: { ...sourceEditor, content: "second source" }
      }
    };
    const successor = {
      ...first,
      id: "block-merged",
      source: {
        editor: { ...sourceEditor, content: "source second source" }
      },
      target: null
    };
    let workspaceReads = 0;
    const fetchMock = vi.fn((url, options = {}) => {
      if (String(url).includes("/block-workspace")) {
        workspaceReads += 1;
        return Promise.resolve(jsonResponse(
          workspaceReads === 1
            ? workspacePage([first, second])
            : workspacePage([successor], {
              sourcePlan: { id: "plan-2", version: 2 }
            })
        ));
      }
      if (String(url).endsWith("/blocks/block-1/correction") && options.method === "POST") {
        return Promise.resolve(jsonResponse({
          success: true,
          source_plan: { id: "plan-2", version: 2 }
        }));
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });

    await waitFor(() => expect(view.getByRole("button", { name: "Block 1 actions" })).toBeTruthy());
    await fireEvent.click(view.getByRole("button", { name: "Block 1 actions" }));
    await fireEvent.click(view.getByRole("menuitem", { name: "Merge with next" }));
    expect(view.getByText("Merge block 1 with the next block?")).toBeTruthy();
    await fireEvent.click(view.getByRole("button", { name: "Merge blocks" }));

    await waitFor(() => expect(view.container.textContent).toContain("source second source"));
    const correctionCall = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/blocks/block-1/correction"));
    expect(JSON.parse(correctionCall[1].body)).toEqual({
      action: "merge_next",
      expected_source_plan_id: "plan-1",
      expected_active_version: 1
    });
    expect(view.queryByRole("button", { name: "Block 2 actions" })).toBeNull();
  });

  it("renders source and target formatting in the continuous comparison", async () => {
    const formatted = verifiedBlock();
    const formattedSource = {
      contract_version: "translation-block-editor-v1",
      content: "The <em>source</em> paragraph.",
      fields: []
    };
    formatted.source = { ...formatted.source, editor: formattedSource };
    formatted.target = {
      ...formatted.target,
      editor: {
        contract_version: "translation-block-editor-v1",
        content: "Akapit <em>docelowy</em>.",
        fields: []
      }
    };
    const fetchMock = vi.fn((url) => {
      if (String(url).includes("/block-workspace")) return Promise.resolve(jsonResponse(workspace(formatted)));
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });

    const comparison = await view.findByTestId("source-target-comparison");
    expect(comparison.querySelector(".source-content em").textContent).toBe("source");
    expect(comparison.querySelector(".translation-surface em").textContent).toBe("docelowy");
    expect(comparison.querySelectorAll("textarea")).toHaveLength(0);
  });

  it("presents field-only accessibility metadata without a fake prose editor", async () => {
    const accessible = verifiedBlock();
    accessible.role = "accessibility_text";
    accessible.source = {
      editor: {
        contract_version: "translation-block-editor-v1",
        content: "",
        fields: [{ name: "title", value: "Greeting" }]
      }
    };
    accessible.target = {
      ...accessible.target,
      editor: {
        contract_version: "translation-block-editor-v1",
        content: "",
        fields: [{ name: "title", value: "Powitanie" }]
      }
    };
    const fetchMock = vi.fn((url) => {
      if (String(url).includes("/block-workspace")) return Promise.resolve(jsonResponse(workspace(accessible)));
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });

    await waitFor(() => expect(view.getByRole("textbox", { name: "Translated title" })).toBeTruthy());
    expect(view.getByRole("heading", { level: 2, name: "Title" })).toBeTruthy();
    expect(view.queryByRole("textbox", { name: "Translation text" })).toBeNull();
    expect(view.container.textContent).toContain("Accessible text");
    expect(view.container.textContent).not.toContain("This block is carried by its descriptions");
    expect(view.container.textContent).not.toContain("Translated descriptions");
  });

  it("presents a lone image description without redundant alt-field chrome", async () => {
    const accessible = verifiedBlock();
    accessible.role = "accessibility_text";
    accessible.source = {
      editor: {
        contract_version: "translation-block-editor-v1",
        content: "",
        fields: [{ name: "alt", value: "Black and white portrait" }]
      }
    };
    accessible.target = {
      ...accessible.target,
      editor: {
        contract_version: "translation-block-editor-v1",
        content: "",
        fields: [{ name: "alt", value: "Czarno-biały portret" }]
      }
    };
    const fetchMock = vi.fn((url) => {
      if (String(url).includes("/block-workspace")) return Promise.resolve(jsonResponse(workspace(accessible)));
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });

    await waitFor(() => expect(view.getByRole("textbox", { name: "Translated alt" })).toBeTruthy());
    expect(view.getByRole("heading", { level: 2, name: "Image description" })).toBeTruthy();
    expect(view.getByText("Black and white portrait")).toBeTruthy();
    expect(view.container.querySelector(".source-fields")).toBeNull();
    expect(view.container.querySelector(".lexical-fields")).toBeNull();
    expect(view.container.textContent).not.toContain("Accessible text");
  });

  it("exposes glossary terms and notes without showing contract identifiers", async () => {
    const glossaryEntry = {
      concept_id: "private-concept-1",
      active_version: 1,
      source_term: "Widget",
      target_term: "Moduł",
      notes: "Use this in the technical register.",
      scope: "document",
      updated_at: null
    };
    const glossary = {
      contract: "translator-block-glossary-v3",
      project_id: "project-1",
      source_plan_id: "plan-1",
      entries: [glossaryEntry],
      proposals: [],
      suggestions: []
    };
    const fetchMock = vi.fn((url) => {
      if (String(url).includes("/block-workspace")) return Promise.resolve(jsonResponse(workspace(verifiedBlock())));
      if (String(url).includes("/block-glossary")) return Promise.resolve(jsonResponse(glossary));
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });

    await waitFor(() => expect(view.getByRole("button", { name: /^Glossary/ })).toBeTruthy());
    await fireEvent.click(view.getByRole("button", { name: /^Glossary/ }));
    await waitFor(() => expect(view.container.textContent).toContain("Use this in the technical register."));
    expect(view.container.textContent).toContain("Widget");
    expect(view.container.textContent).toContain("Moduł");
    expect(view.container.textContent).not.toContain("private-concept-1");
    expect(view.container.textContent).not.toContain("translator-block-glossary-v3");
  });

  it("refreshes the glossary on reopen without losing a hidden draft", async () => {
    const glossary = {
      contract: "translator-block-glossary-v3",
      project_id: "project-1",
      source_plan_id: "plan-1",
      entries: [],
      proposals: [],
      suggestions: []
    };
    const fetchMock = vi.fn((url) => {
      if (String(url).includes("/block-workspace")) return Promise.resolve(jsonResponse(workspace(verifiedBlock())));
      if (String(url).includes("/block-glossary")) return Promise.resolve(jsonResponse(glossary));
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });

    await view.findByTestId("source-target-comparison");
    expect(view.container.querySelector(".workspace-context")).toBeNull();
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/block-glossary"))).toBe(false);
    const launcher = view.getByRole("button", { name: /^Glossary/ });
    launcher.focus();
    await fireEvent.click(launcher);
    const dialog = await view.findByRole("dialog", { name: "Glossary" });
    expect(dialog.classList).toContain("size-lg");
    await waitFor(() => expect(launcher.getAttribute("aria-label")).toBe("Glossary (0)"));
    await fireEvent.click(view.getByRole("button", { name: "Add term" }));
    await fireEvent.input(view.getByLabelText("Source term"), { target: { value: "Keep this draft" } });
    await fireEvent(dialog, new Event("cancel", { cancelable: true }));
    await waitFor(() => expect(view.queryByRole("dialog", { name: "Glossary" })).toBeNull());
    expect(document.activeElement).toBe(launcher);
    glossary.suggestions.push({
      id: "external-term-1", source_term: "Atlas", target_term: "Atlas",
      notes: "Added through MCP while the glossary was closed.", state: "pending",
      version: 1, created_at: "2026-09-07T04:00:00Z",
      author: { type: "external", label: "Translator" }
    });
    await fireEvent.click(launcher);
    await waitFor(() => expect(launcher.getAttribute("aria-label")).toBe("Glossary (1)"));
    expect(view.container.textContent).toContain("Added through MCP while the glossary was closed.");
    expect(view.getByLabelText("Source term").value).toBe("Keep this draft");
    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("/block-glossary"))).toHaveLength(2);
  });

  it("reloads the current glossary after a concurrent edit conflict", async () => {
    const glossary = (version, target) => ({
      contract: "translator-block-glossary-v3",
      project_id: "project-1",
      source_plan_id: "plan-1",
      entries: [{
        concept_id: "concept-1",
        active_version: version,
        source_term: "Widget",
        target_term: target,
        notes: null,
        scope: "document",
        updated_at: null
      }],
      proposals: [],
      suggestions: []
    });
    let glossaryReads = 0;
    const fetchMock = vi.fn((url, options = {}) => {
      if (String(url).includes("/block-workspace")) return Promise.resolve(jsonResponse(workspace(verifiedBlock())));
      if (String(url).includes("/block-glossary") && options.method === "PUT") {
        return Promise.resolve(jsonResponse({ error: "Conflict", code: "glossary-active-conflict" }, 409));
      }
      if (String(url).includes("/block-glossary")) {
        glossaryReads += 1;
        return Promise.resolve(jsonResponse(glossary(glossaryReads, glossaryReads === 1 ? "Moduł" : "Widżet")));
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });

    await fireEvent.click(await view.findByRole("button", { name: /^Glossary/ }));
    await waitFor(() => expect(view.container.textContent).toContain("Moduł"));
    await fireEvent.click(view.getByRole("button", { name: "Edit" }));
    await fireEvent.click(view.getByRole("button", { name: "Save for next run" }));

    await waitFor(() => expect(view.container.textContent).toContain("Widżet"));
    expect(glossaryReads).toBe(2);
    expect(view.getByRole("alert").textContent).toContain("latest version is shown");
    expect(view.queryByRole("button", { name: "Save for next run" })).toBeNull();
  });

  it("keeps analysis terminology advisory until the owner reviews it", async () => {
    const proposal = {
      proposal_id: "proposal-1",
      concept_id: "concept-1",
      source_term: "signal",
      target_term: "sygnał",
      notes: "Keep distinct from sign.",
      scope: "document",
      proposed_at: "2026-09-01T00:00:00+00:00"
    };
    const suggested = {
      contract: "translator-block-glossary-v3",
      project_id: "project-1",
      source_plan_id: "plan-1",
      entries: [],
      proposals: [proposal],
      suggestions: []
    };
    const locked = {
      ...suggested,
      entries: [{
        concept_id: "concept-1",
        active_version: 1,
        source_term: "signal",
        target_term: "znak sygnałowy",
        notes: "Reviewed wording.",
        scope: "document",
        updated_at: "2026-09-01T00:01:00+00:00"
      }],
      proposals: []
    };
    const fetchMock = vi.fn((url, options = {}) => {
      if (String(url).includes("/block-workspace")) return Promise.resolve(jsonResponse(workspace(verifiedBlock())));
      if (String(url).includes("/block-glossary") && options.method === "PUT") {
        return Promise.resolve(jsonResponse(locked));
      }
      if (String(url).includes("/block-glossary")) return Promise.resolve(jsonResponse(suggested));
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });

    await fireEvent.click(await view.findByRole("button", { name: /^Glossary/ }));
    await waitFor(() => expect(view.container.textContent).toContain("Analysis suggestions"));
    expect(view.container.textContent).toContain("Keep distinct from sign.");
    await fireEvent.click(view.getByRole("button", { name: "Review" }));
    const target = view.getByRole("textbox", { name: "Preferred translation" });
    const notes = view.getByRole("textbox", { name: /Notes/ });
    expect(target.value).toBe("sygnał");
    await fireEvent.input(target, { target: { value: "znak sygnałowy" } });
    await fireEvent.input(notes, { target: { value: "Reviewed wording." } });
    await fireEvent.click(view.getByRole("button", { name: "Approve for next run" }));

    await waitFor(() => expect(view.container.textContent).toContain("znak sygnałowy"));
    expect(view.container.textContent).toContain("Locked terms");
    expect(view.container.textContent).not.toContain("Analysis suggestions");
    const putCall = fetchMock.mock.calls.find(([, options]) => options.method === "PUT");
    expect(JSON.parse(putCall[1].body)).toEqual({
      source_term: "signal",
      target_term: "znak sygnałowy",
      notes: "Reviewed wording.",
      accepted_target_forms: [],
      expected_active_version: 0,
      expected_proposal_id: "proposal-1"
    });
  });

  it("reloads the glossary projection after approving a collaborator suggestion", async () => {
    const pending = {
      contract: "translator-block-glossary-v3",
      project_id: "project-1",
      source_plan_id: "plan-1",
      entries: [],
      proposals: [],
      suggestions: [{
        id: "suggestion-1",
        version: 1,
        source_term: "Merkabah",
        target_term: "Merkawa",
        notes: "Reviewer proposal.",
        state: "pending",
        created_at: "2026-09-05T07:00:00Z",
        author: { type: "reviewer", label: "Daniel" }
      }]
    };
    const approved = {
      ...pending,
      entries: [{
        concept_id: "concept-1",
        active_version: 1,
        source_term: "Merkabah",
        target_term: "Merkawa",
        notes: "Reviewer proposal.",
        scope: "document",
        updated_at: "2026-09-05T07:01:00Z"
      }],
      suggestions: []
    };
    let glossaryReads = 0;
    let resolutionBody = null;
    const fetchMock = vi.fn((url, options = {}) => {
      const value = String(url);
      if (value.includes("/block-workspace")) return Promise.resolve(jsonResponse(workspace(verifiedBlock())));
      if (value.endsWith("/glossary-suggestions/suggestion-1/resolve") && options.method === "POST") {
        resolutionBody = JSON.parse(options.body);
        return Promise.resolve(jsonResponse({ suggestion: { ...pending.suggestions[0], state: "approved", version: 2 } }));
      }
      if (value.includes("/block-glossary")) {
        glossaryReads += 1;
        return Promise.resolve(jsonResponse(glossaryReads === 1 ? pending : approved));
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });

    await fireEvent.click(await view.findByRole("button", { name: /^Glossary/ }));
    await waitFor(() => expect(view.container.textContent).toContain("Reviewer proposal."));
    await fireEvent.click(view.getByRole("button", { name: "Review" }));
    await fireEvent.click(view.getByRole("button", { name: "Approve for next run" }));

    await waitFor(() => expect(glossaryReads).toBe(2));
    await waitFor(() => expect(view.container.textContent).toContain("Locked terms"));
    await waitFor(() => expect(view.container.textContent).not.toContain("Review suggestions"));
    expect(resolutionBody).toEqual({
      action: "approve",
      expected_version: 1,
      source_term: "Merkabah",
      target_term: "Merkawa",
      notes: "Reviewer proposal."
    });
  });
});

describe("BlockWorkspaceApp conflict handling", () => {
  it("guards navigation while a translation has unsaved changes and releases the guard after saving", async () => {
    vi.stubGlobal("fetch",vi.fn((url,options={})=>{
      if (String(url).includes('/block-workspace')) return Promise.resolve(jsonResponse(workspace(block(candidate,1))));
      if (options.method==='PUT') return Promise.resolve(jsonResponse({success:true,block:block({...candidate,parts:[{kind:'text',text:'Saved correction'}]},2)}));
      throw new Error(`Unexpected request: ${url}`);
    }));
    const view=render(BlockWorkspaceApp,{props:{project:{id:'project-1',engine_contract:'document-block-v1'},bootstrapData:{projectId:'project-1'}}});
    await waitFor(()=>expect(view.container.querySelector('[contenteditable="true"]')).toBeTruthy());
    vi.spyOn(window,'confirm').mockReturnValue(false);
    const editor=view.container.querySelector('[contenteditable="true"]');editor.textContent='Saved correction';await fireEvent.input(editor);
    expect(window.dispatchEvent(new Event('carlina:before-navigate',{cancelable:true}))).toBe(false);
    expect(window.dispatchEvent(new Event('beforeunload',{cancelable:true}))).toBe(false);
    expect(editor.textContent).toBe('Saved correction');
    await fireEvent.click(view.getByRole('button',{name:'Save translation',exact:true}));
    await waitFor(()=>expect(view.queryByRole('button',{name:'Save translation',exact:true})).toBeNull());
    await waitFor(()=>expect(window.dispatchEvent(new Event('carlina:before-navigate',{cancelable:true}))).toBe(true));
  });
  it("uses latest_block, preserves the local draft, and blocks saving until explicit reload", async () => {
    const latestCandidate = { ...candidate, parts: [{ kind: "text", text: "server latest" }] };
    const latestBlock = block(latestCandidate, 2);
    const fetchMock = vi.fn((url, options = {}) => {
      if (String(url).includes("/block-workspace")) return Promise.resolve(jsonResponse(workspace(block(candidate, 1))));
      if (options.method === "PUT") {
        return Promise.resolve(jsonResponse({
          error: "The block changed before this edit was saved.",
          code: "stale_active_version",
          latest_block: latestBlock,
          active_version: 2
        }, 409));
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });

    await waitFor(() => expect(view.container.querySelector('[contenteditable="true"]')).toBeTruthy());
    const editor = view.container.querySelector('[contenteditable="true"]');
    editor.textContent = "local draft";
    await fireEvent.input(editor);
    await fireEvent.click(view.container.querySelector('button[title="Save translation"]'));

    await waitFor(() => expect(view.container.querySelector('[data-testid="block-conflict"]')).toBeTruthy());
    expect(view.container.querySelector('[contenteditable="true"]').textContent).toBe("local draft");
    expect(view.container.querySelector('[data-testid="block-conflict"]').textContent).toContain("server latest");
    expect(view.container.querySelector('button[title="Reload the server version before saving."]').disabled).toBe(true);

    await fireEvent.click(view.container.querySelector('[data-testid="block-conflict"] button'));
    await waitFor(() => expect(view.container.querySelector('[data-testid="block-conflict"]')).toBeNull());
    expect(view.container.querySelector('[contenteditable="true"]').textContent).toBe("server latest");
  });
});

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
}

function secondBlock(candidateValue, activeVersion = 1) {
  return {
    ...block(candidateValue, activeVersion),
    id: "block-2",
    order: 2
  };
}

describe("BlockWorkspaceApp concurrent reads and writes", () => {
  it("retains a loaded page when a delayed status poll finishes after load more", async () => {
    const poll = deferred();
    const loadMore = deferred();
    const firstBlock = block(candidate, 1);
    const second = secondBlock({ ...candidate, block_id: "block-2", parts: [{ kind: "text", text: "second" }] });
    let initialDelivered = false;
    const fetchMock = vi.fn((url) => {
      const parsed = new URL(String(url), "http://localhost");
      const offset = parsed.searchParams.get("offset");
      if (parsed.pathname.endsWith("/status")) return poll.promise;
      if (offset === "0" && !initialDelivered) {
        initialDelivered = true;
        return Promise.resolve(jsonResponse(workspacePage([firstBlock], { total: 2, latestOperation: { id: "op-1", state: "running" } })));
      }
      if (offset === "1") return loadMore.promise;
      throw new Error(`Unexpected workspace offset: ${offset}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });

    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/status"))).toBe(true));
    await fireEvent.click(view.getByRole("button", { name: /load more blocks/i }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes("offset=1"))).toBe(true));
    loadMore.resolve(jsonResponse(workspacePage([second], { offset: 1, total: 2, latestOperation: { id: "op-1", state: "running" } })));
    await waitFor(() => expect(view.container.querySelector('[data-block-id="block-2"]')).toBeTruthy());

    poll.resolve(jsonResponse(workspaceStatus({ id: "op-1", state: "running" })));
    await waitFor(() => expect(view.container.querySelector('[data-block-id="block-2"]')).toBeTruthy());
    expect(view.container.querySelector('[data-block-id="block-2"] [contenteditable="false"]').textContent).toBe("second");
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("offset=1"))).toBe(true);
  });

  it("keeps active targets read-only until terminal status, then permits saving", async () => {
    const poll = deferred();
    const save = deferred();
    const firstBlock = block(candidate, 1);
    let initialDelivered = false;
    const fetchMock = vi.fn((url, options = {}) => {
      const parsed = new URL(String(url), "http://localhost");
      if (options.method === "PUT") return save.promise;
      if (parsed.pathname.endsWith("/status")) return poll.promise;
      if (parsed.searchParams.get("offset") === "0" && !initialDelivered) {
        initialDelivered = true;
        return Promise.resolve(jsonResponse(workspacePage([firstBlock], { latestOperation: { id: "op-1", state: "running" } })));
      }
      if (parsed.searchParams.get("offset") === "0") return Promise.resolve(jsonResponse(workspacePage([firstBlock], { latestOperation: { id: "op-1", state: "completed" } })));
      if (parsed.pathname.endsWith("/block-glossary")) return Promise.resolve(jsonResponse({ entries: [], proposals: [], suggestions: [] }));
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });

    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/status"))).toBe(true));
    expect(view.container.querySelector('[contenteditable="true"]')).toBeNull();
    expect(fetchMock.mock.calls.some(([, options]) => options?.method === "PUT")).toBe(false);
    poll.resolve(jsonResponse(workspaceStatus({ id: "op-1", state: "completed" })));
    await waitFor(() => expect(view.container.querySelector('[contenteditable="true"]')).toBeTruthy());
    const editor = view.container.querySelector('[contenteditable="true"]');
    editor.textContent = "saved candidate";
    await fireEvent.input(editor);
    await fireEvent.click(view.container.querySelector('button[title="Save translation"]'));
    await waitFor(() => expect(fetchMock.mock.calls.some(([, options]) => options?.method === "PUT")).toBe(true));

    const savedCandidate = { ...candidate, parts: [{ kind: "text", text: "saved candidate" }] };
    save.resolve(jsonResponse({ block: block(savedCandidate, 2) }));
    await waitFor(() => expect(view.container.querySelector('[contenteditable="true"]').textContent).toBe("saved candidate"));
    expect(view.container.querySelector('[data-testid="block-conflict"]')).toBeNull();
  });

  it("refreshes every loaded page when the lightweight status poll observes completion", async () => {
    const terminalStatus = deferred();
    const firstPending = { ...block(candidate, 0), target: null };
    const secondPending = { ...secondBlock({ ...candidate, block_id: "block-2" }, 0), target: null };
    const firstComplete = block({ ...candidate, parts: [{ kind: "text", text: "first complete" }] }, 1);
    const secondComplete = secondBlock({
      ...candidate,
      block_id: "block-2",
      parts: [{ kind: "text", text: "second complete" }]
    }, 1);
    let offsetZeroReads = 0;
    let glossaryReads = 0;
    const completeSummary = {
      total_blocks: 2,
      active_targets: 2,
      structurally_valid_active_targets: 2,
      document_complete: true
    };
    const fetchMock = vi.fn((url) => {
      const parsed = new URL(String(url), "http://localhost");
      const offset = parsed.searchParams.get("offset");
      if (parsed.pathname.endsWith("/status")) return terminalStatus.promise;
      if (parsed.pathname.endsWith("/block-glossary")) {
        glossaryReads += 1;
        return Promise.resolve(jsonResponse({
          contract: "translator-block-glossary-v3",
          project_id: "project-1",
          source_plan_id: "plan-1",
          entries: [],
          proposals: glossaryReads === 1 ? [] : [{
            proposal_id: "proposal-1",
            concept_id: "concept-1",
            source_term: "Begriff",
            target_term: "pojęcie",
            notes: "Analysis suggestion",
            scope: "document",
            proposed_at: "2026-09-01T00:00:00Z"
          }],
          suggestions: []
        }));
      }
      if (offset === "0") {
        offsetZeroReads += 1;
        if (offsetZeroReads === 1) {
          return Promise.resolve(jsonResponse(workspacePage([firstPending], {
            total: 2,
            latestOperation: { id: "op-1", state: "running" }
          })));
        }
        return Promise.resolve(jsonResponse({
          ...workspacePage([firstComplete], {
            total: 2,
            latestOperation: { id: "op-1", state: "completed" }
          }),
          target_summary: completeSummary
        }));
      }
      if (offset === "1") {
        if (offsetZeroReads === 1) {
          return Promise.resolve(jsonResponse(workspacePage([secondPending], {
            offset: 1,
            total: 2,
            latestOperation: { id: "op-1", state: "running" }
          })));
        }
        return Promise.resolve(jsonResponse({
          ...workspacePage([secondComplete], {
            offset: 1,
            total: 2,
            latestOperation: { id: "op-1", state: "completed" }
          }),
          target_summary: completeSummary
        }));
      }
      throw new Error(`Unexpected workspace offset: ${offset}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });

    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/status"))).toBe(true));
    await fireEvent.click(view.getByRole("button", { name: /^Glossary/ }));
    await waitFor(() => expect(glossaryReads).toBe(1));
    expect(view.container.querySelector('[data-block-id="block-1"]').dataset.blockStatus).toBe("pending");
    await fireEvent.click(view.getByRole("button", { name: /load more blocks/i }));
    await waitFor(() => expect(view.container.querySelector('[data-block-id="block-2"]')).toBeTruthy());
    terminalStatus.resolve(jsonResponse(workspaceStatus({ id: "op-1", state: "completed" })));
    await waitFor(() => expect(offsetZeroReads).toBe(2));
    await waitFor(() => expect(
      view.container.querySelector('[data-block-id="block-1"] [contenteditable="true"]').textContent
    ).toBe("first complete"));
    await waitFor(() => expect(
      view.container.querySelector('[data-block-id="block-2"] [contenteditable="true"]').textContent
    ).toBe("second complete"));
    await waitFor(() => expect(glossaryReads).toBe(2));
    await waitFor(() => expect(view.container.textContent).toContain("Begriff"));
    expect(view.container.textContent).toContain("pojęcie");
    expect(view.container.textContent).not.toContain("This block is carried by its descriptions");
  });
});

describe("BlockWorkspaceApp semantic actions", () => {
  it("lets a viewer inspect the exact saved dossier revision and its provenance", async () => {
    const summary = {
      dossier_id: "dossier-1",
      revision_id: "revision-1",
      version: 1,
      scope: { kind: "document", block_ids: [] },
      submitted_at: "2026-09-05T06:30:00+00:00"
    };
    const fetchMock = vi.fn((url) => {
      const value = String(url);
      if (value.includes("/research-dossiers/dossier-1?")) {
        return Promise.resolve(jsonResponse({
          ...summary,
          provenance: {
            method: "parallel external research",
            models: ["GPT-5.6"],
            agents: ["terminology", "allusions"],
            notes: "Corroborated against cited editions."
          },
          payload: {
            document_brief: "A difficult study translation.",
            style_guidance: ["Prefer conventional scholarly transliterations."],
            entities: [],
            terminology: [{
              source_term: "Ein Sof",
              target_term: "Ein Sof",
              notes: "Retain and explain.",
              source_block_ids: ["block-1"],
              confidence: 0.9
            }],
            references: [],
            allusions: [],
            unresolved_questions: [],
            annotation_proposals: []
          }
        }));
      }
      if (value.endsWith("/research-dossiers")) {
        return Promise.resolve(jsonResponse({ dossiers: [summary] }));
      }
      if (value.includes("/block-workspace")) {
        return Promise.resolve(jsonResponse({
          ...workspace(verifiedBlock()),
          access: { role: "viewer", label: "Study reader" }
        }));
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });

    await waitFor(() => expect(
      view.container.querySelector(".access-badge span")?.textContent
    ).toBe("Viewer"));
    await fireEvent.click(await view.findByRole("button", { name: "Research dossiers" }));
    expect(view.container.textContent).toContain("Read-only research record");
    expect(view.queryByRole("button", { name: "Record dossier" })).toBeNull();
    expect(view.container.textContent).not.toContain("A difficult study translation.");
    await fireEvent.click(await view.findByRole("button", { name: "View conclusions" }));
    await waitFor(() => expect(view.container.textContent).toContain("A difficult study translation."));
    expect(view.container.textContent).toContain("Ein Sof");
    expect(view.container.textContent).toContain("parallel external research");
    expect(view.container.textContent).toContain("terminology, allusions");
    expect(view.container.textContent).toContain("Corroborated against cited editions.");
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes(
      "/research-dossiers/dossier-1?include_payload=1&revision_id=revision-1"
    ))).toBe(true);
  });

  it("records an external research dossier as a durable unverified revision", async () => {
    let dossierBody = null;
    const glossary = {
      contract: "translator-block-glossary-v3",
      project_id: "project-1",
      source_plan_id: "plan-1",
      entries: [],
      proposals: [],
      suggestions: []
    };
    const fetchMock = vi.fn((url, options = {}) => {
      const value = String(url);
      if (value.endsWith("/research-dossiers") && options.method === "POST") {
        dossierBody = JSON.parse(options.body);
        return Promise.resolve(jsonResponse({
          dossier: {
            dossier_id: "dossier-1",
            revision_id: "revision-1",
            version: 1,
            scope: { kind: "document", block_ids: [] },
            submitted_at: "2026-09-05T06:30:00+00:00"
          }
        }));
      }
      if (value.endsWith("/research-dossiers")) return Promise.resolve(jsonResponse({ dossiers: [] }));
      if (value.includes("/block-workspace")) return Promise.resolve(jsonResponse(workspace(verifiedBlock())));
      if (value.includes("/block-glossary")) return Promise.resolve(jsonResponse(glossary));
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });

    await fireEvent.click(await view.findByRole("button", { name: "Research dossiers" }));
    const recordButton = await view.findByRole("button", { name: "Record dossier" });
    await waitFor(() => expect(recordButton.disabled).toBe(false));
    await fireEvent.click(recordButton);

    await waitFor(() => expect(dossierBody).not.toBeNull());
    expect(dossierBody).toMatchObject({
      scope: { kind: "document", block_ids: [] },
      provenance: { method: "external research submission", models: [], agents: [], notes: "" },
      payload: {
        document_brief: "",
        terminology: [],
        references: [],
        annotation_proposals: []
      }
    });
    expect(dossierBody.client_submission_id).toEqual(expect.any(String));
  });

  it("selects a retranslation range and records its exact blocks without per-request model overrides", async () => {
    const second = secondBlock({ ...candidate, block_id: "block-2", parts: [{ kind: "text", text: "second" }] });
    const third = {
      ...secondBlock({ ...candidate, block_id: "block-3", parts: [{ kind: "text", text: "third" }] }),
      id: "block-3",
      order: 3
    };
    let retranslationBody = null;
    let retranslationExecuteBody = null;
    const fetchMock = vi.fn((url, options = {}) => {
      const value = String(url);
      if (value.endsWith("/retranslation-requests") && options.method === "POST") {
        retranslationBody = JSON.parse(options.body);
        return Promise.resolve(jsonResponse({ request: { id: "request-1", version: 1, state: "requested" } }));
      }
      if (value.endsWith("/retranslation-requests/request-1/execute") && options.method === "POST") {
        retranslationExecuteBody = JSON.parse(options.body);
        return Promise.resolve(jsonResponse({
          request: { id: "request-1", version: 2, state: "started" },
          operation: { id: "op-1", state: "queued" }
        }));
      }
      if (value.endsWith("/research-dossiers")) return Promise.resolve(jsonResponse({ dossiers: [] }));
      if (value.endsWith("/status")) {
        return Promise.resolve(jsonResponse(workspaceStatus({ id: "op-1", state: "queued" })));
      }
      if (value.includes("/block-workspace")) {
        return Promise.resolve(jsonResponse(workspacePage([verifiedBlock(), second, third], {
          total: 3,
          latestOperation: retranslationBody ? { id: "op-1", state: "queued" } : {}
        })));
      }
      if (value.includes("/settings")) {
        return Promise.resolve(jsonResponse({ translator_model: "mimo" }));
      }
      if (value.includes("/api/llm/models")) {
        return Promise.resolve(jsonResponse({ models: [
          { id: "mimo", name: "MiMo", provider_name: "Xiaomi", ready: true },
          {
            id: "deepseek",
            name: "DeepSeek V4 Flash",
            provider_name: "OpenCode Go",
            ready: true,
            supports_reasoning: true,
            supported_reasoning_levels: ["off", "high"]
          }
        ] }));
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });

    await view.findByRole("button", { name: "Select block 1 for retranslation" });
    await fireEvent.click(view.getByRole("button", { name: "Select block 1 for retranslation" }));
    await fireEvent.click(view.getByRole("button", { name: "Select block 3 for retranslation" }), { shiftKey: true });
    expect(view.getByRole("button", { name: "Retranslate (3)" })).toBeTruthy();
    await fireEvent.click(view.getByRole("button", { name: "Block 2 selected for retranslation" }), { ctrlKey: true });
    expect(view.getByRole("button", { name: "Retranslate (2)" })).toBeTruthy();

    expect(view.queryByRole("combobox", { name: "Model for next translation" })).toBeNull();
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/api/llm/models"))).toBe(false);
    expect(view.getByRole("group", { name: "Document tools" }).contains(view.getByRole("button", { name: "Retranslate (2)" }))).toBe(true);
    await fireEvent.click(view.getByRole("button", { name: "Retranslate (2)" }));
    await fireEvent.change(await view.findByRole("combobox", { name: /research context/i }), { target: { value: "fresh_document" } });
    await fireEvent.click(view.getByRole("button", { name: "Record and start" }));
    await waitFor(() => expect(retranslationBody).toMatchObject({
      scope: { kind: "blocks", block_ids: ["block-1", "block-3"] },
      expected_source_plan_id: "plan-1",
      expected_source_plan_version: 1,
      annotation_profile: "none",
      research_choice: "fresh_document"
    }));
    expect(retranslationBody).not.toHaveProperty("translator_model");
    expect(retranslationBody).not.toHaveProperty("translation_reasoning");
    await waitFor(() => expect(retranslationExecuteBody).toEqual({ expected_version: 1 }));
  });

  it("starts block translation and refreshes the durable operation projection", async () => {
    const fetchMock = vi.fn();
    let workspaceReads = 0;
    fetchMock.mockImplementation((url, options = {}) => {
      if (options.method === "POST") {
        return Promise.resolve(jsonResponse({ operation: { id: "op-1", state: "queued" } }));
      }
      if (String(url).endsWith("/status")) {
        return Promise.resolve(jsonResponse(workspaceStatus({ id: "op-1", state: "queued" })));
      }
      if (String(url).includes("/block-workspace")) {
        workspaceReads += 1;
        return Promise.resolve(jsonResponse(workspacePage([block(candidate, 1)], {
          total: 2,
          latestOperation: workspaceReads > 2 ? { id: "op-1", state: "queued" } : {}
        })));
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });

    await waitFor(() => expect(view.getByRole("button", { name: "Translate remaining blocks" })).toBeTruthy());
    await fireEvent.click(view.getByRole("button", { name: "Translate remaining blocks" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([, options]) => options.method === "POST")).toBe(true));
    await waitFor(() => expect(view.getByTestId("block-operation-state").textContent).toContain("in progress"));
    await waitFor(() => expect(view.getByRole("button", { name: "Translation in progress" }).disabled).toBe(true));
    const postCall = fetchMock.mock.calls.find(([, options]) => options.method === "POST");
    expect(postCall[1].headers["Idempotency-Key"]).toMatch(/^translator\.block\.translate\.project-1\./);
    expect(JSON.parse(postCall[1].body)).not.toHaveProperty("translator_model");
    expect(JSON.parse(postCall[1].body)).not.toHaveProperty("translation_reasoning");
  });

  it("opens only a sandboxed preview after every block has a candidate", async () => {
    const fetchMock = vi.fn((url) => {
      if (String(url).includes("/block-workspace")) return Promise.resolve(jsonResponse(workspace(block(candidate, 1))));
      if (String(url).includes("/preview")) return Promise.resolve(new Response("<main>semantic preview</main>", { status: 200 }));
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });

    await waitFor(() => expect(view.getByRole("button", { name: "Preview document" }).disabled).toBe(false));
    expect(view.queryByRole("button", { name: "Translate remaining blocks" })).toBeNull();
    expect(view.container.textContent).not.toContain("Semantic document workflow");
    const toolbar = view.getByRole("group", { name: "Document tools" });
    expect(view.getByTestId("block-workspace").firstElementChild).toBe(toolbar);
    expect(toolbar.contains(view.getByRole("button", { name: "Preview document" }))).toBe(true);
    await fireEvent.click(view.getByRole("button", { name: "Preview document" }));
    await waitFor(() => expect(view.getByTitle("Semantic translation preview")).toBeTruthy());
    const iframe = view.getByTitle("Semantic translation preview");
    expect(iframe.getAttribute("sandbox")).toBe("allow-scripts");
    expect(iframe.getAttribute("srcdoc")).toContain("script-src 'sha256-");
    expect(iframe.getAttribute("srcdoc")).toContain("semantic preview");
    await fireEvent.click(view.getByRole("button", { name: "Close document preview" }));
    await waitFor(() => expect(view.queryByTitle("Semantic translation preview")).toBeNull());
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/preview"))).toBe(true);
  });

  it("keeps translation disabled for guests and for an active operation", async () => {
    const activePage = workspacePage([block(candidate, 1)], {
      total: 2,
      latestOperation: { id: "op-1", state: "running" }
    });
    const fetchMock = vi.fn((url) => {
      if (String(url).endsWith("/status")) {
        return Promise.resolve(jsonResponse(workspaceStatus({ id: "op-1", state: "running" })));
      }
      if (String(url).includes("/block-workspace")) return Promise.resolve(jsonResponse(activePage));
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const ownerView = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });
    await waitFor(() => expect(ownerView.getByTestId("block-operation-state")).toBeTruthy());

    const guestView = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1", guestMode: true }
      }
    });
    await waitFor(() => expect(guestView.getByTestId("block-operation-state")).toBeTruthy());
    expect(ownerView.getByRole("button", { name: "Translation in progress" }).disabled).toBe(true);
    expect(guestView.getByRole("button", { name: "Translation in progress" }).disabled).toBe(true);
    expect(ownerView.getByRole("button", { name: "Settings" }).disabled).toBe(true);
    expect(guestView.container.querySelector('button[title="Configure the next semantic block operation."]')).toBeNull();
  });

  it("keeps cancel_requested active and posts one explicit stop request", async () => {
    let cancelled = false;
    const fetchMock = vi.fn((url, options = {}) => {
      if (String(url).includes("/cancel") && options.method === "POST") {
        cancelled = true;
        return Promise.resolve(jsonResponse({
          success: true,
          operation: {
            id: "op-1",
            state: "cancel_requested",
            progress: 50,
            message: "Cancellation requested; waiting for a safe worker boundary."
          }
        }));
      }
      if (String(url).endsWith("/status")) {
        return Promise.resolve(jsonResponse(workspaceStatus({
          id: "op-1",
          state: cancelled ? "cancel_requested" : "running",
          progress: 50
        })));
      }
      if (String(url).includes("/block-workspace")) {
        const state = cancelled ? "cancel_requested" : "running";
        return Promise.resolve(jsonResponse(workspacePage([block(candidate, 1)], {
          total: 2,
          latestOperation: { id: "op-1", state, progress: 50 }
        })));
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });

    await fireEvent.click(await view.findByRole("button", { name: "Stop workflow" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/cancel"))).toBe(true));
    await waitFor(() => expect(view.getByText("Stopping block workflow")).toBeTruthy());
    expect(view.getByRole("button", { name: "Stopping..." }).disabled).toBe(true);
    expect(view.getByRole("button", { name: "Settings" }).disabled).toBe(true);
    expect(view.getByRole("button", { name: "Translation in progress" }).disabled).toBe(true);
  });

  it("loads block settings for the owner and sends only the exact block settings payload", async () => {
    const selection = {
      tool_id: "web.search",
      provider_id: "search-provider",
      provider_domain: "web",
      addon_id: "research-addon",
      connection_mode: "connectionless",
      connection_id: ""
    };
    let settingsPut = null;
    const fetchMock = vi.fn((url, options = {}) => {
      if (String(url).includes("/block-workspace")) return Promise.resolve(jsonResponse(workspace(block(candidate, 1))));
      if (String(url).includes("/settings") && options.method === "PUT") {
        settingsPut = JSON.parse(options.body);
        return Promise.resolve(jsonResponse({ success: true }));
      }
      if (String(url).includes("/settings")) {
        return Promise.resolve(jsonResponse({
          translator_model: "translator-ready",
          analyzer_model: "analyzer-ready",
          researcher_model: "researcher-ready",
          enable_pre_analysis: true,
          enable_research: true,
          enable_translator_notes: true,
          translation_unit_mode: "aligned_sentences",
          research_tool_selections: [selection],
          translation_batch_input_tokens: 128000,
          translation_batch_max_blocks: 18,
          custom_prompt: "Keep footnotes semantic."
        }));
      }
      if (String(url).includes("/llm/models")) {
        return Promise.resolve(jsonResponse({ models: [
          { id: "translator-ready", name: "Translator", provider: "Test", ready: true },
          { id: "analyzer-ready", name: "Analyzer", provider: "Test", ready: true },
          { id: "researcher-ready", name: "Researcher", provider: "Test", ready: true }
        ] }));
      }
      if (String(url).includes("/block-research-tools")) {
        return Promise.resolve(jsonResponse([{
          tool_id: "web.search",
          tool_name: "Search",
          provider_id: "search-provider",
          provider_name: "Search provider",
          provider_domain: "web",
          addon_id: "research-addon",
          connection_modes: ["connectionless"],
          connections: [],
          executable: true,
          private_data_capable: false,
          authority_warning: "",
          unavailable_reason: ""
        }]));
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });

    await waitFor(() => expect(view.getByRole("button", { name: "Settings" }).disabled).toBe(false));
    await fireEvent.click(view.getByRole("button", { name: "Settings" }));
    await waitFor(() => expect(view.getByRole("heading", { name: "Semantic block settings" })).toBeTruthy());
    expect(view.container.textContent).not.toContain("chunk size");
    expect(view.container.textContent).not.toContain("research_plugin_ids");
    await fireEvent.click(view.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(settingsPut).toBeTruthy());
    expect(Object.keys(settingsPut).sort()).toEqual([
      "analyzer_model",
      "annotation_profile",
      "custom_prompt",
      "enable_pre_analysis",
      "enable_research",
      "enable_translator_notes",
      "research_tool_selections",
      "researcher_model",
      "translation_batch_input_tokens",
      "translation_batch_max_blocks",
      "translation_context_mode",
      "translation_unit_mode",
      "translator_model"
    ]);
    expect(settingsPut.research_tool_selections).toEqual([selection]);
    expect(settingsPut.translation_batch_input_tokens).toBe(128000);
    expect(settingsPut.translation_batch_max_blocks).toBe(18);
    expect(settingsPut.translation_unit_mode).toBe("aligned_sentences");
    expect(settingsPut.enable_translator_notes).toBe(true);
    expect(settingsPut.annotation_profile).toBe("restrained");
    expect(settingsPut.custom_prompt).toBe("Keep footnotes semantic.");
  });

  it.each(["auto", "jpeg", "webp"])("loads a saved %s cover crop and saves its edition against a fresh source witness", async (imageMode) => {
    const recipe = { epub_options: { profile: "publishing-v2", direction: "rtl" }, epub_cover: { mode: "pdf_page", page: 3, crop: [0.1, 0, 0.9, 1] }, epub_image_mode: imageMode };
    const edition = { schema: "epub-edition-v1", version: 3, source_revision: "a".repeat(64), current_source_revision: "a".repeat(64), stale: false, recipe, effective_recipe: recipe };
    let savedBody;
    const fetchMock = vi.fn((url, options = {}) => {
      const path = new URL(String(url), "http://localhost").pathname;
      if (path.endsWith("/block-workspace")) return Promise.resolve(jsonResponse(workspace(verifiedBlock())));
      if (path.endsWith("/export-metadata")) return Promise.resolve(jsonResponse({ version: 0, metadata: {}, cover_source: { pdf: true, document_id: "document-1", candidate: { page: 1 } } }));
      if (path.endsWith("/epub/edition")) {
        if (options.method === "PUT") {
          savedBody = JSON.parse(options.body);
          return Promise.resolve(jsonResponse({ ...edition, version: 4, recipe: savedBody.recipe, effective_recipe: savedBody.recipe }));
        }
        return Promise.resolve(jsonResponse(edition));
      }
      if (path.endsWith("/epub/preflight")) return Promise.resolve(jsonResponse({
        options: JSON.parse(options.body).epub_options, edition_revision: "d".repeat(64),
        review_hash: "c".repeat(64), numbering_revision: 0, suggested_chapter_level: 2,
        outline: { input_sha256: "a".repeat(64), headings: [], tables: [], images: [], pages: [] },
        quality: { schema: "epub-quality-v1", profile: "publishing-v2", input_sha256: "a".repeat(64), options_sha256: "b".repeat(64), artifact_sha256: "0".repeat(64), content_sha256: "0".repeat(64), counts: {}, epubcheck_version: null, issue_count: 0, issues: [], checks: { structure: "not_run", epubcheck: "not_run", ace: "not_run", manual: "not_reviewed" } }
      }));
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceApp, { props: { project: { id: "project-1", engine_contract: "document-block-v1" }, bootstrapData: { projectId: "project-1" } } });
    await waitFor(() => expect(view.getByRole("button", { name: "Export document" }).disabled).toBe(false));
    await fireEvent.click(view.getByRole("button", { name: "Export document" }));
    const save = await view.findByRole("button", { name: "Save edition choices" });
    await waitFor(() => expect(save.disabled).toBe(false));
    expect(view.getByAltText("Source PDF page for choosing the cover crop").getAttribute("src")).toContain("pdf-pages/2/thumbnail");
    const left = Array.from(view.container.querySelectorAll("label")).find((label) => label.textContent.trim() === "Left (%)")?.querySelector("input");
    expect(left?.value).toBe("10");
    expect(view.getByLabelText("Image format").value).toBe(imageMode);
    await fireEvent.click(save);
    await view.findByText("Edition choices saved.");
    expect(savedBody).toMatchObject({ expected_version: 3, expected_source_revision: "d".repeat(64), recipe });
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/epub/preflight"))).toHaveLength(1);
    expect(JSON.parse(fetchMock.mock.calls.find(([url]) => String(url).endsWith("/epub/preflight"))[1].body).epub_image_mode).toBe(imageMode);
  });

  it.each(["generated", "pdf_page"])("queues %s covers with the translated edition details", async (mode) => {
    let exportBody = null;
    vi.stubGlobal("fetch", vi.fn((url, options = {}) => {
      const path = new URL(String(url), "http://localhost").pathname;
      if (path.endsWith("/epub/preflight")) return Promise.resolve(epubPreflightResponse(options));
      if (path.endsWith("/block-workspace")) return Promise.resolve(jsonResponse(workspace(verifiedBlock())));
      if (path.endsWith("/export-metadata")) return Promise.resolve(jsonResponse({ version: 0, metadata: {}, cover_source: { pdf: true, document_id: "document-1", candidate: { page: 3, evidence: "Title page." } } }));
      if (path.endsWith("/export") && options.method === "POST") {
        exportBody = JSON.parse(options.body);
        return Promise.resolve(new Response(JSON.stringify({ error: "Fixture stopped after request capture." }), { status: 400, headers: { "Content-Type": "application/json" } }));
      }
      throw new Error(`Unexpected request: ${url}`);
    }));
    const view = render(BlockWorkspaceApp, { props: { project: { id: "project-1", engine_contract: "document-block-v1" }, bootstrapData: { projectId: "project-1" } } });
    await waitFor(() => expect(view.getByRole("button", { name: "Export document" }).disabled).toBe(false));
    await fireEvent.click(view.getByRole("button", { name: "Export document" }));
    await waitFor(() => expect(view.getByRole("button", { name: "Export and download" }).disabled).toBe(false));
    await fireEvent.input(view.getByLabelText("Title"), { target: { value: "Translated title" } });
    await fireEvent.input(view.getByLabelText("Authors"), { target: { value: "Edition Author" } });
    await fireEvent.click(view.getByLabelText(mode === "generated" ? "Generate from this edition’s title and authors" : "Use a source PDF page"));
    if (mode === "pdf_page") expect(view.getByAltText("Source PDF page for choosing the cover crop").getAttribute("src")).toContain("pdf-pages/2/thumbnail");
    await fireEvent.click(view.getByRole("button", { name: "Export and download" }));
    await waitFor(() => expect(exportBody?.epub_cover).toEqual(mode === "pdf_page" ? { mode, page: 3 } : { mode }));
    expect(exportBody.export_metadata).toMatchObject({ title: "Translated title", authors: ["Edition Author"] });
    expect(exportBody.epub_review_hash).toBe("c".repeat(64));
    const preflightCall = vi.mocked(fetch).mock.calls.find(([url]) => String(url).endsWith("/epub/preflight"));
    expect(JSON.parse(preflightCall[1].body).export_metadata).toEqual(exportBody.export_metadata);
    expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).endsWith("/api/upload"))).toBe(false);
    view.unmount();
  });

  it("gates semantic export on a complete, structurally verified target", async () => {
    const fetchMock = vi.fn((url) => {
      if (String(url).includes("/block-workspace")) return Promise.resolve(jsonResponse(workspace(block(candidate, 1))));
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const unverifiedView = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });
    await waitFor(() => expect(unverifiedView.getByTestId("block-export-actions")).toBeTruthy());
    expect(unverifiedView.getByRole("button", { name: "Export document" }).disabled).toBe(true);
    unverifiedView.unmount();

    vi.stubGlobal("fetch", vi.fn((url) => {
      if (String(url).includes("/block-workspace")) return Promise.resolve(jsonResponse(workspace(verifiedBlock())));
      throw new Error(`Unexpected request: ${url}`);
    }));
    const verifiedView = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });
    await waitFor(() => expect(verifiedView.getByRole("button", { name: "Export document" }).disabled).toBe(false));
    await fireEvent.click(verifiedView.getByRole("button", { name: "Export document" }));
    expect(verifiedView.getByRole("radio", { name: /EPUB/ })).toBeTruthy();
    expect(verifiedView.getByText(/printed source TOC is omitted by default/i)).toBeTruthy();
    expect(verifiedView.getAllByText(/printed source TOC by default/i).length).toBeGreaterThanOrEqual(2);
  });

  it("fails closed on malformed or mismatched export job URLs", async () => {
    const invalidStatusFetch = vi.fn((url, options = {}) => {
      if (String(url).endsWith("/epub/preflight")) return Promise.resolve(epubPreflightResponse(options));
      if (String(url).endsWith("/export-metadata")) return Promise.resolve(jsonResponse({ version: 0, metadata: {} }));
      if (String(url).includes("/block-workspace")) return Promise.resolve(jsonResponse(workspace(verifiedBlock())));
      if (options.method === "POST") {
        return Promise.resolve(jsonResponse({ status_url: "/addons/translator/api/projects/project-2/jobs/job-1" }));
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", invalidStatusFetch);
    const invalidStatusView = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });
    await waitFor(() => expect(invalidStatusView.getByRole("button", { name: "Export document" }).disabled).toBe(false));
    await fireEvent.click(invalidStatusView.getByRole("button", { name: "Export document" }));
    await waitFor(() => expect(invalidStatusView.getByRole("button", { name: "Export and download" }).disabled).toBe(false));
    await fireEvent.click(invalidStatusView.getByRole("button", { name: "Export and download" }));
    await waitFor(() => expect(invalidStatusView.getByText(/invalid job status URL/i)).toBeTruthy());
    invalidStatusView.unmount();

    const mismatchFetch = vi.fn((url, options = {}) => {
      if (String(url).endsWith("/epub/preflight")) return Promise.resolve(epubPreflightResponse(options));
      if (String(url).endsWith("/export-metadata")) return Promise.resolve(jsonResponse({ version: 0, metadata: {} }));
      if (String(url).includes("/block-workspace")) return Promise.resolve(jsonResponse(workspace(verifiedBlock())));
      if (options.method === "POST") {
        return Promise.resolve(jsonResponse({ status_url: "/addons/translator/api/projects/project-1/jobs/job-1" }));
      }
      if (String(url).includes("/jobs/job-1")) {
        return Promise.resolve(jsonResponse({
          status: "completed",
          result: { download_url: "/addons/translator/api/projects/project-1/jobs/job-2/download" }
        }));
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", mismatchFetch);
    const mismatchView = render(BlockWorkspaceApp, {
      props: {
        project: { id: "project-1", name: "Blocks", engine_contract: "document-block-v1" },
        bootstrapData: { projectId: "project-1" }
      }
    });
    await waitFor(() => expect(mismatchView.getByRole("button", { name: "Export document" }).disabled).toBe(false));
    await fireEvent.click(mismatchView.getByRole("button", { name: "Export document" }));
    await waitFor(() => expect(mismatchView.getByRole("button", { name: "Export and download" }).disabled).toBe(false));
    await fireEvent.click(mismatchView.getByRole("button", { name: "Export and download" }));
    await waitFor(() => expect(mismatchView.getByText(/safe download URL/i)).toBeTruthy());
    expect(mismatchFetch.mock.calls.some(([url]) => String(url).includes("job-2/download"))).toBe(false);
  });

  it("polls a newly queued export immediately and reports failure without stranding the workspace", async () => {
    const job = deferred();
    let terminal = false;
    const fetchMock = vi.fn((url, options = {}) => {
      const path = new URL(String(url), "http://localhost").pathname;
      if (path.endsWith("/epub/preflight")) return Promise.resolve(epubPreflightResponse(options));
      if (path.endsWith("/export") && options.method === "POST") {
        return Promise.resolve(jsonResponse({
          operation: { id: "export-op", state: "queued" },
          status_url: "/addons/translator/api/projects/project-1/jobs/job-1"
        }));
      }
      if (path.endsWith("/status")) return Promise.resolve(jsonResponse(workspaceStatus({ id: "export-op", state: "running" })));
      if (path.endsWith("/export-metadata")) return Promise.resolve(jsonResponse({ version: 0, metadata: {} }));
      if (path.endsWith("/jobs/job-1")) return job.promise;
      if (path.endsWith("/block-workspace")) return Promise.resolve(jsonResponse(workspacePage([verifiedBlock()], {
        latestOperation: terminal ? { id: "export-op", state: "failed" } : {}
      })));
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceApp, {
      props: { project: { id: "project-1", engine_contract: "document-block-v1" }, bootstrapData: { projectId: "project-1" } }
    });
    await waitFor(() => expect(view.getByRole("button", { name: "Export document" }).disabled).toBe(false));
    await fireEvent.click(view.getByRole("button", { name: "Export document" }));
    await waitFor(() => expect(view.getByRole("button", { name: "Export and download" }).disabled).toBe(false));
    await fireEvent.click(view.getByRole("button", { name: "Export and download" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/status"))).toBe(true));
    await fireEvent.click(view.getByRole("button", { name: "Hide", exact: true }));
    await fireEvent.click(view.getByRole("button", { name: "View export progress" }));
    expect(fetchMock.mock.calls.filter(([url, options]) => new URL(String(url), "http://localhost").pathname.endsWith("/export") && options?.method === "POST").length).toBe(1);
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/epub/preflight")).length).toBe(1);
    terminal = true;
    job.resolve(jsonResponse({ status: "failed", message: "Document conversion failed." }));
    await waitFor(() => expect(view.getByText("Document conversion failed.").getAttribute("role")).toBe("alert"));
    await waitFor(() => expect(view.getByRole("button", { name: "Export document" }).disabled).toBe(false));
  });

  it("prepares a user-initiated blob download with metadata without waiting for a workspace reload", async () => {
    const refresh = deferred();
    let workspaceReads = 0;
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const createObjectURL = vi.fn(() => "blob:http://localhost/prepared-book");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", Object.assign(class extends URL {}, { createObjectURL, revokeObjectURL }));
    vi.stubGlobal("fetch", vi.fn((url, options = {}) => {
      const path = new URL(String(url), "http://localhost").pathname;
      if (path.endsWith("/epub/preflight")) return Promise.resolve(epubPreflightResponse(options));
      if (path.endsWith("/block-workspace")) {
        workspaceReads += 1;
        return workspaceReads === 1 ? Promise.resolve(jsonResponse(workspace(verifiedBlock()))) : refresh.promise;
      }
      if (path === "/api/upload") {
        expect(options.body.get("file").name).toBe("cover.png");
        return Promise.resolve(jsonResponse({ file_path: "/uploads/7/cover.png" }));
      }
      if (options.method === "POST") {
        expect(JSON.parse(options.body).export_metadata).toMatchObject({
          title: "English title", authors: ["Author"], translators: ["Translator"],
          subtitle: "A study", description: "Edition description", publisher: "Publisher",
          publication_date: "2026-09", rights: "CC BY 4.0", subjects: ["Philosophy", "Freedom"],
          journal_or_series: "Collected works", volume: "2", editors: ["Editor"],
          identifiers: [{ scheme: "ISBN", value: "9781234567897" }],
          other_contributors: [{ name: "Illustrator", role: "ill" }],
          cover_upload_path: "/uploads/7/cover.png"
        });
        return Promise.resolve(jsonResponse({ status_url: "/addons/translator/api/projects/project-1/jobs/job-1" }));
      }
      if (path.endsWith("/jobs/job-1/download")) return Promise.resolve(new Response(new Blob(["EPUB bytes"])));
      if (path.endsWith("/export-metadata")) return Promise.resolve(jsonResponse({ version: 0, metadata: {} }));
      if (path.endsWith("/jobs/job-1")) return Promise.resolve(jsonResponse({
        status: "completed", result: { download_url: "/addons/translator/api/projects/project-1/jobs/job-1/download" }
      }));
      throw new Error(`Unexpected request: ${url}`);
    }));
    const view = render(BlockWorkspaceApp, {
      props: { project: { id: "project-1", engine_contract: "document-block-v1" }, bootstrapData: { projectId: "project-1" } }
    });
    await waitFor(() => expect(view.getByRole("button", { name: "Export document" }).disabled).toBe(false));
    await fireEvent.click(view.getByRole("button", { name: "Export document" }));
    await waitFor(() => expect(view.getByRole("button", { name: "Export and download" }).disabled).toBe(false));
    await fireEvent.input(view.getByLabelText("Title"), { target: { value: "English title" } });
    await fireEvent.input(view.getByLabelText("Authors"), { target: { value: "Author" } });
    await fireEvent.input(view.getByLabelText("Translators"), { target: { value: "Translator" } });
    await fireEvent.input(view.getByLabelText("Subtitle"), { target: { value: "A study" } });
    await fireEvent.change(view.getByLabelText("Cover image"), { target: { files: [new File(["synthetic cover"], "cover.png", { type: "image/png" })] } });
    await fireEvent.click(view.getByText("More book details"));
    for (const [label, value] of Object.entries({ Description: "Edition description", Publisher: "Publisher", "Publication date": "2026-09", "Rights / license": "CC BY 4.0", "Subjects / tags": "Philosophy\nFreedom", Series: "Collected works", Volume: "2", Editors: "Editor" })) {
      await fireEvent.input(view.getByLabelText(label), { target: { value } });
    }
    await fireEvent.click(view.getByRole("button", { name: "Add identifier" }));
    await fireEvent.input(view.getByLabelText("Identifier", { exact: true }), { target: { value: "9781234567897" } });
    await fireEvent.click(view.getByRole("button", { name: "Add contributor" }));
    await fireEvent.input(view.getByLabelText("Contributor name"), { target: { value: "Illustrator" } });
    await fireEvent.click(view.getByRole("button", { name: "Export and download" }));
    await waitFor(() => expect(createObjectURL).toHaveBeenCalledOnce());
    expect(click).not.toHaveBeenCalled();
    const download = view.getByRole("link", { name: "Download prepared document" });
    expect(download.href).toBe("blob:http://localhost/prepared-book");
    expect(download.hasAttribute("download")).toBe(true);
    expect(view.getByText(/Export ready/)).toBeTruthy();
    refresh.resolve(jsonResponse(workspace(verifiedBlock())));
    view.unmount();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:http://localhost/prepared-book");
  });
});
