import { describe, expect, it, vi } from "vitest";
import { buildProjectCreatePayload, retainCreateRequestKey } from "./create-request.js";

describe("project create idempotency", () => {
  it("keeps passive context defaults and explicit retained context on document projects", () => {
    expect(buildProjectCreatePayload({ source_type: "document" }, false).translation_context_mode).toBe("self_contained");
    expect(buildProjectCreatePayload({ source_type: "document", translation_context_mode: "retained" }, false).translation_context_mode).toBe("retained");
    expect(buildProjectCreatePayload({ source_type: "subtitle_studio", translation_context_mode: "retained" }, false)).not.toHaveProperty("translation_context_mode");
  });
  it("reuses the key for an identical retry and rotates it after a payload change", () => {
    const createKey = vi.fn(() => `key-${createKey.mock.calls.length + 1}`);
    const first = retainCreateRequestKey(null, { name: "A" }, createKey);
    const retry = retainCreateRequestKey(first.state, { name: "A" }, createKey);
    const changed = retainCreateRequestKey(retry.state, { name: "B" }, createKey);
    expect(retry.key).toBe(first.key);
    expect(changed.key).not.toBe(first.key);
    expect(createKey).toHaveBeenCalledTimes(2);
  });

  it("treats the selected translation engine as part of the idempotent request", () => {
    const createKey = vi.fn(() => `key-${createKey.mock.calls.length + 1}`);
    const block = retainCreateRequestKey(null, { name: "A", engine_contract: "document-block-v1" }, createKey);
    const legacy = retainCreateRequestKey(block.state, { name: "A", engine_contract: "segment-v1" }, createKey);
    expect(legacy.key).not.toBe(block.key);
  });

  it("uses canonical research selections for document blocks and keeps legacy IDs for segments", () => {
    const exact = {
      tool_id: "web.search",
      provider_id: "serp",
      provider_domain: "web",
      addon_id: "research-addon",
      connection_mode: "saved",
      connection_id: "conn-1"
    };
    const blockPayload = buildProjectCreatePayload({
      source_type: "document",
      engine_contract: "segment-v1",
      research_plugin_ids: ["legacy-provider"],
      research_tool_selections: [exact, exact],
      translation_batch_input_tokens: 64000,
      translation_batch_max_blocks: 24,
      chunk_size: 2500
    }, true);
    expect(blockPayload.research_tool_selections).toEqual([exact]);
    expect(blockPayload.engine_contract).toBe("document-block-v1");
    expect(blockPayload.translation_batch_input_tokens).toBe(64000);
    expect(blockPayload.translation_batch_max_blocks).toBe(24);
    expect(blockPayload).not.toHaveProperty("chunk_size");
    expect(blockPayload).not.toHaveProperty("research_plugin_ids");

    const legacyPayload = buildProjectCreatePayload({
      source_type: "subtitle_studio",
      engine_contract: "document-block-v1",
      research_plugin_ids: ["legacy-provider", "legacy-provider"],
      research_tool_selections: [exact],
      translation_batch_input_tokens: 64000,
      translation_batch_max_blocks: 24
    }, true);
    expect(legacyPayload.engine_contract).toBe("segment-v1");
    expect(legacyPayload.research_plugin_ids).toEqual(["legacy-provider"]);
    expect(legacyPayload).not.toHaveProperty("translation_batch_input_tokens");
    expect(legacyPayload).not.toHaveProperty("translation_batch_max_blocks");
    expect(legacyPayload).not.toHaveProperty("research_tool_selections");
  });

  it("never silently disables research that the user requested", () => {
    expect(() => buildProjectCreatePayload({
      source_type: "document",
      engine_contract: "document-block-v1",
      enable_research: true,
      research_tool_selections: []
    }, false)).toThrow(/requested research is not executable/i);
  });

  it("keeps sentence mode in document creation and out of timed subtitles", () => {
    expect(buildProjectCreatePayload({ source_type: "document", translation_unit_mode: "aligned_sentences" }, false).translation_unit_mode).toBe("aligned_sentences");
    expect(buildProjectCreatePayload({ source_type: "document" }, false).translation_unit_mode).toBe("block");
    expect(buildProjectCreatePayload({ source_type: "subtitle_studio", translation_unit_mode: "aligned_sentences" }, false)).not.toHaveProperty("translation_unit_mode");
  });
});
