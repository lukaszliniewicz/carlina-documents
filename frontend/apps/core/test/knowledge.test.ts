import assert from "node:assert/strict";
import test from "node:test";

import type {
  FrontendKnowledgeCollection,
  FrontendKnowledgeItem,
  FrontendKnowledgeStreamEvent,
} from "@carlina/sdk";

import {
  collectionMetadataRows,
  consumeKnowledgeEventStream,
  filterKnowledgeCollections,
  knowledgeItemTitle,
  knowledgeJobFailed,
  knowledgeJobFinished,
  parseKnowledgeSearchResult,
} from "../src/lib/knowledge.ts";

function collection(
  overrides: Partial<FrontendKnowledgeCollection> = {},
): FrontendKnowledgeCollection {
  return {
    id: 1,
    user_id: 7,
    name: "Autumn references",
    description: "Muted editorial direction",
    item_count: 3,
    created_at: "2026-07-26T10:00:00",
    source_type: "document_upload",
    collection_mode: "vector",
    source_metadata: { engine: "hybrid_pdf" },
    last_synced_at: null,
    storage_path: "7/collections/1",
    ...overrides,
  };
}

test("filters collections through semantic names, modes, and source details", () => {
  const values = [
    collection(),
    collection({
      id: 2,
      name: "Editorial correspondence",
      source_type: "email",
      collection_mode: "regular",
      source_metadata: {
        account_label: "editor@example.test",
        scope: "threads",
      },
    }),
  ];

  assert.deepEqual(
    filterKnowledgeCollections(values, {
      query: "editor@example.test",
      mode: "all",
      source: "all",
    }).map((item) => item.id),
    [2],
  );
  assert.deepEqual(
    filterKnowledgeCollections(values, {
      query: "",
      mode: "vector",
      source: "document_upload",
    }).map((item) => item.id),
    [1],
  );
  assert.deepEqual(collectionMetadataRows(values[0]), [
    { label: "Default extractor", value: "hybrid_pdf" },
  ]);
});

test("derives readable item titles and parses retrieval envelopes", () => {
  const item: FrontendKnowledgeItem = {
    id: 9,
    user_id: 7,
    source_type: "document_upload",
    source_id: "file::fallback.pdf",
    content_snippet: "Evidence",
    metadata: { original_filename: "editorial-reference.pdf" },
    has_embedding: true,
    created_at: null,
    updated_at: null,
  };

  assert.equal(knowledgeItemTitle(item), "editorial-reference.pdf");
  assert.deepEqual(
    parseKnowledgeSearchResult(
      "### SOURCE DOCUMENT METADATA: [Page 3 · Editorial brief] ###\nMuted evidence.",
    ),
    {
      metadata: "Page 3 · Editorial brief",
      text: "Muted evidence.",
    },
  );
  assert.deepEqual(parseKnowledgeSearchResult({ content: "Plain passage" }), {
    metadata: "",
    text: "Plain passage",
  });
});

test("recognizes terminal job states", () => {
  assert.equal(knowledgeJobFinished("completed"), true);
  assert.equal(knowledgeJobFinished("running"), false);
  assert.equal(knowledgeJobFailed("error"), true);
  assert.equal(knowledgeJobFailed("queued"), false);
});

test("consumes split and multiline server-sent event frames", async () => {
  const encoder = new TextEncoder();
  const parts = [
    'data: {"type":"log","message":"Starting"}\n\n',
    'data: {"type":"progress",\n',
    'data: "message":"Halfway","percent":50}\n\n',
    "data: final plain-text update\n\n",
  ];
  const response = new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const part of parts) controller.enqueue(encoder.encode(part));
        controller.close();
      },
    }),
    { status: 200 },
  );
  const events: FrontendKnowledgeStreamEvent[] = [];

  await consumeKnowledgeEventStream(response, (event) => events.push(event));

  assert.deepEqual(events, [
    { type: "log", message: "Starting" },
    { type: "progress", message: "Halfway", percent: 50 },
    { type: "log", message: "final plain-text update" },
  ]);
});
